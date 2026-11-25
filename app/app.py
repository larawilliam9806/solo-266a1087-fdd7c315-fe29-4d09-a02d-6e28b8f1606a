"""Flask application: routes for dataset upload, paper marking and simulations."""
from __future__ import annotations

import json
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from . import db
from .csv_import import CSVError, parse_csv
from .simulator import is_running, start_simulation
from .solver import (
    MAX_WARNINGS,
    build_groups,
    unsupported_conclusions,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    db.init_db()

    # ------------------------------------------------------------- helpers --

    def _serialize_simulation(row) -> dict:
        active_dois = {r["doi"] for r in db.all_rows()}
        marks = {d: m for d, m in db.current_marks().items() if d in active_dois}
        meta = db.dataset_meta() or {}
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
        snap_marks = {d: m for d, m in (snapshot.get("marks") or {}).items()
                      if d in active_dois}
        stale = bool(snapshot) and (
            snap_marks != marks
            or snapshot.get("dataset_uploaded_at") != meta.get("uploaded_at")
        )
        return {
            "id": row["id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "k_limit": row["k_limit"],
            "snapshot_captured_at": snapshot.get("captured_at"),
            "stale": stale,
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": (row["error"] or "").splitlines()[0] if row["error"] else None,
        }

    def _latest() -> dict | None:
        row = db.latest_simulation()
        return _serialize_simulation(row) if row else None

    def _live_unsupported() -> list[str]:
        """Unsupported conclusions under the *current* marks."""
        rows = [(r["conclusion_id"], r["group_id"], r["doi"])
                for r in db.all_rows()]
        marks = db.current_marks()
        retracted = {d for d, m in marks.items() if m == "retracted"}
        return unsupported_conclusions(build_groups(rows), retracted)

    # ---------------------------------------------------------------- pages --

    @app.get("/")
    def index():
        return render_template("index.html", max_warnings=MAX_WARNINGS)

    # ---------------------------------------------------------------- state --

    @app.get("/api/state")
    def state():
        active_dois = {r["doi"] for r in db.all_rows()}
        papers = [
            {"doi": r["doi"], "mark": r["mark"], "updated_at": r["updated_at"]}
            for r in db.get_papers() if r["doi"] in active_dois
        ]
        counts = db.mark_counts()
        rows = db.all_rows()
        return jsonify({
            "dataset": db.dataset_meta(),
            "conclusion_count": len({r["conclusion_id"] for r in rows}),
            "paper_count": len(papers),
            "counts": counts,
            "max_warnings": MAX_WARNINGS,
            "warning_limit_reached": counts["warning"] >= MAX_WARNINGS,
            "running": is_running(),
            "live_unsupported": _live_unsupported() if db.dataset_loaded() else [],
            "latest_simulation": _latest(),
        })

    # --------------------------------------------------------------- upload --

    @app.post("/api/dataset")
    def upload_dataset():
        file = request.files.get("file")
        if file is None:
            return jsonify({"error": "请选择要上传的 CSV 文件"}), 400
        try:
            records = parse_csv(file.read())
        except CSVError as exc:
            return jsonify({"error": str(exc)}), 400
        db.replace_dataset(records, _now())
        active = {r["doi"] for r in db.all_rows()}
        return jsonify({
            "ok": True,
            "row_count": len(records),
            "paper_count": len(active),
        })

    # ---------------------------------------------------------------- marks --

    @app.post("/api/papers/mark")
    def mark_paper():
        payload = request.get_json(silent=True) or {}
        doi = (payload.get("doi") or "").strip().lower()
        mark = payload.get("mark")
        if mark not in ("normal", "warning", "retracted"):
            return jsonify({"error": "标记必须是 normal / warning / retracted"}), 400
        if db.get_mark(doi) is None:
            return jsonify({"error": f"未知 DOI：{doi}"}), 404

        current = db.get_mark(doi)
        if mark == "warning" and current != "warning" \
                and db.count_warnings() >= MAX_WARNINGS:
            return jsonify({
                "error": f"预警文献最多 {MAX_WARNINGS} 篇，已达上限"
            }), 409
        db.set_mark(doi, mark, _now())
        return jsonify({"ok": True, "doi": doi, "mark": mark,
                        "warning_count": db.count_warnings()})

    # ----------------------------------------------------------- simulations --

    @app.post("/api/simulate")
    def simulate():
        if not db.dataset_loaded():
            return jsonify({"error": "请先上传 CSV 数据"}), 400
        if is_running():
            return jsonify({"error": "已有推演正在运行，请等待完成"}), 409
        payload = request.get_json(silent=True) or {}
        try:
            k = int(payload.get("k"))
        except (TypeError, ValueError):
            return jsonify({"error": "新增撤稿篇数上限必须是整数"}), 400
        if k < 0:
            return jsonify({"error": "新增撤稿篇数上限不能为负数"}), 400
        if k > MAX_WARNINGS:
            return jsonify({
                "error": f"上限不能超过预警文献数上限 {MAX_WARNINGS}"
            }), 400
        sim_id = start_simulation(k)
        return jsonify({"ok": True, "simulation_id": sim_id})

    @app.get("/api/simulations/latest")
    def latest_simulation():
        payload = _latest()
        if payload is None:
            return jsonify({"error": "暂无推演结果"}), 404
        return jsonify(payload)

    return app
