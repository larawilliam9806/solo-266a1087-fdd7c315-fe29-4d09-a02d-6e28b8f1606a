"""Background simulation runner.

A simulation runs against a snapshot of the paper marks taken at start time.
Marks changed while the simulation runs neither affect it nor block editing:
the finished result simply becomes "stale".  A failed simulation never
overwrites the last successful result — it is stored as its own failed row.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime

from . import db
from .solver import analyze

_running_lock = threading.Lock()
_running = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_running() -> bool:
    return _running


def start_simulation(k: int) -> int:
    """Kick off a simulation thread. Raises RuntimeError if one is running."""
    global _running
    with _running_lock:
        if _running:
            raise RuntimeError("已有推演正在运行，请等待完成")
        marks = db.current_marks()
        snapshot = {
            "marks": dict(marks),
            "dataset_uploaded_at": (db.dataset_meta() or {}).get("uploaded_at"),
            "captured_at": _now(),
        }
        sim_id = db.insert_simulation(_now(), k, snapshot)
        _running = True

    thread = threading.Thread(
        target=_run,
        args=(sim_id, k,
              [(r["conclusion_id"], r["group_id"], r["doi"])
               for r in db.all_rows()],
              snapshot["marks"]),
        name=f"sim-{sim_id}",
        daemon=True,
    )
    thread.start()
    return sim_id


def _run(sim_id: int, k: int, rows, marks: dict) -> None:
    global _running
    try:
        result = analyze(rows, marks, k)
        db.finish_simulation(sim_id, _now(), result)
    except Exception as exc:  # noqa: BLE001 - recorded, never kills the thread
        db.fail_simulation(
            sim_id, _now(),
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
    finally:
        with _running_lock:
            _running = False
