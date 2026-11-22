from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator

from sqlalchemy import String, Text, create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class SchemaRevision(Base):
    __tablename__ = "schema_meta"

    revision: Mapped[str] = mapped_column(String(80), primary_key=True)


class AppSecret(Base):
    """환경변수로 주지 않은 비밀값(세션 서명 키, 가명처리 키)을 재배포 후에도 유지하기 위한 보관소."""

    __tablename__ = "app_secrets"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # 서버리스 PostgreSQL(Neon·Vercel Postgres 등)은 유휴 연결을 끊으므로 사용 전 확인·주기적 재연결
        kwargs.update(pool_pre_ping=True, pool_recycle=280,
                      pool_size=int(os.environ.get("IRB_DB_POOL_SIZE", "5")),
                      max_overflow=int(os.environ.get("IRB_DB_MAX_OVERFLOW", "5")))
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
    from . import filestore as _filestore  # noqa: F401
    from . import models  # noqa: F401  (모델 등록)
    from .demo import models as _demo_models  # noqa: F401
    from .doctemplates import models as _doctemplates_models  # noqa: F401
    from .forms import models as _forms_models  # noqa: F401
    from .knowledge import models as _knowledge_models  # noqa: F401
    from .mvp import models as _mvp_models  # noqa: F401
    from .pipeline import models as _pipeline_models  # noqa: F401
    from .public_irb import checklist_models as _checklist_models  # noqa: F401
    from .public_irb import records as _public_records  # noqa: F401
    from .public_irb import source_models as _source_models  # noqa: F401
    from .qc import models as _qc_models  # noqa: F401
    from .qc.pattern_store import seed_patterns
    from .submission import models as _submission_models  # noqa: F401

    engine = get_engine()
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Cold starts must not create the same new tables concurrently.
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x1EB_DD01})
        Base.metadata.create_all(conn)
    added = migrate_columns(engine)
    if added:
        log.info("added columns: %s", ", ".join(added))
    with Session(engine) as db:
        if db.get(SchemaRevision, "public-irb-v1") is None:
            db.add(SchemaRevision(revision="public-irb-v1"))
            db.commit()
        seed_patterns(db)
        db.commit()


def sync_secrets(settings) -> None:
    """PostgreSQL 사용 시, 이 프로세스에서 생성한 비밀값을 DB에 보관된 값으로 바꾼다(없으면 저장).

    재배포마다 세션 키가 바뀌어 로그인이 풀리거나 가명처리 키가 바뀌어 가명 ID가 달라지는 것을 막는다.
    """
    names = [name for name in ("secret_key", "pseudonym_secret") if name in settings.generated_secrets]
    if settings.is_sqlite or not names:
        return
    engine = get_engine()
    AppSecret.__table__.create(engine, checkfirst=True)
    for name in names:
        with Session(engine) as db:
            stored = db.scalar(select(AppSecret.value).where(AppSecret.name == name))
            if stored is None:
                db.add(AppSecret(name=name, value=getattr(settings, name)))
                try:
                    db.commit()
                    continue
                except IntegrityError:  # 다른 워커가 먼저 저장함
                    db.rollback()
                    stored = db.scalar(select(AppSecret.value).where(AppSecret.name == name))
            setattr(settings, name, stored)
        settings.generated_secrets.discard(name)


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
