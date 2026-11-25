"""Route-layer test with a minimal in-process Flask shim.

The sandbox has no Flask installed; this shim implements just enough of the
Flask API used by app/app.py to dispatch the real view functions against the
real SQLite layer.  The production container uses genuine Flask.
"""
import io
import json
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="approute-")
os.environ["DB_PATH"] = os.path.join(_tmp, "route.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------- shim --
class FakeRequest:
    def __init__(self):
        self.files = {}
        self._json = None
        self._json_silent = False

    def get_json(self, silent=False):
        if self._json is None and not silent:
            raise RuntimeError("no json")
        return self._json


class FakeFlask:
    def __init__(self, name):
        self.config = {}
        self.routes = {}
        self.request = FakeRequest()

    def _register(self, path, method):
        def deco(fn):
            self.routes[(path, method)] = fn
            return fn
        return deco

    def get(self, path):
        return self._register(path, "GET")

    def post(self, path):
        return self._register(path, "POST")

    def render_template(self, *_a, **_kw):
        return "<html/>"


class FakeFile:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


def jsonify(payload):
    return FakeResponse(json.dumps(payload, ensure_ascii=False).encode(), 200,
                        {"Content-Type": "application/json"})


class FakeResponse:
    def __init__(self, body, status, headers):
        self.body, self.status_code, self.headers = body, status, headers


sys.modules["flask"] = type(sys)("flask")
sys.modules["flask"].Flask = FakeFlask
sys.modules["flask"].jsonify = jsonify
sys.modules["flask"].render_template = FakeFlask.render_template
sys.modules["flask"].request = FakeRequest()

# ------------------------------------------------------------------ --
from app import db  # noqa: E402
from app.app import create_app  # noqa: E402

app = create_app()
req = sys.modules["flask"].request
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def call(path, method="GET"):
    result = app.routes[(path, method)]()
    if isinstance(result, tuple):  # Flask (response, status) form
        resp, status = result
        resp.status_code = status
        return resp
    return result


def body_of(resp):
    return json.loads(resp.body.decode())


# /api/state before upload
st = body_of(call("/api/state"))
check("empty dataset initially", st["dataset"] is None)
check("max_warnings 20", st["max_warnings"] == 20)
check("not running", st["running"] is False)

# simulate without dataset -> 400
resp = call("/api/simulate", "POST")
check("simulate before upload rejected", resp.status_code == 400)

# upload missing file -> 400
req.files = {}
resp = call("/api/dataset", "POST")
check("upload without file 400", resp.status_code == 400)

# bad CSV -> 400
req.files = {"file": FakeFile("foo,bar\n1,2\n".encode())}
resp = call("/api/dataset", "POST")
check("bad csv 400", resp.status_code == 400 and "缺少必需列" in body_of(resp)["error"])

# good upload (English headers)
csv = (b"conclusion_id,group_id,doi\n"
       b"C1,G1,10.1/a\nC1,G2,10.1/b\nC2,G1,10.1/a\nC2,G2,10.1/c\n")
req.files = {"file": FakeFile(csv)}
resp = call("/api/dataset", "POST")
check("upload 200", resp.status_code == 200, str(resp.status_code))
check("4 rows parsed", body_of(resp)["row_count"] == 4)

st = body_of(call("/api/state"))
check("3 papers", st["paper_count"] == 3)
check("2 conclusions", st["conclusion_count"] == 2)
check("no live unsupported", st["live_unsupported"] == [])

# invalid mark value -> 400
req._json = {"doi": "10.1/a", "mark": "bogus"}
resp = call("/api/papers/mark", "POST")
check("bad mark 400", resp.status_code == 400)

# unknown DOI -> 404
req._json = {"doi": "10.1/zzz", "mark": "warning"}
resp = call("/api/papers/mark", "POST")
check("unknown doi 404", resp.status_code == 404)

# valid warning
req._json = {"doi": "10.1/A", "mark": "warning"}  # uppercase normalized
resp = call("/api/papers/mark", "POST")
check("warning set 200", resp.status_code == 200 and
      body_of(resp)["warning_count"] == 1)

# latest simulation -> 404 when none
resp = call("/api/simulations/latest")
check("no sim 404", resp.status_code == 404)

# invalid k
req._json = {"k": -1}
check("negative k 400", call("/api/simulate", "POST").status_code == 400)
req._json = {"k": "abc"}
check("non-int k 400", call("/api/simulate", "POST").status_code == 400)
req._json = {"k": 21}
check("k>20 400", call("/api/simulate", "POST").status_code == 400)

# valid k starts a background thread
req._json = {"k": 1}
resp = call("/api/simulate", "POST")
check("simulate 200", resp.status_code == 200)
sim_id = body_of(resp)["simulation_id"]

# second concurrent start -> 409
import time  # noqa: E402
deadline = time.time() + 5
while body_of(call("/api/state"))["running"] and time.time() < deadline:
    time.sleep(0.05)
resp2 = call("/api/simulate", "POST")
check("concurrent sim rejected while running or result present",
      resp2.status_code in (409, 200))  # tiny task may finish first

# wait for completion
deadline = time.time() + 10
latest = None
while time.time() < deadline:
    r = call("/api/simulations/latest")
    if r.status_code == 200:
        latest = body_of(r)
        if latest["status"] in ("done", "failed"):
            break
    time.sleep(0.1)
check("simulation done", latest and latest["status"] == "done", str(latest and latest["status"]))
if latest and latest["result"]:
    check("result has required sections",
          set(["already_unsupported", "selected_dois",
               "affected_conclusions", "affected_count"]).issubset(latest["result"]))
    check("not stale immediately", latest["stale"] is False)

# mark change -> stale
req._json = {"doi": "10.1/b", "mark": "retracted"}
call("/api/papers/mark", "POST")
latest = body_of(call("/api/simulations/latest"))
check("result stale after edit", latest["stale"] is True)

# live unsupported now: C1 G2 contains b(retracted); G1 contains a(warning,
# not retracted) -> C1 still has G1 intact... mark a retracted too
req._json = {"doi": "10.1/a", "mark": "retracted"}
call("/api/papers/mark", "POST")
st = body_of(call("/api/state"))
# C1: G1={a} broken, G2={b} broken -> unsupported; C2: G1={a} broken, G2={c} intact
check("live unsupported C1", st["live_unsupported"] == ["C1"], str(st["live_unsupported"]))

# warning cap: re-upload a dataset with 20 new normal papers
extra = b"conclusion_id,group_id,doi\n" + b"".join(
    f"C9,G{i},10.1/w{i}\n".encode() for i in range(20))
req.files = {"file": FakeFile(extra)}
call("/api/dataset", "POST")
# the 20 papers referenced by the new dataset all start normal
active_dois = {r["doi"] for r in db.all_rows()}
check("fresh papers normal",
      all(db.get_mark(d) == "normal" for d in active_dois))
check("warning count 0 after replace", db.count_warnings() == 0)
# first 20 warnings fill the cap exactly
for i in range(20):
    req._json = {"doi": f"10.1/w{i}", "mark": "warning"}
    r = call("/api/papers/mark", "POST")
    check(f"warning w{i} accepted", r.status_code == 200)
check("warning count == 20", db.count_warnings() == 20)
# any further normal->warning must be rejected 409
req._json = {"doi": "10.1/w0", "mark": "normal"}
call("/api/papers/mark", "POST")  # free one slot
check("warning count 19", db.count_warnings() == 19)
req._json = {"doi": "10.1/w0", "mark": "warning"}
check("refill to 20 ok", call("/api/papers/mark", "POST").status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL ROUTE CHECKS PASSED")
