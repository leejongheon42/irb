"""등록 서식의 자리표시자 채우기."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..engine.ruleset import get_ruleset
from ..models import Protocol
from . import core, docx_engine, hwpx_engine
from . import placeholders as PH
from .fields import SPECIAL_BLANK, SPECIAL_KEEP, FieldResolver, format_value
from .models import MEDIA_TYPES, DocumentTemplate

ENGINES = {"docx": docx_engine, "hwpx": hwpx_engine}

# ---------------------------------------------------------------- 채우기
@dataclass
class FillResult:
    content: bytes
    filename: str
    media_type: str
    fmt: str
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stats: core.FillStats | None = None


class FillContext:
    """자리표시자 하나를 문자열로 바꾸는 규칙(템플릿 매핑 + 과제 값)."""

    def __init__(self, tpl: DocumentTemplate, resolver: FieldResolver):
        self.tpl = tpl
        self.resolver = resolver
        self.missing: set[str] = set()

    def render(self, ph: PH.Placeholder, scope: core.Scope | None) -> str | None:
        if ph.marker:
            return ""  # 반복 표식은 지운다
        if ph.is_item_scoped:
            return self._render_item(ph, scope)
        from .service import resolve_target

        path, default_fmt = resolve_target(self.tpl, ph.name)
        if path is None:
            self.missing.add(ph.name)
            return None
        if path == SPECIAL_KEEP:
            return None
        if path == SPECIAL_BLANK:
            return ""
        fmt = ph.fmt or default_fmt
        if ph.op:
            return format_value(self.resolver.compare(path, ph.op, ph.operand), fmt or "check")
        return format_value(self.resolver.value(path), fmt)

    def _render_item(self, ph: PH.Placeholder, scope: core.Scope | None) -> str | None:
        if scope is None:
            return None  # 반복 행 밖의 item/no 는 그대로 둔다
        target, attr = PH.normalize_item_name(ph.name)
        if scope.empty:
            return ""
        if target == "no":
            return str(scope.index)
        item = scope.item
        if attr:
            value = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
        else:
            value = item
        if ph.op:
            return format_value(_item_compare(value, ph.op, ph.operand), ph.fmt or "check")
        return format_value(value, ph.fmt)

    def list_items(self, name: str) -> list[Any] | None:
        from .service import resolve_target

        path, _ = resolve_target(self.tpl, name)
        if path is None or path in (SPECIAL_BLANK, SPECIAL_KEEP):
            return None
        value = self.resolver.value(path)
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]


def _item_compare(value: Any, op: str, operand: str) -> bool:
    text = format_value(value)
    return operand in text if op == "~" else text == operand


def fill(db: Session, tpl: DocumentTemplate, protocol: Protocol) -> FillResult:
    from .service import TemplateError, read_original

    if tpl.committee != (protocol.committee or "institution"):
        raise TemplateError("과제와 서식의 심의위원회가 다릅니다.")
    raw = read_original(tpl)
    ctx = FillContext(tpl, FieldResolver(db, protocol))
    stats = core.FillStats()
    try:
        content = ENGINES[tpl.fmt].fill(raw, ctx.render, ctx.list_items, stats)
    except TemplateError:
        raise
    except Exception as e:  # 손상된 서식 등
        raise TemplateError(f"서식을 채우는 중 오류가 발생했습니다({type(e).__name__}).") from e
    label = get_ruleset().label("document_types", tpl.doc_type)
    warnings = list(stats.warnings)
    if ctx.missing:
        warnings.append("매핑되지 않아 그대로 남은 자리표시자: " + ", ".join(sorted(ctx.missing)))
    return FillResult(
        content=content,
        filename=f"{label}_v{protocol.version}.{tpl.fmt}",
        media_type=MEDIA_TYPES[tpl.fmt],
        fmt=tpl.fmt,
        warnings=warnings,
        missing=sorted(ctx.missing),
        stats=stats,
    )


def plain_text(tpl: DocumentTemplate, raw: bytes | None = None) -> str:
    from .service import read_original

    return ENGINES[tpl.fmt].plain_text(raw if raw is not None else read_original(tpl))


