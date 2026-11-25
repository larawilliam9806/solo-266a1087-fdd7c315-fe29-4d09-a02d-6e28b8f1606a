"""SQLite persistence layer.

One connection per thread (check_same_thread=False + thread-local storage).
The app runs under a single gunicorn worker with multiple threads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "app.db")

_lock = threading.RLock()
_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS dataset (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    uploaded_at TEXT NOT NULL,
    row_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL DEFAULT 1,
    conclusion_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    doi TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rows_conclusion ON rows(conclusion_id);
CREATE INDEX IF NOT EXISTS idx_rows_doi ON rows(doi);

CREATE TABLE IF NOT EXISTS papers (
    doi TEXT PRIMARY KEY,
    mark TEXT NOT NULL DEFAULT 'normal' CHECK (mark IN ('normal', 'warning', 'retracted')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    k_limit INTEGER,
    snapshot TEXT,
    result TEXT,
    error TEXT
);
"""


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    with _lock:
        conn = get_db()
        conn.executescript(SCHEMA)
        conn.commit()


# ---------------------------------------------------------------- dataset --

def dataset_loaded() -> bool:
    row = get_db().execute("SELECT 1 FROM dataset WHERE id = 1").fetchone()
    return row is not None


def dataset_meta() -> dict | None:
    row = get_db().execute(
        "SELECT uploaded_at, row_count FROM dataset WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {"uploaded_at": row["uploaded_at"], "row_count": row["row_count"]}


def replace_dataset(records: list[tuple[str, str, str]], uploaded_at: str) -> None:
    """Replace the whole CSV dataset inside one transaction.

    Paper marks are preserved per DOI; newly seen DOIs default to normal.
    """
    with _lock:
        conn = get_db()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM rows")
            conn.execute("DELETE FROM dataset")
            conn.execute(
                "INSERT INTO dataset (id, uploaded_at, row_count) VALUES (1, ?, ?)",
                (uploaded_at, len(records)),
            )
            conn.executemany(
                "INSERT INTO rows (dataset_id, conclusion_id, group_id, doi) "
                "VALUES (1, ?, ?, ?)",
                records,
            )
            known = {r["doi"] for r in conn.execute("SELECT doi FROM papers")}
            new_dois = sorted({r[2] for r in records} - known)
            conn.executemany(
                "INSERT OR IGNORE INTO papers (doi, mark, updated_at) VALUES (?, 'normal', ?)",
                [(d, uploaded_at) for d in new_dois],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def all_rows() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT conclusion_id, group_id, doi FROM rows ORDER BY id"
    ).fetchall()


# ----------------------------------------------------------------- papers --

def get_papers() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT doi, mark, updated_at FROM papers ORDER BY doi"
    ).fetchall()


def get_mark(doi: str) -> str | None:
    row = get_db().execute("SELECT mark FROM papers WHERE doi = ?", (doi,)).fetchone()
    return row["mark"] if row else None


def count_warnings() -> int:
    """Warnings only count against the cap when referenced by the dataset."""
    return mark_counts().get("warning", 0)


def mark_counts() -> dict[str, int]:
    """Per-mark counts limited to DOIs referenced by the current dataset."""
    rows = get_db().execute(
        "SELECT p.mark AS mark, COUNT(*) AS n FROM papers p "
        "WHERE EXISTS (SELECT 1 FROM rows r WHERE r.doi = p.doi) "
        "GROUP BY p.mark"
    ).fetchall()
    counts = {"normal": 0, "warning": 0, "retracted": 0}
    for r in rows:
        counts[r["mark"]] = r["n"]
    return counts


def set_mark(doi: str, mark: str, updated_at: str) -> None:
    with _lock:
        conn = get_db()
        cur = conn.execute(
            "UPDATE papers SET mark = ?, updated_at = ? WHERE doi = ?",
            (mark, updated_at, doi),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise KeyError(doi)


def current_marks() -> dict[str, str]:
    return {r["doi"]: r["mark"] for r in get_db().execute("SELECT doi, mark FROM papers")}


# ------------------------------------------------------------ simulations --

def insert_simulation(started_at: str, k_limit: int, snapshot: dict) -> int:
    with _lock:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO simulations (started_at, status, k_limit, snapshot) "
            "VALUES (?, 'running', ?, ?)",
            (started_at, k_limit, json.dumps(snapshot, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid


def finish_simulation(sim_id: int, finished_at: str, result: dict) -> None:
    with _lock:
        conn = get_db()
        conn.execute(
            "UPDATE simulations SET status='done', finished_at=?, result=? WHERE id=?",
            (finished_at, json.dumps(result, ensure_ascii=False), sim_id),
        )
        conn.commit()


def fail_simulation(sim_id: int, finished_at: str, error: str) -> None:
    with _lock:
        conn = get_db()
        conn.execute(
            "UPDATE simulations SET status='failed', finished_at=?, error=? WHERE id=?",
            (finished_at, error, sim_id),
        )
        conn.commit()


def latest_simulation() -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM simulations ORDER BY id DESC LIMIT 1"
    ).fetchone()
