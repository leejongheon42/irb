"""애플리케이션 설정.

모든 값은 환경변수로 덮어쓸 수 있다. 병원 폐쇄망(망분리) 환경을 가정하여
외부 의존성 없이 동작하도록 기본값을 잡았다.
"""
from __future__ import annotations

import os
import secrets
import shutil
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
    # 서류 품질 점검 기준(기관용 사본). 비우면 irb_automation/qc/criteria.yaml
    qc_criteria_path: str = field(default_factory=lambda: os.environ.get("IRB_QC_CRITERIA_PATH", ""))
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

    # AI 기능 (선택). 기본 비활성화 - 외부 API 전송이므로 기관 승인 후 활성화한다.
    # IRB_AI_ENABLED 또는 (하위 호환) IRB_AI_REVIEW_ENABLED 로 켠다.
    ai_review_enabled: bool = field(
        default_factory=lambda: _env_bool("IRB_AI_ENABLED", False) or _env_bool("IRB_AI_REVIEW_ENABLED", False)
    )
    ai_model: str = field(default_factory=lambda: os.environ.get("IRB_AI_MODEL", "claude-opus-5-5"))

    # 문서 자동화 파이프라인
    # 업로드 시 자동 실행(로컬 단계는 항상, AI 단계는 AI 활성 시에만)
    ai_pipeline_auto: bool = field(default_factory=lambda: _env_bool("IRB_AI_PIPELINE_AUTO", True))
    # AI로 원본 파일(PDF·이미지)을 보낼 수 있는 서류 종류. 환자 자료가 들어가지 않는 서류만 허용한다.
    ai_doc_types_allowed: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip()
            for t in os.environ.get("IRB_AI_DOC_TYPES_ALLOWED", "protocol,application,crf,deid_plan").split(",")
            if t.strip()
        )
    )
    # LibreOffice 실행 파일(문서 → PDF 변환). 미지정 시 PATH에서 자동 탐지
    soffice_path: str = field(
        default_factory=lambda: os.environ.get("IRB_SOFFICE_PATH")
        or shutil.which("soffice")
        or shutil.which("libreoffice")
        or ""
    )
    # 파이프라인을 요청 스레드에서 동기 실행(테스트·디버그용)
    pipeline_sync: bool = field(default_factory=lambda: _env_bool("IRB_PIPELINE_SYNC", False))

    @property
    def ai_enabled(self) -> bool:
        return self.ai_review_enabled

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
    def pipeline_dir(self) -> Path:
        """문서 자동화 파이프라인 작업 파일(가져오기 원본, 변환 결과)."""
        return self.data_dir / "pipeline"

    @property
    def mapping_dir(self) -> Path:
        """추가정보(매핑테이블) 저장소 - 가명정보와 분리 보관 (개인정보보호법 제28조의4)."""
        return self.data_dir / "keys" / "mappings"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.deid_dir, self.mapping_dir, self.pipeline_dir):
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
