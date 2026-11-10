"""문서 자동화 파이프라인 실행.

단계(종류별로 해당 단계만 생성)
  recognize   ① 문서 인식(텍스트 추출, 스캔본은 AI OCR)
  classify    ② 서류 종류 분류(업로드 종류와 불일치 경고)
  pii         ③ 개인정보 점검(실제 환자 식별정보 → 사무국 알림)
  extract     ④ 정보 추출(규칙 + AI 병합)
  convert     ⑤ 변환(마크다운 저장, PDF는 요청 시 생성)
  consistency ⑥ 정합성(과제 입력값 vs 문서 내용)
  prereview   ⑦ 사전검토(규칙 엔진)
  related     ⑧ 관련 정보 검색(지식베이스·유사 과제)
  ai_summary  ⑨ AI 검토 요약(AI 활성 시)
  forms       ⑩ (가져오기) 기관 서식 자동 작성 제안

각 단계는 실패해도 다음 단계를 가능한 범위에서 계속한다. 실행은 커밋 이후 백그라운드 스레드에서
별도 DB 세션으로 수행되며(IRB_PIPELINE_SYNC=true 이면 동기 실행), 단계별 결과·소요시간을 저장한다.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from .. import audit
from ..ai import client as ai
from ..ai import privacy
from ..config import get_settings
from ..database import SessionLocal
from ..docproc import classify as cls
from ..docproc import extract_fields as ef
from ..docproc.convert import external_to_protocol, safe_name, to_markdown
from ..docproc.text import ExtractedText, detect_format, extract_text
from ..engine.facts import PROTOCOL_FIELDS, ProtocolFacts
from ..engine.prereview import run_prereview
from ..engine.ruleset import get_ruleset
from ..models import Document, Protocol, User
from ..notifications import admins, notify
from ..timeutil import now
from ..workflow import service as wf
from .models import ExtractedTextCache, PipelineRun, PipelineStep

log = logging.getLogger(__name__)

STEP_LABELS = {
    "recognize": "문서 인식",
    "classify": "서류 분류",
    "pii": "개인정보 점검",
    "extract": "정보 추출",
    "convert": "형식 변환",
    "consistency": "정합성 점검",
    "prereview": "사전검토",
    "related": "관련 정보 검색",
    "ai_summary": "AI 검토 요약",
    "forms": "기관 서식 작성 제안",
}
KIND_LABELS = {"document": "서류 분석", "import": "타 기관 계획서 가져오기", "protocol": "과제 재분석"}
STATUS_LABELS = {"queued": "대기", "running": "진행 중", "succeeded": "완료", "partial": "일부 완료",
                 "failed": "실패", "skipped": "건너뜀"}
STEPS_BY_KIND = {
    "document": ["recognize", "classify", "pii", "extract", "convert", "consistency", "related", "ai_summary"],
    "protocol": ["recognize", "classify", "pii", "extract", "convert", "consistency", "prereview", "related", "ai_summary"],
    "import": ["recognize", "classify", "pii", "extract", "convert", "prereview", "related", "ai_summary", "forms"],
}
# 정보 추출·정합성 점검 대상 서류(계획서 성격)
PLAN_DOC_TYPES = {"protocol", "application", "deid_plan", "waiver_request", "exemption_request"}

_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="irb-pipeline")
    return _executor


class Skip(Exception):  # noqa: N818 - 흐름 제어용
    """단계를 건너뛴다(사유 메시지)."""


# ====================================================================== 시작
def _create_run(db: Session, user: User | None, kind: str, **values: Any) -> PipelineRun:
    run = PipelineRun(kind=kind, created_by=user.id if user else None, status="queued", **values)
    for seq, name in enumerate(STEPS_BY_KIND[kind], start=1):
        run.steps.append(PipelineStep(seq=seq, name=name, status="queued"))
    db.add(run)
    db.flush()
    audit.record(db, user, "pipeline_start", "pipeline", run.id,
                 {"kind": kind, "protocol_id": run.protocol_id, "document_id": run.document_id})
    return run


def _dispatch(db: Session, run_id: int) -> None:
    """커밋 이후 실행(백그라운드) - 동기 모드면 즉시 커밋 후 실행."""
    if get_settings().pipeline_sync:
        db.commit()
        run_pipeline(run_id)
        db.expire_all()
        return

    def _after_commit(_session: Session) -> None:
        _pool().submit(_safe_run, run_id)

    event.listen(db, "after_commit", _after_commit, once=True)


def _safe_run(run_id: int) -> None:
    try:
        run_pipeline(run_id)
    except Exception:  # 백그라운드 스레드에서 예외가 사라지지 않도록 기록
        log.exception("pipeline run %s crashed", run_id)


def start_document_pipeline(db: Session, user: User | None, document_id: int) -> PipelineRun | None:
    """서류 저장 직후 호출(제출 모듈 hook). 자동 실행이 꺼져 있으면 None."""
    if not get_settings().ai_pipeline_auto:
        return None
    doc = db.get(Document, document_id)
    if doc is None:
        return None
    run = _create_run(db, user, "document", protocol_id=doc.protocol_id, document_id=doc.id,
                      input_name=doc.filename, input_path=doc.stored_path, input_sha256=doc.sha256)
    _dispatch(db, run.id)
    return run


def start_import_pipeline(db: Session, user: User, filename: str, raw: bytes, *, sync: bool = False) -> PipelineRun:
    folder = get_settings().pipeline_dir / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}_{safe_name(filename)}"
    path.write_bytes(raw)
    run = _create_run(db, user, "import", input_name=Path(filename).name, input_path=str(path),
                      input_sha256=hashlib.sha256(raw).hexdigest())
    if sync:  # 호출한 쪽에서 즉시 결과가 필요할 때(전체 파이프라인 테스트 등)
        db.commit()
        run_pipeline(run.id)
        db.expire_all()
        return db.get(PipelineRun, run.id)
    _dispatch(db, run.id)
    return run


def start_protocol_pipeline(db: Session, user: User | None, protocol_id: int) -> PipelineRun:
    protocol = db.get(Protocol, protocol_id)
    if protocol is None:
        raise ValueError("과제를 찾을 수 없습니다.")
    doc = latest_plan_document(protocol)
    run = _create_run(
        db, user, "protocol", protocol_id=protocol_id, document_id=doc.id if doc else None,
        input_name=doc.filename if doc else "(과제 입력값)", input_path=doc.stored_path if doc else "",
        input_sha256=doc.sha256 if doc else "",
    )
    _dispatch(db, run.id)
    return run


def latest_plan_document(protocol: Protocol) -> Document | None:
    docs = [d for d in protocol.documents if d.doc_type == "protocol"]
    return max(docs, key=lambda d: (d.version, d.id)) if docs else None


# ====================================================================== 실행
class _Context:
    def __init__(self, db: Session, run: PipelineRun) -> None:
        self.db = db
        self.run = run
        self.user = db.get(User, run.created_by) if run.created_by else None
        self.document = db.get(Document, run.document_id) if run.document_id else None
        self.protocol = db.get(Protocol, run.protocol_id) if run.protocol_id else None
        self.use_ai = ai.ai_enabled()
        self.raw: bytes | None = None
        self.extracted: ExtractedText | None = None
        self.doc_type: str = (self.document.doc_type if self.document else "protocol")
        self.classified: str | None = None
        self.fields: dict[str, dict[str, Any]] = {}
        self.pii: dict[str, int] = {}
        self.findings: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.prereview: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return self.extracted.text if self.extracted else ""

    @property
    def effective_type(self) -> str:
        if self.run.kind == "import":
            return "protocol"
        return self.doc_type

    def transmit(self, purpose: str, payload: bytes | str, kind: str, masked: int = 0) -> None:
        privacy.record_transmission(self.db, self.user, purpose, "pipeline", self.run.id, payload, kind, masked)


def run_pipeline(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = now()
        run.error = ""
        for step in run.steps:
            step.status, step.message, step.result = "queued", "", {}
        db.commit()
        ctx = _Context(db, run)
        for step in run.steps:
            _run_step(ctx, step)
        statuses = {s.status for s in run.steps}
        run.status = "failed" if run.step("recognize") and run.step("recognize").status == "failed" else (
            "partial" if "failed" in statuses else "succeeded")
        run.finished_at = now()
        run.summary = _summary(ctx)
        audit.record(db, ctx.user, "pipeline_finish", "pipeline", run.id, {"status": run.status})
        db.commit()
    except Exception as e:
        db.rollback()
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status, run.error, run.finished_at = "failed", f"{type(e).__name__}: {e}", now()
            db.commit()
        raise
    finally:
        db.close()


def _run_step(ctx: _Context, step: PipelineStep) -> None:
    handler = STEP_HANDLERS[step.name]
    step.status, step.started_at = "running", now()
    ctx.db.commit()
    t0 = time.monotonic()
    try:
        result = handler(ctx) or {}
        step.message = str(result.pop("_message", ""))
        step.result = result
        step.status = "succeeded"
    except Skip as s:
        step.status, step.message = "skipped", str(s)
    except Exception as e:  # 단계 실패는 기록하고 계속
        ctx.db.rollback()
        log.warning("pipeline step %s failed: %s", step.name, e, exc_info=True)
        step = ctx.db.get(PipelineStep, step.id)
        step.status, step.message = "failed", f"{type(e).__name__}: {e}"[:1000]
    step.finished_at = now()
    step.duration_ms = int((time.monotonic() - t0) * 1000)
    ctx.db.commit()


# ---------------------------------------------------------------------- ① 인식
def _load_raw(ctx: _Context) -> bytes | None:
    path = ctx.run.input_path
    if path and Path(path).is_file():
        return Path(path).read_bytes()
    return None


def step_recognize(ctx: _Context) -> dict[str, Any]:
    raw = _load_raw(ctx)
    if raw is None:
        if ctx.run.kind == "protocol" and ctx.protocol is not None:
            ctx.extracted = ExtractedText(protocol_text(ctx.protocol), None, "protocol_fields")
            return {"_message": "첨부된 연구계획서가 없어 과제 입력값으로 분석합니다.", "method": "protocol_fields",
                    "chars": ctx.extracted.chars}
        raise RuntimeError("원본 파일을 찾을 수 없습니다.")
    ctx.raw = raw
    sha = hashlib.sha256(raw).hexdigest()
    cached = ctx.db.scalars(select(ExtractedTextCache).where(ExtractedTextCache.sha256 == sha)).first()
    if cached is not None and cached.text.strip():
        ctx.extracted = ExtractedText(cached.text, cached.pages, cached.method, list(cached.warnings or []))
        return {"_message": f"이전 인식 결과 재사용({cached.method})", "method": cached.method, "pages": cached.pages,
                "chars": ctx.extracted.chars, "warnings": ctx.extracted.warnings, "cached": True}
    fmt = detect_format(ctx.run.input_name, raw)
    extracted = extract_text(ctx.run.input_name, raw)
    if extracted.chars < 30 and fmt in ("pdf", "png", "jpeg", "gif", "webp") and ctx.use_ai:
        allowed, reason = privacy.binary_allowed(ctx.effective_type, extracted.text)
        if allowed:
            ocr = extract_text(ctx.run.input_name, raw, allow_ai_ocr=True)
            if ocr.method == "ai_ocr":
                ctx.transmit("문서 OCR", raw, "pdf" if fmt == "pdf" else "image")
            extracted = ocr
        else:
            extracted.warnings.append(f"AI OCR 미사용: {reason}")
    ctx.extracted = extracted
    if extracted.chars:
        row = cached or ExtractedTextCache(sha256=sha)
        row.document_id = ctx.run.document_id
        row.method, row.pages, row.text, row.warnings = extracted.method, extracted.pages, extracted.text, extracted.warnings
        ctx.db.add(row)
    msg = f"{extracted.method} · {extracted.chars:,}자" + (f" · {extracted.pages}쪽" if extracted.pages else "")
    if not extracted.chars:
        msg = "텍스트를 인식하지 못했습니다. " + " ".join(extracted.warnings)
    return {"_message": msg, "method": extracted.method, "pages": extracted.pages, "chars": extracted.chars,
            "warnings": extracted.warnings, "format": fmt}


def protocol_text(protocol: Protocol) -> str:
    labels = ef.FIELD_LABELS
    lines = []
    for key in ("title", "objectives", "background", "methods", "target_population", "inclusion_criteria",
                "exclusion_criteria", "data_items", "access_controls", "destruction_method"):
        value = getattr(protocol, key, "") or ""
        if str(value).strip():
            lines += [f"# {labels.get(key, key)}", str(value)]
    return "\n".join(lines)


def _need_text(ctx: _Context) -> str:
    if not ctx.text.strip():
        raise Skip("인식된 텍스트가 없습니다.")
    return ctx.text


# ---------------------------------------------------------------------- ② 분류
def step_classify(ctx: _Context) -> dict[str, Any]:
    text = _need_text(ctx)
    if ctx.extracted and ctx.extracted.method == "protocol_fields":
        raise Skip("과제 입력값 분석에는 분류가 필요 없습니다.")
    doc_type, conf, reason = cls.classify_document(ctx.run.input_name, text)
    method = "rule"
    if ctx.use_ai and conf < 0.6:
        guarded = privacy.mask_text(text[:6000])
        result = cls.classify_with_ai(ctx.run.input_name, guarded.text)
        if result:
            ctx.transmit("서류 분류", guarded.text, "text", guarded.masked)
            doc_type, conf, reason = result
            method = "ai"
    ctx.classified = doc_type
    rs = get_ruleset()
    declared = ctx.document.doc_type if ctx.document else None
    mismatch = bool(declared and declared != "other" and doc_type != declared and conf >= 0.6)
    msg = f"{rs.label('document_types', doc_type)} (신뢰도 {conf:.0%})"
    if mismatch:
        msg += f" - 업로드 종류 '{rs.label('document_types', declared)}'와 다를 수 있습니다."
    return {"_message": msg, "doc_type": doc_type, "label": rs.label("document_types", doc_type),
            "confidence": conf, "reason": reason, "method": method, "declared": declared, "mismatch": mismatch}


# ---------------------------------------------------------------------- ③ 개인정보 점검
PII_LABELS = {"rrn": "주민등록번호", "phone": "전화번호", "mobile": "휴대전화번호", "email": "이메일", "address": "주소", "card": "카드번호",
              "passport": "여권번호", "ip": "IP 주소", "name": "이름(호칭형)", "mrn": "등록번호", "account": "계좌번호",
              "foreigner_no": "외국인등록번호", "driver_license": "운전면허번호", "business_no": "사업자번호"}


def step_pii(ctx: _Context) -> dict[str, Any]:
    text = _need_text(ctx)
    counts = privacy.scan(text)
    ctx.pii = counts
    if not counts:
        return {"_message": "개인정보 패턴이 탐지되지 않았습니다.", "counts": {}}
    detail = ", ".join(f"{PII_LABELS.get(k, k)} {v}건" for k, v in counts.items())
    severe = bool({"rrn", "foreigner_no", "passport", "card", "account"} & set(counts)) or sum(counts.values()) >= 3
    if severe and ctx.run.kind != "protocol":
        link = f"/pipeline/{ctx.run.id}"
        where = f"과제 {ctx.protocol.protocol_no or ctx.protocol.id}" if ctx.protocol else "가져오기 문서"
        notify(ctx.db, {*([ctx.user.id] if ctx.user else []), *(u.id for u in admins(ctx.db))}, f"[개인정보 주의] {where}의 '{ctx.run.input_name}'에서 {detail}이(가) 탐지되었습니다.",
               link=link, dedup_key=f"pipeline-pii-{ctx.run.input_sha256 or ctx.run.id}")
    return {"_message": f"탐지: {detail}" + (" - 본인과 관리자에게 알렸습니다." if severe and ctx.run.kind != "protocol" else ""),
            "counts": counts, "severe": severe}


# ---------------------------------------------------------------------- ④ 정보 추출
def step_extract(ctx: _Context) -> dict[str, Any]:
    text = _need_text(ctx)
    target = ctx.classified or ctx.effective_type
    if ctx.run.kind != "import" and target not in PLAN_DOC_TYPES and ctx.effective_type not in PLAN_DOC_TYPES:
        raise Skip("계획서 성격의 서류가 아니어서 정보 추출을 건너뜁니다.")
    fields = ef.extract_fields(text)
    fields.update(ef.extract_custom_fields(text, ef.custom_field_defs(ctx.db)))
    notes = []
    if ctx.use_ai:
        guarded = privacy.mask_text(text)
        try:
            ai_fields = ef.ai_extract_fields(guarded.text)
            ctx.transmit("계획서 정보 추출", guarded.text, "text", guarded.masked)
            fields = ef.merge_fields(fields, ai_fields)
            notes.append(f"AI 보조 {len(ai_fields)}개 항목")
        except ai.AIError as e:
            notes.append(f"AI 보조 실패: {e}")
    ctx.fields = fields
    msg = f"{len(fields)}개 항목 추출" + (f" ({'; '.join(notes)})" if notes else " (규칙 기반)")
    return {"_message": msg, "fields": fields}


# ---------------------------------------------------------------------- ⑤ 변환
def step_convert(ctx: _Context) -> dict[str, Any]:
    _need_text(ctx)
    assert ctx.extracted is not None
    folder = get_settings().pipeline_dir / "converted"
    folder.mkdir(parents=True, exist_ok=True)
    md_path = folder / f"run_{ctx.run.id}.md"
    md_path.write_text(to_markdown(ctx.extracted, ctx.run.input_name), encoding="utf-8")
    return {"_message": "마크다운 변환 완료 · PDF는 내려받을 때 생성", "markdown_path": str(md_path),
            "pdf": True}


# ---------------------------------------------------------------------- ⑥ 정합성
def _norm_list(v: Any) -> set[str]:
    return set(v or []) if isinstance(v, (list, tuple, set)) else set()


def compare_protocol(protocol: Any, fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """과제 입력값과 문서 추출값의 불일치 목록."""
    rs = get_ruleset()
    issues: list[dict[str, Any]] = []

    def add(key: str, severity: str, message: str, proto_value: Any, doc_value: Any) -> None:
        issues.append({"field": key, "label": ef.FIELD_LABELS.get(key, key), "severity": severity, "message": message,
                       "protocol_value": proto_value, "document_value": doc_value,
                       "snippet": fields.get(key, {}).get("source_snippet", "")})

    def val(key: str) -> Any:
        e = fields.get(key)
        return e.get("value") if e else None

    n_doc, n_pro = val("expected_subjects"), getattr(protocol, "expected_subjects", 0) or 0
    if isinstance(n_doc, int) and n_doc and n_pro and abs(n_doc - n_pro) / max(n_doc, n_pro) > 0.1:
        add("expected_subjects", "major", f"대상자 수가 다릅니다(입력 {n_pro:,}명 / 문서 {n_doc:,}명).", n_pro, n_doc)

    for key in ("study_start", "study_end", "data_period_start", "data_period_end"):
        d, p = val(key), getattr(protocol, key, None)
        if d and p and str(p) != str(d):
            add(key, "major" if key.startswith("data_period") else "minor",
                f"{ef.FIELD_LABELS[key]}이 다릅니다(입력 {p} / 문서 {d}).", str(p), d)

    c = val("consent_plan")
    if c and getattr(protocol, "consent_plan", None) and c != protocol.consent_plan:
        names = {"obtain": "동의 취득", "waiver": "동의 면제"}
        add("consent_plan", "major", f"동의 방식이 다릅니다(입력 {names.get(protocol.consent_plan, protocol.consent_plan)} / "
            f"문서 {names.get(c, c)}).", protocol.consent_plan, c)

    for key, catalog, sev in (("identifiers_collected", "identifiers", "major"),
                              ("data_sources", "data_sources", "minor"),
                              ("sensitive_categories", "sensitive_categories", "major"),
                              ("vulnerable_populations", "vulnerable_populations", "major")):
        extra = _norm_list(val(key)) - _norm_list(getattr(protocol, key, []))
        if extra:
            labels = ", ".join(rs.label(catalog, x) for x in sorted(extra))
            add(key, sev, f"문서에는 있으나 과제 입력에 없는 {ef.FIELD_LABELS[key]}: {labels}", "", sorted(extra))

    for key in ("external_transfer", "international_transfer", "data_linkage", "ai_ml_development"):
        if val(key) is True and not getattr(protocol, key, False):
            add(key, "major", f"문서에 '{ef.FIELD_LABELS[key]}' 관련 내용이 있으나 과제 입력은 '아니오'입니다.", False, True)

    r = val("retention_years")
    if isinstance(r, int) and getattr(protocol, "retention_years", None) and r != protocol.retention_years:
        add("retention_years", "minor", f"보관 기간이 다릅니다(입력 {protocol.retention_years}년 / 문서 {r}년).",
            protocol.retention_years, r)

    title = val("title")
    if title and getattr(protocol, "title", "") and _similar(title, protocol.title) < 0.5:
        add("title", "minor", "연구 제목이 문서와 다릅니다.", protocol.title, title)
    return issues


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, ef.norm(a), ef.norm(b)).ratio()


def step_consistency(ctx: _Context) -> dict[str, Any]:
    if ctx.protocol is None:
        raise Skip("연결된 과제가 없습니다.")
    if not ctx.fields:
        raise Skip("추출된 정보가 없어 비교하지 않습니다.")
    if ctx.extracted and ctx.extracted.method == "protocol_fields":
        raise Skip("첨부 계획서가 없어 비교할 대상이 없습니다.")
    issues = compare_protocol(ctx.protocol, ctx.fields)
    ctx.issues = issues
    if not issues:
        return {"_message": "과제 입력값과 문서 내용이 일치합니다.", "issues": []}
    return {"_message": f"불일치 {len(issues)}건", "issues": issues}


# ---------------------------------------------------------------------- ⑦ 사전검토
def draft_protocol(fields: dict[str, dict[str, Any]]) -> Protocol:
    """추출값으로 만든 저장하지 않는 임시 과제(사전검토·유사 과제 검색용)."""
    data = external_to_protocol(fields)
    data.pop("custom_fields", None)
    p = Protocol(title=data.get("title") or "(제목 없음)")
    for key, value in data.items():
        if key in PROTOCOL_FIELDS:
            setattr(p, key, value)
    return p


def _facts_from_fields(fields: dict[str, dict[str, Any]]) -> ProtocolFacts:
    data = {k: v for k, v in external_to_protocol(fields).items() if k in PROTOCOL_FIELDS}
    return ProtocolFacts.from_mapping(data, submission_kind="initial", uploaded_doc_types={"protocol"})


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_type": result.get("review_type"),
        "review_type_label": result.get("review_type_label"),
        "risk": result.get("risk", {}),
        "ready_for_review": result.get("ready_for_review"),
        "findings": [{k: f.get(k, "") for k in ("severity", "severity_label", "code", "message", "recommendation", "field")}
                     for f in result.get("findings", [])],
        "next_steps": result.get("next_steps", []),
    }


def step_prereview(ctx: _Context) -> dict[str, Any]:
    if ctx.run.kind == "import":
        if not ctx.fields:
            raise Skip("추출된 정보가 없습니다.")
        result = run_prereview(_facts_from_fields(ctx.fields))
    elif ctx.protocol is not None:
        result = wf.preview(ctx.db, ctx.protocol)
    else:
        raise Skip("연결된 과제가 없습니다.")
    brief = _brief(result)
    ctx.prereview = brief
    ctx.findings = brief["findings"]
    return {"_message": f"{brief['review_type_label']} 예상 · 지적 {len(brief['findings'])}건", **brief}


# ---------------------------------------------------------------------- ⑧ 관련 정보 검색
def _knowledge():
    pkg = __package__.rsplit(".", 1)[0]
    try:
        return importlib.import_module(f"{pkg}.knowledge.service")
    except ImportError:
        return None


def step_related(ctx: _Context) -> dict[str, Any]:
    ks = _knowledge()
    if ks is None:
        raise Skip("지식베이스 모듈이 설치되어 있지 않습니다.")
    findings = ctx.findings or [
        {"code": f"consistency:{i['field']}", "message": i["message"], "severity": i["severity"]} for i in ctx.issues
    ]
    related: dict[str, list] = {}
    if findings:
        related = ks.related_for_findings(ctx.db, findings, k=3) or {}
    protocol = ctx.protocol if ctx.run.kind != "import" else (draft_protocol(ctx.fields) if ctx.fields else None)
    similar = ks.similar_protocols(ctx.db, protocol, k=5) if protocol is not None else []
    if protocol is not None and getattr(protocol, "id", None):
        similar = [s for s in similar if s.get("protocol_id", s.get("id")) != protocol.id]
    n = sum(len(v) for v in related.values())
    return {"_message": f"관련 근거 {n}건 · 유사 과제 {len(similar)}건", "related": related, "similar": similar}


# ---------------------------------------------------------------------- ⑨ AI 요약
AI_SUMMARY_SYSTEM = """당신은 대한민국 병원 기관생명윤리위원회(IRB) 사무국의 서류 검토 보조자입니다.
제출된 문서와 자동 점검 결과를 읽고 간사·심의위원이 빠르게 파악할 수 있도록 정리합니다.
- 문서에 근거가 있는 내용만 쓰고 추측하지 마십시오.
- 자동 점검(규칙 엔진, 정합성)이 이미 찾은 내용은 반복하지 말고, 서술상의 모순·누락에 집중하십시오.
- 승인·부결 결론을 내리지 마십시오. 모든 응답은 한국어로 작성하십시오."""
AI_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "문서 요약 3~5문장"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "concerns": {"type": "array", "items": {"type": "string"}, "description": "검토 시 확인이 필요한 사항"},
        "missing_items": {"type": "array", "items": {"type": "string"}, "description": "문서에서 빠진 것으로 보이는 항목"},
        "questions_for_researcher": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points", "concerns", "missing_items", "questions_for_researcher"],
    "additionalProperties": False,
}


def step_ai_summary(ctx: _Context) -> dict[str, Any]:
    if not ctx.use_ai:
        raise Skip("AI 기능이 비활성화되어 있습니다.")
    text = _need_text(ctx)
    guarded = privacy.mask_text(text[:120000])
    checks = {
        "consistency_issues": [i["message"] for i in ctx.issues],
        "rule_findings": [f["message"] for f in ctx.findings][:30],
        "classified_as": ctx.classified,
    }
    import json

    content = [
        ai.text_document_block(guarded.text, ctx.run.input_name or "문서"),
        {"type": "text", "text": "<automated_checks>\n" + json.dumps(checks, ensure_ascii=False, indent=2)
         + "\n</automated_checks>\n위 문서를 검토하여 지정된 JSON 형식으로 응답하십시오."},
    ]
    try:
        data, response = ai.structured_call_with_response(
            system=AI_SUMMARY_SYSTEM, content=content, schema=AI_SUMMARY_SCHEMA, effort="medium",
            purpose="문서 검토 요약",
        )
    except ai.AIUnavailable as e:
        raise Skip(str(e)) from e
    ctx.transmit("문서 검토 요약", guarded.text, "text", guarded.masked)
    data["model"] = getattr(response, "model", "")
    data["_message"] = "AI 요약 완료(참고용)"
    return data


# ---------------------------------------------------------------------- ⑩ 기관 서식 제안
def step_forms(ctx: _Context) -> dict[str, Any]:
    from ..documents import GENERATABLE

    if not ctx.fields:
        raise Skip("추출된 정보가 없습니다.")
    rs = get_ruleset()
    facts = _facts_from_fields(ctx.fields)
    suggestions = [{"doc_type": "application", "label": rs.label("document_types", "application"),
                    "reason": "모든 신규 과제에 필요"}]
    if facts.consent_plan == "waiver":
        suggestions.append({"doc_type": "waiver_request", "label": rs.label("document_types", "waiver_request"),
                            "reason": "문서에 동의 면제 신청 내용이 있음"})
    if facts.deid_level == "pseudonymized" or facts.identifiers_collected:
        suggestions.append({"doc_type": "deid_plan", "label": rs.label("document_types", "deid_plan"),
                            "reason": "가명처리 또는 식별자 수집"})
    if facts.data_items:
        suggestions.append({"doc_type": "crf", "label": rs.label("document_types", "crf"), "reason": "수집 항목이 기재됨"})
    suggestions.append({"doc_type": "security_pledge", "label": rs.label("document_types", "security_pledge"),
                        "reason": "의료데이터 연구 공통"})
    if facts.multicenter:
        suggestions.append({"doc_type": "site_list", "label": rs.label("document_types", "site_list"),
                            "reason": "다기관 연구"})
    suggestions = [s for s in suggestions if s["doc_type"] in GENERATABLE]
    return {"_message": f"과제 생성 후 자동 작성할 서식 {len(suggestions)}종", "suggestions": suggestions}


STEP_HANDLERS = {
    "recognize": step_recognize,
    "classify": step_classify,
    "pii": step_pii,
    "extract": step_extract,
    "convert": step_convert,
    "consistency": step_consistency,
    "prereview": step_prereview,
    "related": step_related,
    "ai_summary": step_ai_summary,
    "forms": step_forms,
}


def _summary(ctx: _Context) -> dict[str, Any]:
    return {
        "chars": ctx.extracted.chars if ctx.extracted else 0,
        "classified": ctx.classified,
        "fields": len(ctx.fields),
        "pii": ctx.pii,
        "issues": len(ctx.issues),
        "review_type": (ctx.prereview or {}).get("review_type_label"),
        "ai": ctx.use_ai,
    }


# ====================================================================== 조회·적용
def can_view_run(db: Session, user: User, run: PipelineRun) -> bool:
    if user.is_admin:
        return True
    if run.protocol_id:
        protocol = db.get(Protocol, run.protocol_id)
        return protocol is not None and wf.can_view(db, user, protocol)
    return run.created_by == user.id


def runs_for_protocol(db: Session, protocol_id: int, limit: int = 30) -> list[PipelineRun]:
    return list(db.scalars(select(PipelineRun).where(PipelineRun.protocol_id == protocol_id)
                           .order_by(PipelineRun.id.desc()).limit(limit)))


def latest_fields(run: PipelineRun) -> dict[str, dict[str, Any]]:
    step = run.step("extract")
    return dict((step.result or {}).get("fields", {})) if step and step.status == "succeeded" else {}


def run_payload(run: PipelineRun) -> dict[str, Any]:
    return {
        "id": run.id, "kind": run.kind, "status": run.status, "status_label": STATUS_LABELS.get(run.status, run.status),
        "active": run.active, "protocol_id": run.protocol_id, "document_id": run.document_id,
        "input_name": run.input_name, "error": run.error, "summary": run.summary or {},
        "steps": [{"seq": s.seq, "name": s.name, "label": STEP_LABELS[s.name], "status": s.status,
                   "status_label": STATUS_LABELS.get(s.status, s.status), "message": s.message,
                   "duration_ms": s.duration_ms} for s in run.steps],
    }


def apply_import(db: Session, user: User, run: PipelineRun, selected: set[str], ip: str = "") -> Protocol:
    if run.kind != "import" or run.active:
        raise wf.WorkflowError("완료된 가져오기 실행만 적용할 수 있습니다.")
    if run.protocol_id:
        raise wf.WorkflowError("이미 과제가 생성된 가져오기입니다.")
    data = external_to_protocol(latest_fields(run), selected)
    custom = data.pop("custom_fields", None)
    protocol = wf.create_protocol(db, user, data, ip)
    if custom and hasattr(protocol, "custom_fields"):
        protocol.custom_fields = {**(protocol.custom_fields or {}), **custom}
    src = Path(run.input_path)
    if src.is_file():
        folder = get_settings().upload_dir / str(protocol.id)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{uuid.uuid4().hex}_{safe_name(run.input_name)}"
        shutil.copyfile(src, dest)
        doc = Document(protocol_id=protocol.id, doc_type="protocol", filename=run.input_name, stored_path=str(dest),
                       sha256=run.input_sha256, size=dest.stat().st_size, version=1, uploaded_by=user.id)
        db.add(doc)
        db.flush()
        run.document_id = doc.id
    run.protocol_id = protocol.id
    audit.record(db, user, "pipeline_apply", "protocol", protocol.id,
                 {"run_id": run.id, "mode": "import", "fields": sorted(selected)}, ip)
    return protocol


def _is_empty(value: Any) -> bool:
    return value in (None, "", 0, [], {}) or value is False


def apply_to_protocol(
    db: Session, user: User, protocol: Protocol, run: PipelineRun, selected: set[str], mode: str = "fill", ip: str = ""
) -> list[str]:
    """추출 제안을 기존 과제에 반영. mode: fill(빈 칸만) | overwrite(덮어쓰기). 반영된 항목 목록."""
    data = external_to_protocol(latest_fields(run), selected)
    custom = data.pop("custom_fields", None)
    if mode != "overwrite":
        data = {k: v for k, v in data.items() if _is_empty(getattr(protocol, k, None))}
    wf.update_protocol(db, user, protocol, data, ip)
    applied = sorted(data)
    if custom and hasattr(protocol, "custom_fields"):
        current = dict(protocol.custom_fields or {})
        for k, v in custom.items():
            if mode == "overwrite" or _is_empty(current.get(k)):
                current[k] = v
                applied.append(f"custom:{k}")
        protocol.custom_fields = current
    audit.record(db, user, "pipeline_apply", "protocol", protocol.id,
                 {"run_id": run.id, "mode": mode, "fields": applied}, ip)
    return applied


def seed_pipeline(db: Session) -> int:
    """데모(멱등): 연구자 계정으로 '다른 병원 양식' 샘플 계획서 가져오기 1건을 동기 실행한다."""
    if db.scalars(select(PipelineRun).where(PipelineRun.kind == "import")).first():
        return 0
    user = db.scalars(select(User).where(User.username == "researcher")).first()
    if user is None:
        return 0
    try:
        import sys

        samples_dir = str(Path(__file__).resolve().parents[2] / "samples")
        if samples_dir not in sys.path:
            sys.path.insert(0, samples_dir)
        import generate_documents as samples
    except ImportError:
        return 0
    folder = get_settings().pipeline_dir / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    raw = samples.build_docx()
    path = folder / f"demo_{uuid.uuid4().hex}_hanbit_protocol.docx"
    path.write_bytes(raw)
    run = _create_run(db, user, "import", input_name="한빛대병원_연구계획서.docx", input_path=str(path),
                      input_sha256=hashlib.sha256(raw).hexdigest())
    db.commit()
    run_pipeline(run.id)
    db.expire_all()
    return 1


__all__ = [
    "start_document_pipeline", "start_import_pipeline", "start_protocol_pipeline", "run_pipeline",
]
