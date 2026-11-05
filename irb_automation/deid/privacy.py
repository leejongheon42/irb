"""재식별 위험 평가: k-익명성, l-다양성."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def equivalence_classes(rows: list[list[str]], qi_idx: list[int]) -> Counter[tuple[str, ...]]:
    return Counter(tuple(r[i] if i < len(r) else "" for i in qi_idx) for r in rows)


def k_anonymity(rows: list[list[str]], qi_idx: list[int], k_threshold: int) -> dict[str, Any]:
    if not rows or not qi_idx:
        return {
            "applicable": False,
            "min_k": None,
            "classes": 0,
            "records_below_k": 0,
            "ratio_below_k": 0.0,
            "k_threshold": k_threshold,
            "distribution": {},
        }
    classes = equivalence_classes(rows, qi_idx)
    below = sum(n for n in classes.values() if n < k_threshold)
    dist = Counter()
    for n in classes.values():
        bucket = "1" if n == 1 else "2-4" if n < 5 else "5-9" if n < 10 else "10+"
        dist[bucket] += n
    return {
        "applicable": True,
        "min_k": min(classes.values()),
        "classes": len(classes),
        "records_below_k": below,
        "ratio_below_k": round(below / len(rows), 4),
        "k_threshold": k_threshold,
        "distribution": {k: dist.get(k, 0) for k in ("1", "2-4", "5-9", "10+")},
    }


def l_diversity(rows: list[list[str]], qi_idx: list[int], sa_idx: list[int]) -> dict[str, Any]:
    if not rows or not qi_idx or not sa_idx:
        return {"applicable": False, "per_attribute": {}}
    result: dict[str, Any] = {"applicable": True, "per_attribute": {}}
    for sa in sa_idx:
        groups: dict[tuple[str, ...], set[str]] = defaultdict(set)
        for r in rows:
            key = tuple(r[i] if i < len(r) else "" for i in qi_idx)
            groups[key].add(r[sa] if sa < len(r) else "")
        result["per_attribute"][sa] = min(len(v) for v in groups.values())
    return result


def suppress_below_k(rows: list[list[str]], qi_idx: list[int], k: int) -> tuple[list[list[str]], int]:
    """k 미만 동질집합에 속한 레코드 삭제(레코드 삭제 기법)."""
    if not qi_idx:
        return rows, 0
    classes = equivalence_classes(rows, qi_idx)
    kept = [r for r in rows if classes[tuple(r[i] if i < len(r) else "" for i in qi_idx)] >= k]
    return kept, len(rows) - len(kept)
