"""End-to-end business-flow tests using the real SQLite layer and solver.

Flask itself is only HTTP glue; this script exercises the same calls the
routes make, against a temporary database, using the standard library only.
"""
import os
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="appdb-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.csv_import import parse_csv  # noqa: E402
from app.solver import analyze  # noqa: E402

CSV = """结论号,引文组号,DOI
C1,G1,10.1/a
C1,G1,10.1/b
C1,G2,10.1/c
C2,G1,10.1/a
C2,G2,10.1/d
C3,G1,10.1/e
C3,G2,10.1/f
C4,G1,10.1/g
""".encode("utf-8")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


db.init_db()

# 1. upload
records = parse_csv(CSV)
db.replace_dataset(records, "2026-11-25T10:00:00")
check("dataset loaded", db.dataset_loaded())
check("4 conclusions", len({r[0] for r in records}) == 4)
check("7 unique papers", len(db.get_papers()) == 7)
check("all normal initially", all(r["mark"] == "normal" for r in db.get_papers()))

# 2. warning cap of 20
for r in db.get_papers():
    db.set_mark(r["doi"], "warning", "2026-11-25T10:01:00")
check("can mark 7 warnings (<20)", db.count_warnings() == 7)

# reset
for r in db.get_papers():
    db.set_mark(r["doi"], "normal", "2026-11-25T10:02:00")

# 3. scenario: retract a -> C1/G1 and C2/G1 broken; warn c and d
db.set_mark("10.1/a", "retracted", "2026-11-25T10:03:00")
db.set_mark("10.1/c", "warning", "2026-11-25T10:03:00")
db.set_mark("10.1/d", "warning", "2026-11-25T10:03:00")
check("warning count 2", db.count_warnings() == 2)

rows = [(r["conclusion_id"], r["group_id"], r["doi"]) for r in db.all_rows()]
marks0 = db.current_marks()

# 4. snapshot simulation
snapshot_marks = dict(marks0)
sim1 = db.insert_simulation("2026-11-25T10:04:00", 1,
                            {"marks": snapshot_marks,
                             "dataset_uploaded_at": "2026-11-25T10:00:00",
                             "captured_at": "2026-11-25T10:04:00"})
result1 = analyze(rows, snapshot_marks, 1)
db.finish_simulation(sim1, "2026-11-25T10:04:02", result1)
# k=1: c kills C1 (G1 broken by a, G2 by c); d kills C2. tie -> c (lex smaller)
check("sim1 affected==1", result1["affected_count"] == 1, str(result1["selected_dois"]))
check("sim1 picks c", result1["selected_dois"] == ["10.1/c"])
check("sim1 C1 affected", result1["affected_conclusions"] == ["C1"])
check("sim1 no already-unsupported", result1["already_unsupported"] == [])

# 5. mutate marks AFTER snapshot -> result must be stale, result unchanged
db.set_mark("10.1/f", "retracted", "2026-11-25T10:05:00")
latest = db.latest_simulation()
import json  # noqa: E402
snap = json.loads(latest["snapshot"])
stale = snap["marks"] != db.current_marks()
check("stale flag after mark change", stale)
stored = json.loads(latest["result"])
check("stored result untouched", stored["selected_dois"] == ["10.1/c"])

# 6. failed simulation keeps previous result available
sim2 = db.insert_simulation("2026-11-25T10:06:00", 1,
                           {"marks": dict(db.current_marks()),
                            "dataset_uploaded_at": "2026-11-25T10:00:00",
                            "captured_at": "2026-11-25T10:06:00"})
db.fail_simulation(sim2, "2026-11-25T10:06:01", "RuntimeError: boom")
latest2 = db.latest_simulation()
check("latest row is failed", latest2["status"] == "failed")
check("failed row has no result", latest2["result"] is None)
check("failed row error kept", "boom" in latest2["error"])
# previous successful result still queryable
ok_row = db.get_db().execute(
    "SELECT * FROM simulations WHERE status='done' ORDER BY id DESC LIMIT 1"
).fetchone()
check("previous success retained", ok_row is not None and
      json.loads(ok_row["result"])["selected_dois"] == ["10.1/c"])

# 7. re-upload preserves existing marks
db.replace_dataset(parse_csv(CSV), "2026-11-25T11:00:00")
check("marks preserved across re-upload",
      db.get_mark("10.1/a") == "retracted" and db.get_mark("10.1/c") == "warning")
check("new dataset timestamp", db.dataset_meta()["uploaded_at"] == "2026-11-25T11:00:00")
# old sim now stale due to dataset timestamp change
snap2 = json.loads(db.latest_simulation()["snapshot"])
check("old sim stale after re-upload",
      snap2["dataset_uploaded_at"] != db.dataset_meta()["uploaded_at"])

# 8. unsupported detection: retract c as well -> C1 fully broken (a in G1, c in G2)
db.set_mark("10.1/c", "retracted", "2026-11-25T11:01:00")
from app.solver import build_groups, unsupported_conclusions  # noqa: E402
groups = build_groups(
    [(r["conclusion_id"], r["group_id"], r["doi"]) for r in db.all_rows()]
)
retracted = {d for d, m in db.current_marks().items() if m == "retracted"}
unsup = unsupported_conclusions(groups, retracted)
check("C1 unsupported after c retracted", "C1" in unsup, str(unsup))
check("C2 still alive (G2 d intact)", "C2" not in unsup)

# 9. unknown DOI mark rejected at DB level
try:
    db.set_mark("10.1/zzz", "warning", "x")
    check("unknown DOI raises", False)
except KeyError:
    check("unknown DOI raises", True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL INTEGRATION CHECKS PASSED")
