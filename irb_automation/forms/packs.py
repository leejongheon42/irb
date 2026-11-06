"""양식 팩: 기관 프로필 + 양식 정의 묶음(YAML). 병원 간 이식·백업·초기 설정에 사용한다.

pack: <영문 식별자>
name: 표시 이름
description: 설명
profile: {institution_name, irb_name, protocol_no_format, letter_footer, contact}   # 선택
forms:
  - kind: protocol | online:<doc_type>
    name: 양식 이름
    schema: {...}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import audit
from ..models import User
from .models import FormDefinition
from .profile import PROFILE_FIELDS, get_profile, update_profile, validate_format
from .schema import SchemaError, has_any_definition, save_definition, validate_schema

PACK_DIR = Path(__file__).parent / "packs"
MAX_PACK_BYTES = 512 * 1024


def bundled_packs() -> list[dict[str, Any]]:
    out = []
    for path in sorted(PACK_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        out.append({"id": path.stem, "name": data.get("name", path.stem), "description": data.get("description", ""),
                    "forms": len(data.get("forms") or [])})
    return out


def read_bundled(pack_id: str) -> str:
    ids = {p["id"] for p in bundled_packs()}
    if pack_id not in ids:
        raise SchemaError([f"알 수 없는 기본 제공 양식 팩: {pack_id}"])
    return (PACK_DIR / f"{pack_id}.yaml").read_text(encoding="utf-8")


def validate_pack(pack: Any) -> list[str]:
    if not isinstance(pack, dict):
        return ["양식 팩은 YAML 매핑이어야 합니다."]
    errors: list[str] = []
    forms = pack.get("forms")
    if not isinstance(forms, list) or not forms:
        errors.append("forms 목록이 비어 있습니다.")
        forms = []
    kinds: set[str] = set()
    for i, f in enumerate(forms, 1):
        if not isinstance(f, dict):
            errors.append(f"{i}번째 양식 형식이 올바르지 않습니다.")
            continue
        kind = str(f.get("kind", ""))
        label = f"{i}번째 양식({kind or 'kind 없음'})"
        if kind in kinds:
            errors.append(f"{label}: 같은 kind가 중복됩니다.")
        kinds.add(kind)
        if not str(f.get("name", "")).strip():
            errors.append(f"{label}: name이 필요합니다.")
        errors += [f"{label}: {e}" for e in validate_schema(kind, f.get("schema"))]
    profile = pack.get("profile")
    if profile is not None:
        if not isinstance(profile, dict):
            errors.append("profile은 매핑이어야 합니다.")
        else:
            unknown = sorted(set(profile) - set(PROFILE_FIELDS))
            if unknown:
                errors.append(f"profile의 알 수 없는 항목: {', '.join(unknown)}")
            if "protocol_no_format" in profile:
                errors += [f"profile.protocol_no_format: {e}" for e in validate_format(str(profile["protocol_no_format"]))]
    return errors


def parse_pack(text: str | bytes) -> dict[str, Any]:
    if isinstance(text, bytes):
        if len(text) > MAX_PACK_BYTES:
            raise SchemaError(["양식 팩 파일이 너무 큽니다(512KB 이하)."])
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise SchemaError(["UTF-8 YAML/JSON 파일만 가져올 수 있습니다."]) from e
    try:
        pack = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SchemaError([f"YAML 문법 오류: {e}"]) from e
    errors = validate_pack(pack)
    if errors:
        raise SchemaError(errors)
    return pack


def import_pack(
    db: Session,
    user: User | None,
    pack: dict[str, Any],
    *,
    activate: bool = True,
    apply_profile: bool = False,
    ip: str = "",
) -> dict[str, Any]:
    errors = validate_pack(pack)
    if errors:
        raise SchemaError(errors)
    source = str(pack.get("pack") or pack.get("name") or "import")[:100]
    created = []
    for f in pack["forms"]:
        d = save_definition(db, user, f["kind"], str(f["name"]), f["schema"], activate=activate,
                            note=str(f.get("note", "")), source=source, ip=ip)
        created.append(d)
    profile_applied = False
    if apply_profile and isinstance(pack.get("profile"), dict):
        current = get_profile(db)
        merged = {k: getattr(current, k) for k in PROFILE_FIELDS}
        merged.update({k: str(v) for k, v in pack["profile"].items()})
        if user is None:
            for k, v in merged.items():
                setattr(current, k, v)
        else:
            errs = update_profile(db, user, merged, ip)
            if errs:
                raise SchemaError(errs)
        profile_applied = True
    audit.record(db, user, "form_pack_import", "form_pack", None,
                 {"pack": source, "forms": [f"{d.kind} v{d.version}" for d in created],
                  "activated": activate, "profile": profile_applied}, ip)
    return {"forms": created, "profile": profile_applied, "source": source}


def export_pack(db: Session, name: str = "") -> str:
    profile = get_profile(db)
    forms = db.scalars(select(FormDefinition).where(FormDefinition.active.is_(True)).order_by(FormDefinition.kind)).all()
    pack = {
        "pack": "export",
        "name": name or f"{profile.institution_name} 양식 팩",
        "description": "IRB 자동화 솔루션에서 내보낸 활성 양식과 기관 프로필",
        "profile": {k: getattr(profile, k) for k in PROFILE_FIELDS},
        "forms": [{"kind": d.kind, "name": d.name, "schema": d.schema} for d in forms],
    }
    return yaml.safe_dump(pack, allow_unicode=True, sort_keys=False, width=120)


def seed_forms(db: Session) -> bool:
    """양식이 한 번도 설정되지 않았으면 기본 양식 팩을 활성화한다(멱등)."""
    if has_any_definition(db):
        return False
    import_pack(db, None, parse_pack(read_bundled("default")), activate=True, apply_profile=False)
    return True


def ensure_seeded() -> None:
    """화면 진입 시 자동 로드(별도 세션). DB 잠금 등으로 실패하면 조용히 건너뛴다."""
    from ..database import SessionLocal

    with SessionLocal() as db:
        try:
            if seed_forms(db):
                db.commit()
        except SQLAlchemyError:
            db.rollback()
