"""분석 결과 반출 심사(airlock / output checking).

안심활용센터·분석전용 서버(TRE)에서 연구자가 반출을 요청한 결과표를 자동 점검한다.
- 소규모 셀: 최소 셀 크기 미만(기본 5) 집계값 탐지 → 자동 억제본 생성
- 개인 단위 자료 의심: 행 수 과다, 식별자형 컬럼, 고유값 비율
- 직접 식별자 탐지: 가명처리 탐지기 재사용
최종 승인은 데이터관리자가 한다(원본 승인 / 억제본 승인 / 반려).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..deid.detector import analyze_table
from ..deid.pipeline import read_csv_bytes, write_csv
from ..engine.ruleset import get_ruleset
from ..models import OutputRequest, Protocol, Role, User
from ..notifications import notify
from ..timeutil import now
from ..workflow import service as wf

_INT = re.compile(r"^\s*-?\d+(\.0+)?\s*$")


def _policy() -> dict[str, Any]:
    return {"min_cell": 5, "allow_zero": True, "max_rows_aggregate": 1000, "suppression_label": "<5",
            **get_ruleset().data.get("output_check", {})}


def check_table(headers: list[str], rows: list[list[str]], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or _policy()
    k = int(policy["min_cell"])
    allow_zero = bool(policy.get("allow_zero", True))
    issues: list[str] = []
    warnings: list[str] = []

    # 1) 직접 식별자 / 개인 단위 자료
    analysis = analyze_table(headers, rows[:2000])
    # 고유값 비율만으로 추정한 'unknown' 식별자는 집계표의 범주 라벨에서 오탐이 많아 제외
    direct = [c["name"] for c in analysis["columns"] if c["category"] == "direct" and c["detected_type"] != "unknown"]
    if direct:
        issues.append("직접 식별자(또는 식별자형) 컬럼: " + ", ".join(direct))
    free = [c["name"] for c in analysis["columns"] if c["category"] == "free_text"]
    if free:
        issues.append("자유기술 컬럼은 반출할 수 없습니다: " + ", ".join(free))
    if len(rows) > int(policy["max_rows_aggregate"]):
        issues.append(f"행 수 {len(rows):,}건 - 집계표가 아닌 개인 단위 자료로 의심됩니다.")

    # 2) 집계(정수) 컬럼의 소규모 셀
    count_cols = []
    for idx in range(len(headers)):
        vals = [r[idx] for r in rows if idx < len(r) and r[idx].strip()]
        if vals and all(_INT.match(v) for v in vals):
            count_cols.append(idx)
    small: list[dict[str, Any]] = []
    for r_i, row in enumerate(rows):
        for c in count_cols:
            if c >= len(row) or not row[c].strip():
                continue
            v = int(float(row[c]))
            if (v == 0 and not allow_zero) or (0 < v < k):
                small.append({"row": r_i + 2, "column": headers[c], "value": v})
    if small:
        issues.append(f"최소 셀 크기({k}) 미만 집계값 {len(small)}개")
    if count_cols and small and len(small) == 1:
        warnings.append("셀 1개만 억제하면 행·열 합계로 역산될 수 있습니다. 합계 행/열도 함께 검토하십시오(보완 억제).")
    totals = [headers[c] for c in count_cols if re.search(r"합계|총|total|sum", headers[c], re.I)]
    if small and totals:
        warnings.append("합계 컬럼이 있어 억제된 셀이 차감으로 노출될 수 있습니다: " + ", ".join(totals))
    if not count_cols:
        warnings.append("정수 집계 컬럼이 없습니다. 통계 모형 결과(계수·p값 등)라면 표본 수 표기를 확인하십시오.")

    verdict = "fail" if issues else ("warn" if warnings else "pass")
    return {
        "verdict": verdict,
        "issues": issues,
        "warnings": warnings,
        "small_cells": small[:200],
        "small_cell_count": len(small),
        "count_columns": [headers[c] for c in count_cols],
        "rows": len(rows),
        "columns": len(headers),
        "min_cell": k,
        "suppressible": bool(small) and not direct and not free and len(rows) <= int(policy["max_rows_aggregate"]),
    }


def suppress(headers: list[str], rows: list[list[str]], check: dict[str, Any], label: str) -> list[list[str]]:
    cols = {h: i for i, h in enumerate(headers)}
    out = [list(r) for r in rows]
    for cell in check["small_cells"]:
        out[cell["row"] - 2][cols[cell["column"]]] = label
    return out


def submit_output(
    db: Session, user: User, protocol: Protocol, filename: str, raw: bytes, description: str, ip: str = ""
) -> OutputRequest:
    wf.require_member(user, protocol)
    if not protocol.is_active_approval:
        raise wf.WorkflowError("유효한 IRB 승인 기간에만 반출을 요청할 수 있습니다.")
    try:
        headers, rows, _ = read_csv_bytes(raw)
    except ValueError as e:
        raise wf.WorkflowError(f"CSV를 읽을 수 없습니다: {e}") from e
    check = check_table(headers, rows)
    settings = get_settings()
    folder = settings.data_dir / "outputs" / str(protocol.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.csv"
    path.write_bytes(raw)
    req = OutputRequest(
        protocol_id=protocol.id, requested_by=user.id, filename=Path(filename).name, stored_path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(), description=description.strip(), check=check,
    )
    if check["suppressible"]:
        sp = folder / f"{uuid.uuid4().hex}_suppressed.csv"
        sp.write_bytes(write_csv(headers, suppress(headers, rows, check, str(_policy()["suppression_label"]))))
        req.suppressed_path = str(sp)
    db.add(req)
    db.flush()
    audit.record(db, user, "output_submit", "output", req.id,
                 {"protocol": protocol.protocol_no, "verdict": check["verdict"], "sha256": req.sha256}, ip)
    from sqlalchemy import select

    stewards = db.scalars(select(User).where(User.role == Role.DATA_STEWARD, User.is_active.is_(True))).all()
    notify(db, stewards, f"반출 심사 요청: {protocol.protocol_no} {req.filename} (자동점검 {check['verdict']})",
           f"/outputs/{req.id}")
    return req


def review_output(db: Session, user: User, req: OutputRequest, action: str, comment: str = "", ip: str = "") -> None:
    if user.role not in (Role.DATA_STEWARD, Role.ADMIN):
        raise wf.PermissionDenied("데이터관리자만 반출을 승인할 수 있습니다.")
    if req.status != "submitted":
        raise wf.WorkflowError("이미 처리된 요청입니다.")
    if action == "approve":
        if req.check.get("issues"):
            raise wf.WorkflowError("자동점검 지적 사항이 있는 원본은 승인할 수 없습니다. 억제본 승인 또는 반려하십시오.")
        req.status = "approved"
    elif action == "approve_suppressed":
        if not req.suppressed_path:
            raise wf.WorkflowError("자동 억제본이 없습니다(식별자·자유기술·개인 단위 자료는 억제로 해결되지 않음).")
        other = [i for i in req.check.get("issues", []) if "최소 셀" not in i]
        if other:
            raise wf.WorkflowError("소규모 셀 외 지적 사항이 있어 억제본도 승인할 수 없습니다.")
        req.status = "approved_suppressed"
    elif action == "reject":
        if not comment.strip():
            raise wf.WorkflowError("반려 사유를 입력하십시오.")
        req.status = "rejected"
    else:
        raise wf.WorkflowError("알 수 없는 처리입니다.")
    req.reviewer_id = user.id
    req.reviewed_at = now()
    req.review_comment = comment.strip()
    audit.record(db, user, "output_review", "output", req.id, {"status": req.status}, ip)
    notify(db, req.protocol.member_ids(), f"반출 심사 결과: {req.filename} - {STATUS_LABELS[req.status]}",
           f"/outputs/{req.id}")


STATUS_LABELS = {
    "submitted": "심사중",
    "approved": "승인",
    "approved_suppressed": "억제본 승인",
    "rejected": "반려",
}


def downloadable_path(req: OutputRequest) -> str | None:
    if req.status == "approved":
        return req.stored_path
    if req.status == "approved_suppressed":
        return req.suppressed_path
    return None
