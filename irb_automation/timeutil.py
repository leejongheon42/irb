from __future__ import annotations

from datetime import date, datetime


def now() -> datetime:
    """로컬 시각(naive). 병원 내부 시스템 특성상 서버 로컬 타임존(KST)을 기준으로 한다."""
    return datetime.now().replace(microsecond=0)


def today() -> date:
    return date.today()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None
