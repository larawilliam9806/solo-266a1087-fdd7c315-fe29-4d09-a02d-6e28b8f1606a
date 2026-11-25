"""CSV upload parsing and validation.

Required columns (Chinese or English headers accepted):
  结论号 / conclusion_id, 引文组号 / group_id, DOI / doi
"""
from __future__ import annotations

import csv
import io

HEADER_ALIASES = {
    "conclusion_id": {"结论号", "结论编号", "结论id", "conclusion_id",
                      "conclusion", "conclusionid", "cid"},
    "group_id": {"引文组号", "引文组编号", "引文组id", "组号", "group_id",
                 "group", "groupid", "gid"},
    "doi": {"doi", "DOI", "文献doi", "文献DOI"},
}


class CSVError(ValueError):
    pass


def _match_header(name: str) -> str | None:
    key = (name or "").strip().lower()
    for canonical, aliases in HEADER_ALIASES.items():
        if key in {a.lower() for a in aliases}:
            return canonical
    return None


def parse_csv(raw: bytes) -> list[tuple[str, str, str]]:
    """Parse uploaded bytes into normalized (conclusion_id, group_id, doi) rows."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise CSVError("文件编码无法识别，请使用 UTF-8 编码的 CSV") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CSVError("CSV 文件为空")

    mapping: dict[int, str] = {}
    for idx, col in enumerate(header):
        canonical = _match_header(col)
        if canonical:
            mapping[idx] = canonical
    if set(mapping.values()) != {"conclusion_id", "group_id", "doi"}:
        raise CSVError(
            "缺少必需列，表头需包含：结论号、引文组号、DOI（也支持英文名）"
        )
    pos = {v: k for k, v in mapping.items()}

    records: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if max(pos.values()) >= len(row):
            raise CSVError(f"第 {line_no} 行列数不足")
        cid = row[pos["conclusion_id"]].strip()
        gid = row[pos["group_id"]].strip()
        doi = row[pos["doi"]].strip().lower()
        if not cid or not gid or not doi:
            raise CSVError(f"第 {line_no} 行存在空的结论号、引文组号或 DOI")
        item = (cid, gid, doi)
        if item not in seen:
            seen.add(item)
            records.append(item)

    if not records:
        raise CSVError("CSV 中没有有效数据行")
    return records
