"""IRB 승인 전 대상자 수 사전 조회(feasibility).

연구자가 연구 설계 단계에서 대상자 규모를 가늠할 수 있도록 집계 건수만 제공한다.
- 소규모 결과는 "<최소셀"로 억제, 그 외는 반올림하여 표시(정확한 건수 노출 방지)
- 사용자별 일일 조회 한도와 전체 조회 기록으로 차분(differencing) 공격을 완화
- 커넥터 교체로 병원 CDW(OMOP CDM 등)에 연결: IRB_CDW_URL
  · 미설정 시 합성 데이터 CSV(IRB_CDW_DEMO_CSV, 기본 samples/synthetic_patients.csv) 사용
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from . import audit
from .engine.ruleset import get_ruleset
from .models import FeasibilityQuery, User
from .timeutil import now

OMOP_MALE, OMOP_FEMALE = 8507, 8532


class FeasibilityError(Exception):
    pass


@dataclass
class Criteria:
    dx_codes: list[str] = field(default_factory=list)  # KCD/ICD 코드 접두어 (OR)
    sex: str = ""  # M | F | ""
    age_min: int | None = None
    age_max: int | None = None
    date_from: date | None = None
    date_to: date | None = None

    def validate(self) -> None:
        if not self.dx_codes:
            raise FeasibilityError("진단코드를 1개 이상 입력하십시오.")
        if len(self.dx_codes) > 20:
            raise FeasibilityError("진단코드는 20개까지 입력할 수 있습니다.")
        for c in self.dx_codes:
            if not c.replace(".", "").isalnum() or len(c) > 10:
                raise FeasibilityError(f"잘못된 진단코드: {c}")
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            raise FeasibilityError("연령 범위가 올바르지 않습니다.")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise FeasibilityError("기간이 올바르지 않습니다.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dx_codes": self.dx_codes, "sex": self.sex, "age_min": self.age_min, "age_max": self.age_max,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
        }


def _age(birth: date, ref: date) -> int:
    return ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))


class DemoCsvConnector:
    """합성 데이터 CSV 커넥터(데모·교육용). 컬럼: 환자번호, 생년월일, 성별, 입원일자, 진단코드."""

    name = "합성 데이터(데모)"

    def __init__(self, path: Path):
        self.path = path

    def count(self, c: Criteria) -> int:
        if not self.path.exists():
            raise FeasibilityError(f"데모 데이터 파일이 없습니다: {self.path}")
        patients: set[str] = set()
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = row.get("진단코드", "")
                if not any(code.upper().startswith(x.upper()) for x in c.dx_codes):
                    continue
                if c.sex and row.get("성별", "") != c.sex:
                    continue
                try:
                    event = date.fromisoformat(row.get("입원일자", ""))
                    birth = date.fromisoformat(row.get("생년월일", ""))
                except ValueError:
                    continue
                if c.date_from and event < c.date_from or c.date_to and event > c.date_to:
                    continue
                age = _age(birth, event)
                if c.age_min is not None and age < c.age_min or c.age_max is not None and age > c.age_max:
                    continue
                patients.add(row.get("환자번호", ""))
        return len(patients)


class OmopConnector:
    """OMOP CDM 커넥터. person / condition_occurrence 테이블과 condition_source_value(KCD 코드)를 사용한다."""

    name = "OMOP CDM"

    def __init__(self, url: str, schema: str = ""):
        self.engine = create_engine(url)
        self.prefix = f"{schema}." if schema else ""

    def count(self, c: Criteria) -> int:
        where = ["(" + " OR ".join(f"co.condition_source_value LIKE :dx{i}" for i in range(len(c.dx_codes))) + ")"]
        params: dict[str, Any] = {f"dx{i}": f"{code}%" for i, code in enumerate(c.dx_codes)}
        if c.sex:
            where.append("p.gender_concept_id = :sex")
            params["sex"] = OMOP_MALE if c.sex == "M" else OMOP_FEMALE
        if c.date_from:
            where.append("co.condition_start_date >= :dfrom")
            params["dfrom"] = c.date_from
        if c.date_to:
            where.append("co.condition_start_date <= :dto")
            params["dto"] = c.date_to
        # 연령은 진단 연도 기준(연 단위) - 표준 SQL 호환을 위해 연도 차이로 계산
        year_expr = "CAST(SUBSTR(CAST(co.condition_start_date AS VARCHAR(10)), 1, 4) AS INTEGER)"
        if c.age_min is not None:
            where.append(f"({year_expr} - p.year_of_birth) >= :amin")
            params["amin"] = c.age_min
        if c.age_max is not None:
            where.append(f"({year_expr} - p.year_of_birth) <= :amax")
            params["amax"] = c.age_max
        sql = (
            f"SELECT COUNT(DISTINCT p.person_id) FROM {self.prefix}person p "
            f"JOIN {self.prefix}condition_occurrence co ON co.person_id = p.person_id "
            f"WHERE {' AND '.join(where)}"
        )
        with self.engine.connect() as conn:
            return int(conn.execute(text(sql), params).scalar() or 0)


def get_connector():
    url = os.environ.get("IRB_CDW_URL")
    if url:
        return OmopConnector(url, os.environ.get("IRB_CDW_SCHEMA", ""))
    default = Path(__file__).resolve().parent.parent / "samples" / "synthetic_patients.csv"
    return DemoCsvConnector(Path(os.environ.get("IRB_CDW_DEMO_CSV", str(default))))


def display_count(n: int, policy: dict[str, Any]) -> str:
    min_cell = int(policy.get("min_cell", 10))
    rounding = int(policy.get("rounding", 10))
    if n < min_cell:
        return f"<{min_cell}"
    if rounding > 1:
        return f"약 {int(round(n / rounding) * rounding):,}"
    return f"{n:,}"


def run_query(db: Session, user: User, criteria: Criteria, connector=None, ip: str = "") -> dict[str, Any]:
    criteria.validate()
    policy = {"min_cell": 10, "rounding": 10, "daily_limit": 50, **get_ruleset().data.get("feasibility_policy", {})}
    start = datetime.combine(date.today(), time.min)
    used = db.scalar(
        select(func.count(FeasibilityQuery.id)).where(FeasibilityQuery.user_id == user.id, FeasibilityQuery.created_at >= start)
    ) or 0
    if used >= int(policy["daily_limit"]):
        raise FeasibilityError(f"일일 조회 한도({policy['daily_limit']}회)를 초과했습니다.")
    connector = connector or get_connector()
    shown = display_count(connector.count(criteria), policy)
    q = FeasibilityQuery(user_id=user.id, criteria=criteria.to_dict(), result_display=shown, created_at=now())
    db.add(q)
    db.flush()
    audit.record(db, user, "feasibility_query", "feasibility", q.id, {"criteria": criteria.to_dict(), "result": shown}, ip)
    return {"display": shown, "source": connector.name, "remaining": int(policy["daily_limit"]) - used - 1,
            "policy": policy}
