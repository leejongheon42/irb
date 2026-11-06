"""LibreOffice(soffice) 헤드리스 변환 도우미."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)


def soffice_available() -> bool:
    path = get_settings().soffice_path
    return bool(path) and Path(path).exists()


def soffice_convert(filename: str, raw: bytes, target: str = "pdf", timeout: int = 90) -> bytes | None:
    """문서를 LibreOffice로 변환한다. 실패하거나 LibreOffice가 없으면 None.

    요청마다 별도 사용자 프로필 디렉터리를 써서 동시 변환 충돌을 피한다.
    """
    soffice = get_settings().soffice_path
    if not soffice or not Path(soffice).exists():
        return None
    suffix = Path(filename).suffix.lower() or ".bin"
    with tempfile.TemporaryDirectory(prefix="irb_soffice_") as tmp:
        src = Path(tmp) / f"input{suffix}"
        src.write_bytes(raw)
        profile = Path(tmp) / "profile"
        cmd = [
            soffice, "--headless", "--norestore", "--nolockcheck",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to", target, "--outdir", tmp, str(src),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout, env={**os.environ, "HOME": tmp}, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("soffice conversion failed: %s", e)
            return None
        ext = target.split(":", 1)[0]
        out = Path(tmp) / f"input.{ext}"
        if proc.returncode != 0 or not out.exists():
            log.info("soffice could not convert %s (rc=%s)", filename, proc.returncode)
            return None
        return out.read_bytes()
