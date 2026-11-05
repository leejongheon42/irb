from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings


class Ruleset:
    """YAML 규칙 세트 래퍼. 기관 SOP에 맞게 YAML만 수정하면 엔진 동작이 바뀐다."""

    def __init__(self, data: dict[str, Any], digest: str, path: Path | None = None):
        self.data = data
        self.digest = digest
        self.path = path

    # ------------------------------------------------------------------ 기본 정보
    @property
    def version(self) -> str:
        return f"{self.data.get('version', 'unknown')}#{self.digest[:8]}"

    def legal(self, key: str | None) -> str:
        if not key:
            return ""
        return self.data.get("legal_basis", {}).get(key, key)

    # ------------------------------------------------------------------ 카탈로그
    def catalog(self, name: str) -> dict[str, Any]:
        return self.data.get("catalogs", {}).get(name, {})

    def label(self, catalog: str, key: str) -> str:
        entry = self.catalog(catalog).get(key)
        if entry is None:
            return key
        if isinstance(entry, dict):
            return entry.get("label", key)
        return str(entry)

    def weight(self, catalog: str, key: str) -> int:
        entry = self.catalog(catalog).get(key)
        if isinstance(entry, dict):
            return int(entry.get("weight", 0))
        return 0

    def options(self, catalog: str) -> list[tuple[str, str]]:
        return [(k, self.label(catalog, k)) for k in self.catalog(catalog)]

    # ------------------------------------------------------------------ 정책
    @property
    def risk_weights(self) -> dict[str, Any]:
        return self.data.get("risk_weights", {})

    @property
    def risk_levels(self) -> list[dict[str, Any]]:
        return self.data.get("risk_levels", [])

    @property
    def review_policy(self) -> dict[str, Any]:
        return self.data.get("review_type", {})

    @property
    def required_documents(self) -> list[dict[str, Any]]:
        return self.data.get("required_documents", [])

    @property
    def deid_policy(self) -> dict[str, Any]:
        return self.data.get("deid_policy", {})


def load_ruleset(path: Path) -> Ruleset:
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    return Ruleset(data, hashlib.sha256(raw).hexdigest(), path)


@lru_cache(maxsize=4)
def _cached(path_str: str, mtime: float) -> Ruleset:
    return load_ruleset(Path(path_str))


def get_ruleset() -> Ruleset:
    path = get_settings().ruleset_path
    return _cached(str(path), path.stat().st_mtime)
