"""Citation-support analysis and the retraction scenario optimizer.

Domain rules
------------
Each review conclusion cites one or more *citation groups*. A conclusion stays
supported as long as **at least one** group is complete (contains no retracted
paper). A group is broken as soon as any of its DOIs is retracted.

* A conclusion is *unsupported* when every one of its groups already contains
  a retracted paper.
* A currently supported conclusion becomes *killable* when every still-intact
  group contains at least one warning paper: retracting a suitable subset of
  warning papers can then break all remaining groups.
* The optimizer chooses at most ``k`` warning DOIs whose (simulated)
  retraction maximizes the number of additionally unsupported conclusions.

There are at most 20 warning papers, so exact enumeration over warning subsets
(bounded by C(20, 10) = 184 756 iterations) is feasible.  Conditions are
encoded as integer bitmasks so each inner evaluation is cheap.
"""
from __future__ import annotations

import re
from itertools import combinations

MAX_WARNINGS = 20

RETRACTED = "retracted"
WARNING = "warning"
NORMAL = "normal"


def natural_key(value: str):
    """Sort key that keeps numeric suffixes in numeric order (C2 < C10)."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", str(value))]


def build_groups(rows) -> dict[str, list[set[str]]]:
    """rows: iterable of (conclusion_id, group_id, doi) -> groups per conclusion."""
    grouped: dict[str, dict[str, set[str]]] = {}
    for conclusion_id, group_id, doi in rows:
        grouped.setdefault(conclusion_id, {}).setdefault(group_id, set()).add(doi)
    return {c: [g for _, g in sorted(grp.items())] for c, grp in grouped.items()}


def unsupported_conclusions(
    groups: dict[str, list[set[str]]], retracted: set[str]
) -> list[str]:
    """Conclusions whose every citation group contains a retracted DOI."""
    out = []
    for cid in sorted(groups, key=natural_key):
        if groups[cid] and all(g & retracted for g in groups[cid]):
            out.append(cid)
    return out


def analyze(
    rows,
    marks: dict[str, str],
    k: int,
    progress=None,
) -> dict:
    """Run the full analysis against a fixed mark snapshot.

    Returns a JSON-serializable dict with:
      already_unsupported, killable_conclusions, selected_dois,
      affected_conclusions, affected_count, k_requested, k_used,
      warning_total, warning_dois, evaluated_combinations.
    """
    if not isinstance(k, int) or k < 0:
        raise ValueError("k 必须是非负整数")

    groups = build_groups(rows)
    retracted = {d for d, m in marks.items() if m == RETRACTED}
    warnings = sorted((d for d, m in marks.items() if m == WARNING),
                      key=natural_key)
    warn_index = {d: i for i, d in enumerate(warnings)}

    already = unsupported_conclusions(groups, retracted)
    already_set = set(already)

    # Killable conclusions: currently supported, and every intact group
    # contains at least one warning DOI.  Each condition is a tuple of integer
    # bitmasks (one per intact group); identical conditions are merged and
    # carry the combined weight of their conclusions.
    condition_to_cids: dict[tuple[int, ...], list[str]] = {}
    for cid in groups:
        if cid in already_set:
            continue
        masks = []
        killable = True
        for g in groups[cid]:
            if g & retracted:
                continue  # already-broken group imposes no new condition
            mask = 0
            for d in g:
                idx = warn_index.get(d)
                if idx is not None:
                    mask |= 1 << idx
            if not mask:
                killable = False  # intact group without a warning cannot break
                break
            masks.append(mask)
        if killable and masks:
            condition_to_cids.setdefault(tuple(masks), []).append(cid)

    # Deterministic order for tie resolution.
    conditions = sorted(condition_to_cids,
                        key=lambda t: natural_key(min(condition_to_cids[t])))
    weights = [len(condition_to_cids[c]) for c in conditions]

    # Only warnings referenced by a killable condition can be selected.
    used_bits = 0
    for cond in conditions:
        for mask in cond:
            used_bits |= mask
    used_idx = [i for i in range(len(warnings)) if used_bits & (1 << i)]
    effective_w = len(used_idx)
    r = min(k, effective_w)

    def hits(sel_mask: int) -> int:
        total = 0
        for cond, weight in zip(conditions, weights):
            if all(sel_mask & mask for mask in cond):
                total += weight
        return total

    evaluated = 0
    best_count = 0
    best_mask = 0
    best_combo: tuple[int, ...] = ()

    if conditions and r > 0:
        # Greedy baseline; also guarantees a sane result before enumeration.
        cur_mask = 0
        remaining = list(used_idx)
        for _ in range(r):
            base = hits(cur_mask)
            pick, pick_gain = None, -1
            for i in remaining:
                gain = hits(cur_mask | (1 << i)) - base
                if gain > pick_gain:
                    pick, pick_gain = i, gain
            if pick is None or pick_gain <= 0:
                break
            cur_mask |= 1 << pick
            remaining.remove(pick)
        best_count, best_mask = hits(cur_mask), cur_mask
        best_combo = tuple(i for i in used_idx if cur_mask & (1 << i))

        # Exact enumeration at the effective size r: hitting-set counts are
        # monotone in subset size, so an optimum of size <= r is matched or
        # beaten at exactly r.  combinations() emits lexicographic order; on
        # equal counts we keep the lexicographically smallest index tuple
        # (== smallest DOI tuple because warnings are sorted), so ties are
        # resolved deterministically.
        for combo in combinations(used_idx, r):
            sel_mask = 0
            for i in combo:
                sel_mask |= 1 << i
            evaluated += 1
            if progress and evaluated % 10000 == 0:
                progress(evaluated)
            n = hits(sel_mask)
            if n > best_count or (n > 0 and n == best_count and
                                  (not best_combo or combo < best_combo)):
                best_count, best_mask, best_combo = n, sel_mask, combo

    # Expand the winning condition set back to affected conclusion ids.
    affected = set()
    for cond, cids in zip(conditions, condition_to_cids.values()):
        if all(best_mask & mask for mask in cond):
            affected.update(cids)

    selected_dois = [warnings[i] for i in used_idx if best_mask & (1 << i)]
    return {
        "already_unsupported": already,
        "killable_conclusions": sorted(
            (cid for cids in condition_to_cids.values() for cid in cids),
            key=natural_key),
        "selected_dois": selected_dois,
        "affected_conclusions": sorted(affected, key=natural_key),
        "affected_count": len(affected),
        "k_requested": k,
        "k_used": r,
        "warning_total": len(warnings),
        "warning_dois": warnings,
        "evaluated_combinations": evaluated,
    }
