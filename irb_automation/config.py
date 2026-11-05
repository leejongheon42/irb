"""애플리케이션 설정.

모든 값은 환경변수로 덮어쓸 수 있다. 병원 폐쇄망(망분리) 환경을 가정하여
외부 의존성 없이 동작하도록 기본값을 잡았다.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass
class Settings:
    institution_name: str = field(
        default_factory=lambda: os.environ.get("IRB_INSTITUTION_NAME", "○○대학교병원")
    )
    irb_name: str = field(
        default_factory=lambda: os.environ.get("IRB_COMMITTEE_NAME", "○○대학교병원 기관생명윤리위원회")
    )
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("IRB_DATA_DIR", str(Path.cwd() / "data")))
    )
    database_url: str = ""
    secret_key: str = field(
        default_factory=lambda: os.environ.get("IRB_SECRET_KEY") or secrets.token_hex(32)
    )
    # 가명처리용 HMAC 비밀키 (추가정보) - 반드시 별도 보관. 운영 시 환경변수로 주입.
    pseudonym_secret: str = field(
        default_factory=lambda: os.environ.get("IRB_PSEUDONYM_SECRET", "")
    )
    ruleset_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("IRB_RULESET_PATH", str(BASE_DIR / "rules" / "default_ruleset.yaml"))
        )
    )
    approval_validity_days: int = field(
        default_factory=lambda: _env_int("IRB_APPROVAL_VALIDITY_DAYS", 365)
    )
    expiry_notice_days: tuple[int, ...] = (60, 30, 7)
    reviewer_due_days: int = field(default_factory=lambda: _env_int("IRB_REVIEWER_DUE_DAYS", 14))
    max_upload_mb: int = field(default_factory=lambda: _env_int("IRB_MAX_UPLOAD_MB", 50))
    password_iterations: int = field(
        default_factory=lambda: _env_int("IRB_PASSWORD_ITERATIONS", 310_000)
    )
    session_https_only: bool = field(
        default_factory=lambda: _env_bool("IRB_SESSION_HTTPS_ONLY", False)
    )
    scheduler_interval_seconds: int = field(
        default_factory=lambda: _env_int("IRB_SCHEDULER_INTERVAL", 3600)
    )
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("IRB_SCHEDULER_ENABLED", True))

    # AI 사전검토 보조 (선택). 연구계획서 텍스트만 전송하며 환자 데이터는 전송하지 않는다.
    ai_review_enabled: bool = field(default_factory=lambda: _env_bool("IRB_AI_REVIEW_ENABLED", False))
    ai_model: str = field(default_factory=lambda: os.environ.get("IRB_AI_MODEL", "claude-opus-5-5"))

    def __post_init__(self) -> None:
        if not self.database_url:
            self.database_url = os.environ.get(
                "IRB_DATABASE_URL", f"sqlite:///{self.data_dir / 'irb.db'}"
            )
        if not self.pseudonym_secret:
            # 개발 환경용: 데이터 디렉터리에 키를 생성해 둔다(운영에서는 HSM/별도 서버 보관 권장).
            key_file = self.data_dir / "keys" / "pseudonym.key"
            if key_file.exists():
                self.pseudonym_secret = key_file.read_text().strip()
            else:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                self.pseudonym_secret = secrets.token_hex(32)
                key_file.write_text(self.pseudonym_secret)
                os.chmod(key_file, 0o600)

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def deid_dir(self) -> Path:
        return self.data_dir / "deid"

    @property
    def mapping_dir(self) -> Path:
        """추가정보(매핑테이블) 저장소 - 가명정보와 분리 보관 (개인정보보호법 제28조의4)."""
        return self.data_dir / "keys" / "mappings"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.deid_dir, self.mapping_dir):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def override_settings(settings: Settings) -> Settings:
    """테스트 등에서 설정을 교체할 때 사용."""
    global _settings
    _settings = settings
    settings.ensure_dirs()
    return settings
