"""같은 서류의 두 버전 텍스트 비교."""
from __future__ import annotations

import difflib
from typing import Any

from ..models import Document
from .preview import document_text


def _lines(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        ln = " ".join(ln.split())
        if ln:
            out.append(ln)
    return out


def diff_rows(old: str, new: str, context: int = 3) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    a, b = _lines(old), _lines(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    rows: list[dict[str, Any]] = []
    added = removed = 0
    opcodes = sm.get_opcodes()
    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            n = i2 - i1
            head = context if idx > 0 else 0
            tail = context if idx < len(opcodes) - 1 else 0
            if n <= head + tail:
                for k in range(n):
                    rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1, "text": a[i1 + k]})
                continue
            for k in range(head):
                rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1, "text": a[i1 + k]})
            rows.append({"op": "skip", "a": None, "b": None, "text": f"… 변경 없는 {n - head - tail}줄 …"})
            for k in range(n - tail, n):
                rows.append({"op": "equal", "a": i1 + k + 1, "b": j1 + k + 1, "text": a[i1 + k]})
            continue
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                rows.append({"op": "delete", "a": k + 1, "b": None, "text": a[k]})
                removed += 1
        if tag in ("insert", "replace"):
            for k in range(j1, j2):
                rows.append({"op": "insert", "a": None, "b": k + 1, "text": b[k]})
                added += 1
    stats = {"added": added, "removed": removed, "similarity": round(sm.ratio() * 100, 1),
             "old_lines": len(a), "new_lines": len(b)}
    return rows, stats


def compare_documents(old: Document, new: Document) -> dict[str, Any]:
    ta, tb = document_text(old), document_text(new)
    if ta is None or tb is None:
        missing = [d for d, t in ((old, ta), (new, tb)) if t is None]
        return {"available": False, "rows": [], "stats": {},
                "reason": "텍스트를 추출할 수 없는 문서가 있습니다: "
                          + ", ".join(f"v{d.version} {d.filename}" for d in missing)}
    if old.sha256 == new.sha256:
        return {"available": True, "rows": [], "stats": {"added": 0, "removed": 0, "similarity": 100.0},
                "reason": "두 버전의 파일 내용이 동일합니다."}
    rows, stats = diff_rows(ta, tb)
    return {"available": True, "rows": rows, "stats": stats, "reason": ""}
