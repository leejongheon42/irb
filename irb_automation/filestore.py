"""업로드 원본을 DB에도 보관해 재배포·컨테이너 교체 후에도 파일이 남도록 한다.

Vercel 같은 서버리스·컨테이너 환경은 배포할 때마다 로컬 디스크가 초기화된다. 파일 경로는 그대로
DB(JSON 등)에 기록해 두고, 실제 바이트는 ``stored_files`` 테이블에 같은 키로 저장한다.
읽을 때 로컬 파일이 없으면 DB에서 꺼내 디스크에 다시 써 두므로(캐시) 이후 호출은 디스크를 쓴다.

``IRB_FILE_STORE=db``이면 항상, ``disk``이면 사용하지 않고, 지정하지 않으면 SQLite가 아닌
DB(PostgreSQL)를 쓸 때 자동으로 켠다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, Integer, LargeBinary, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import get_settings
from .database import Base, SessionLocal
from .timeutil import now

log = logging.getLogger(__name__)


class StoredFile(Base):
    __tablename__ = "stored_files"

    key: Mapped[str] = mapped_column(String(1024), primary_key=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def enabled() -> bool:
    settings = get_settings()
    mode = (os.environ.get("IRB_FILE_STORE") or "").strip().lower()
    if mode in ("db", "disk"):
        return mode == "db"
    return not settings.database_url.startswith("sqlite")


def key_for(path: str | Path) -> str:
    """데이터 디렉터리 기준 상대 경로(다른 위치의 파일은 절대 경로)."""
    path = Path(path)
    try:
        return path.resolve().relative_to(get_settings().data_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_disk(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError:
        log.warning("Could not cache %s on local disk", path, exc_info=True)


def write(path: str | Path, data: bytes, db: Session | None = None) -> None:
    """로컬 디스크에 쓰고, 영속 저장소가 켜져 있으면 DB에도 저장한다.

    ``db``를 넘기면 호출자의 트랜잭션에 포함되고(커밋은 호출자), 없으면 별도 세션으로 즉시 커밋한다.
    """
    path = Path(path)
    _write_disk(path, data)
    if not enabled():
        return
    row = StoredFile(key=key_for(path), size=len(data), content=data)
    if db is not None:
        db.merge(row)
        return
    with SessionLocal() as session:
        session.merge(row)
        session.commit()


def read_bytes(path: str | Path) -> bytes:
    """로컬 파일을 읽고, 없으면 DB에서 복원한다. 어디에도 없으면 FileNotFoundError."""
    path = Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if not enabled():
            raise
    with SessionLocal() as session:
        content = session.scalar(select(StoredFile.content).where(StoredFile.key == key_for(path)))
    if content is None:
        raise FileNotFoundError(str(path))
    _write_disk(path, content)
    return content


def exists(path: str | Path) -> bool:
    path = Path(path)
    if path.is_file():
        return True
    if not enabled():
        return False
    with SessionLocal() as session:
        return session.scalar(select(StoredFile.key).where(StoredFile.key == key_for(path))) is not None


def remove(paths: list[str], db: Session) -> None:
    """DB 사본을 지운다(호출자 트랜잭션). 디스크 파일은 커밋 뒤 호출자가 지운다."""
    if enabled() and paths:
        keys = [key for p in paths for key in (key_for(p), key_for(f"{p}.layout.pdf"))]
        db.execute(delete(StoredFile).where(StoredFile.key.in_(keys)))
