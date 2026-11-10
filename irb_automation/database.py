from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db() -> None:
    from . import models  # noqa: F401  (모델 등록)
    from .demo import models as _demo_models  # noqa: F401
    from .doctemplates import models as _doctemplates_models  # noqa: F401
    from .forms import models as _forms_models  # noqa: F401
    from .knowledge import models as _knowledge_models  # noqa: F401
    from .pipeline import models as _pipeline_models  # noqa: F401
    from .qc import models as _qc_models  # noqa: F401
    from .submission import models as _submission_models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(engine)
    added = migrate_columns(engine)
    if added:
        log.info("added columns: %s", ", ".join(added))


def migrate_columns(engine: Engine) -> list[str]:
    """기존 DB에 없는 컬럼을 추가한다(업그레이드 시 경량 마이그레이션).

    새 테이블은 create_all이 만들고, 기존 테이블에 추가된 컬럼만 ALTER TABLE로 보충한다.
    컬럼 삭제·타입 변경은 지원하지 않으므로 그런 변경은 별도 마이그레이션 도구를 사용한다.
    """
    insp = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
                default = _python_default(col)
                if default is not None:
                    conn.execute(text(f'UPDATE "{table.name}" SET "{col.name}" = :v'), {"v": default})
                added.append(f"{table.name}.{col.name}")
    return added


def _python_default(col) -> object | None:
    d = col.default
    if d is None:
        return None
    if getattr(d, "is_scalar", False):
        value = d.arg
        return value.value if hasattr(value, "value") else value
    if getattr(d, "is_callable", False):
        try:
            value = d.arg(None)
        except TypeError:
            return None
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value
    return None


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
