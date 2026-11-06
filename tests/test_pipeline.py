"""문서 자동화 파이프라인: 텍스트 인식·분류·추출·변환·PII 가드·AI 클라이언트·파이프라인."""
from __future__ import annotations

import json
import sys
import types
import zlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "samples"))
import generate_documents as samples  # noqa: E402

from irb_automation.ai import client as ai  # noqa: E402
from irb_automation.ai import privacy  # noqa: E402
from irb_automation.docproc import convert  # noqa: E402
from irb_automation.docproc.classify import classify_document  # noqa: E402
from irb_automation.docproc.extract_fields import extract_custom_fields, extract_fields, merge_fields, parse_period  # noqa: E402
from irb_automation.docproc.text import decode_para_text, detect_format, extract_text, iter_hwp_records  # noqa: E402


# ====================================================================== 가짜 Claude 클라이언트
class _CM:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0) if self.responses else self.default(kwargs)
        if isinstance(r, Exception):
            raise r
        return r

    def stream(self, **kwargs):
        response = self.create(**{**kwargs, "_stream": True})
        cm = SimpleNamespace(get_final_message=lambda: response)
        return _CM(cm)

    def default(self, kwargs):
        schema = (kwargs.get("output_config") or {}).get("format", {}).get("schema")
        props = (schema or {}).get("properties", {})
        if "summary" in props:
            return msg(json.dumps({"summary": "요약", "key_points": ["a"], "concerns": ["b"], "missing_items": [],
                                   "questions_for_researcher": []}))
        if "doc_type" in props:
            return msg(json.dumps({"doc_type": "protocol", "confidence": 0.9, "reason": "계획서"}))
        if schema:
            data = {k: _empty(v) for k, v in props.items()}
            data.update({"expected_subjects": 1300, "evidence": [{"field": "expected_subjects", "quote": "약 1,200명"}]})
            return msg(json.dumps(data))
        return msg("OCR 텍스트")


def _empty(spec):
    t = spec.get("type")
    return {"string": "", "integer": 0, "boolean": False, "array": [], "number": 0}.get(t, "")


def msg(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture()
def fake_ai(settings):
    settings.ai_review_enabled = True
    messages = FakeMessages([])
    ai.set_client_factory(lambda: SimpleNamespace(beta=SimpleNamespace(messages=messages)))
    yield messages
    ai.set_client_factory(None)
    settings.ai_review_enabled = False


# ====================================================================== 텍스트 인식
def test_extract_formats():
    for name, builder in samples.SAMPLES.items():
        raw = builder()
        r = extract_text(name, raw)
        assert r.chars > 200, (name, r)
        assert "연구" in r.text
    assert detect_format("x.hwp", samples.build_hwp5()) == "hwp"
    assert "연구대상자 수: 500명" in extract_text("a.hwp", samples.build_hwp5()).text
    assert "500명" in extract_text("a.hwp", samples.build_hwp5(compressed=False)).text
    hwpx = extract_text("b.hwpx", samples.build_hwpx()).text
    assert "연구책임자 | 박소아 (소아청소년과)" in hwpx  # 표 행
    docx = extract_text("c.docx", samples.build_docx()).text
    assert "# 1. 연구 목적" in docx and "연구과제명 |" in docx


def test_hwp_record_parser_units():
    payload = "가나".encode("utf-16-le") + (11).to_bytes(2, "little") + b"\x00" * 14 + "다\r".encode("utf-16-le")
    assert decode_para_text(payload) == "가나다"
    body = samples.hwp_body(["첫 문단", "둘째 문단"])
    texts = [decode_para_text(p).strip() for t, _lv, p in iter_hwp_records(body) if t == 67]
    assert texts == ["첫 문단", "둘째 문단"]
    big = samples._hwp_record(67, "가".encode("utf-16-le") * 3000)  # 확장 크기 헤더
    (tag, _lv, p), = list(iter_hwp_records(big))
    assert tag == 67 and len(p) == 6000
    assert zlib.decompressobj(-15)  # 압축 해제기 사용 가능


def test_distribution_and_scanned_warnings():
    r = extract_text("d.hwp", samples.build_hwp5(distribution=True))
    assert r.method == "hwp:preview" and "배포용" in r.warnings[0]
    blank = extract_text("s.pdf", samples.build_blank_pdf())
    assert any("스캔본" in w for w in blank.warnings)
    r = extract_text("p.html", "<html><body><h1>연구 목적</h1><p>테스트&amp;확인</p><script>x()</script></body></html>".encode())
    assert "테스트&확인" in r.text and "x()" not in r.text
    r = extract_text("t.txt", "연구 제목: 한글 CP949".encode("cp949"))
    assert "한글" in r.text
    assert extract_text("x.bin", b"\x00\x01\x02").warnings


def test_ai_ocr_for_scanned_pdf(fake_ai):
    r = extract_text("s.pdf", samples.build_blank_pdf(), allow_ai_ocr=True)
    assert r.method == "ai_ocr" and r.text == "OCR 텍스트"
    assert fake_ai.calls[0]["_stream"] is True  # max_tokens 32000 → 스트리밍
    block = fake_ai.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"


# ====================================================================== 분류·추출
def test_classify():
    for name, builder in samples.SAMPLES.items():
        doc_type, conf, reason = classify_document(name, extract_text(name, builder()).text)
        assert doc_type in ("protocol", "application"), (name, doc_type, reason)
        assert conf >= 0.5
    assert classify_document("cv.docx", "이력서\n학력\n경력\n주요 논문")[0] == "cv"
    assert classify_document("x.txt", "보안서약서\n비밀유지 의무를 준수할 것을 서약합니다")[0] == "security_pledge"
    assert classify_document("x.txt", "")[0] == "other"


def test_extract_heading_variants():
    f = extract_fields(extract_text("a.docx", samples.build_docx()).text)
    assert f["title"]["value"].startswith("만성 신장병")
    assert f["expected_subjects"]["value"] == 1200
    assert (f["data_period_start"]["value"], f["data_period_end"]["value"]) == ("2018-01-01", "2023-12-31")
    assert f["study_end"]["value"] == "2027-12-31" and "study_start" not in f  # 'IRB 승인일 ~'
    assert f["consent_plan"]["value"] == "waiver"
    assert "large_population" in f["waiver_impracticable_reasons"]["value"]
    assert f["retention_years"]["value"] == 3
    assert "cdw" in f["data_sources"]["value"]
    assert "identifiers_collected" not in f  # '주민등록번호와 성명은 수집하지 않는다' (부정)
    assert f["objectives"]["source_snippet"]

    h = extract_fields(extract_text("b.hwpx", samples.build_hwpx()).text)  # Ⅰ. ① 가. 체계
    assert h["title"]["value"].startswith("소아 천식")
    assert h["expected_subjects"]["value"] == 350
    assert (h["study_start"]["value"], h["study_end"]["value"]) == ("2026-03-01", "2027-02-28")
    assert h["consent_plan"]["value"] == "obtain"
    assert "children" in h["vulnerable_populations"]["value"]
    assert h["exclusion_criteria"]["value"].startswith("선천성")  # '배제 기준'

    d = extract_fields(extract_text("c.hwp", samples.build_hwp5()).text)
    assert d["study_type"]["value"] == "imaging_ai" and d["ai_ml_development"]["value"] is True

    text = "제1조 과제명\n가상 과제\n제2조 목표 증례 수\n총 2,500례\n(1) 의무기록 열람 기간: 2019.01 ~ 2020.06\n③ 동의면제 사유: 사망 등으로 추적 불가"
    g = extract_fields(text)
    assert g["title"]["value"] == "가상 과제" and g["expected_subjects"]["value"] == 2500
    assert g["data_period_end"]["value"] == "2020-06-30"
    assert g["consent_plan"]["value"] == "waiver"


def test_parse_period_and_custom_fields():
    assert parse_period("2020년 3월 ~ 2021년") == (date(2020, 3, 1), date(2021, 12, 31))
    assert parse_period("승인일로부터 2026-12-31까지") == (None, date(2026, 12, 31))
    custom = extract_custom_fields("연구비 과제번호 | HI-2026-01\n임상시험 등록번호: KCT0001", [
        {"key": "grant_no", "label": "연구비 과제번호"}, {"key": "cris", "label": "임상시험 등록번호"}])
    assert custom["custom:grant_no"]["value"] == "HI-2026-01" and custom["custom:cris"]["value"] == "KCT0001"


def test_merge_and_external_to_protocol():
    rule = {"expected_subjects": {"value": 1200, "confidence": 0.9, "source_snippet": "", "method": "section"},
            "title": {"value": "A", "confidence": 0.4, "source_snippet": "", "method": "first_line"}}
    aif = {"expected_subjects": {"value": 1200, "confidence": 0.8, "source_snippet": "", "method": "ai"},
           "title": {"value": "B", "confidence": 0.8, "source_snippet": "", "method": "ai"}}
    m = merge_fields(rule, aif)
    assert m["expected_subjects"]["confidence"] > 0.9 and m["title"]["value"] == "B" and m["title"]["alternative"] == "A"
    data = convert.external_to_protocol({
        "study_end": {"value": "2027-12-31"}, "data_sources": {"value": ["emr", "bogus"]},
        "study_type": {"value": "nope"}, "custom:x": {"value": "1"}, "expected_subjects": {"value": "12"},
    })
    assert data["study_end"] == date(2027, 12, 31) and data["data_sources"] == ["emr"]
    assert "study_type" not in data and data["custom_fields"] == {"x": "1"} and data["expected_subjects"] == 12


def test_markdown_and_pdf():
    ex = extract_text("a.docx", samples.build_docx())
    md = convert.to_markdown(ex, "제목")
    assert md.startswith("# 제목") and "## 1. 연구 목적" in md and "| 연구과제명 |" in md
    pdf = convert.to_pdf("a.hwpx", samples.build_hwpx())  # LibreOffice가 HWPX를 못 읽어도 텍스트 PDF
    assert pdf and pdf.startswith(b"%PDF")
    assert "소아 천식" in extract_text("x.pdf", pdf).text
    assert convert.to_pdf("x.pdf", b"%PDF-1.4 x") == b"%PDF-1.4 x"


# ====================================================================== PII 가드
def test_privacy_mask_and_binary_policy(settings):
    text = "환자 홍길동 환자, 주민번호 900101-1234567, 연락처 010-1234-5678, 연구기간 2020-01-01 ~ 2021-12-31"
    g = privacy.mask_text(text)
    assert g.has_pii and "900101-1234567" not in g.text and "010-1234-5678" not in g.text
    assert "2020-01-01" in g.text and "2021-12-31" in g.text  # 날짜 보존
    assert {"rrn", "mobile", "name"} <= set(g.pii)
    assert privacy.mask_text("연구 목적 기술").masked == 0
    ok, _ = privacy.binary_allowed("protocol", "연구 목적만 있음")
    assert ok
    ok, reason = privacy.binary_allowed("protocol", text)
    assert not ok and "개인정보" in reason
    ok, _ = privacy.binary_allowed("consent_form", "")
    assert not ok


# ====================================================================== AI 클라이언트
def test_ai_client_parameters_and_errors(fake_ai):
    fake_ai.responses = [msg(json.dumps({"a": 1}))]
    out = ai.structured_call(system="s", content="c", schema={"type": "object"}, purpose="t")
    assert out == {"a": 1}
    call = fake_ai.calls[-1]
    assert call["betas"] == [ai.FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema" and call["output_config"]["effort"] == "high"
    assert "thinking" not in call and call["model"] == "claude-opus-5-5"

    fake_ai.responses = [msg("", stop="refusal")]
    with pytest.raises(ai.AIError, match="응답하지"):
        ai.text_call(system="s", content="c")
    fake_ai.responses = [msg("잘림", stop="max_tokens")]
    with pytest.raises(ai.AIError, match="길이"):
        ai.text_call(system="s", content="c")
    fake_ai.responses = [msg("not json")]
    with pytest.raises(ai.AIError, match="형식"):
        ai.structured_call(system="s", content="c", schema={"type": "object"})
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_ai.responses = [anthropic.RateLimitError("rl", response=httpx.Response(429, request=req), body=None)]
    with pytest.raises(ai.AIError, match="한도"):
        ai.text_call(system="s", content="c")
    fake_ai.responses = [anthropic.AuthenticationError("auth", response=httpx.Response(401, request=req), body=None)]
    with pytest.raises(ai.AIUnavailable):
        ai.text_call(system="s", content="c")
    assert ai.recent_usage()[0]["ok"] is False
    assert ai.text_document_block("t", "제목", citations=True)["citations"] == {"enabled": True}
    with pytest.raises(ai.AIError):
        ai.image_block(b"x", "image/bmp")


def test_ai_disabled(settings):
    assert not ai.ai_enabled()
    with pytest.raises(ai.AIUnavailable):
        ai.text_call(system="s", content="c")
    from irb_automation.engine.ai_review import run_ai_review

    assert run_ai_review({}, {}) is None


def test_ai_review_uses_shared_client(fake_ai):
    from irb_automation.engine.ai_review import run_ai_review

    fake_ai.responses = [msg(json.dumps({"summary": "s", "consistency_issues": [], "data_minimization": [],
                                         "questions_for_researcher": []}))]
    out = run_ai_review({"title": "t", "objectives": "연락처 010-1111-2222"}, {"findings": []})
    assert out["summary"] == "s" and out["model"] == "claude-opus-5-5" and out["masked"] >= 1
    sent = fake_ai.calls[-1]["messages"][0]["content"][0]["text"]
    assert "010-1111-2222" not in sent
    fake_ai.responses = [msg("", stop="refusal")]
    assert "error" in run_ai_review({}, {"findings": []})


# ====================================================================== 파이프라인
def _sync(settings):
    settings.pipeline_sync = True


def _attach(db, protocol, user, name, raw, doc_type="protocol"):
    from irb_automation.config import get_settings
    from irb_automation.models import Document

    folder = get_settings().upload_dir / str(protocol.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(raw)
    import hashlib

    doc = Document(protocol_id=protocol.id, doc_type=doc_type, filename=name, stored_path=str(path),
                   sha256=hashlib.sha256(raw).hexdigest(), size=len(raw), version=1, uploaded_by=user.id)
    db.add(doc)
    db.flush()
    return doc


def test_document_pipeline_consistency_and_pii(settings, db, users):
    from irb_automation.models import Notification, Protocol
    from irb_automation.pipeline import service as ps
    from irb_automation.pipeline.models import PipelineRun

    _sync(settings)
    r = users["researcher"]
    p = Protocol(title="만성 신장병 환자에서 SGLT2 억제제 사용과 고칼륨혈증", pi_id=r.id, expected_subjects=500,
                 consent_plan="obtain", data_period_start=date(2018, 1, 1), data_period_end=date(2022, 12, 31))
    db.add(p)
    db.flush()
    doc = _attach(db, p, r, "plan.docx", samples.build_docx())
    run = ps.start_document_pipeline(db, r, doc.id)
    run = db.get(PipelineRun, run.id)
    db.refresh(run)
    assert run.status == "succeeded", [(s.name, s.status, s.message) for s in run.steps]
    status = {s.name: s.status for s in run.steps}
    assert status["ai_summary"] == "skipped" and status["related"] == "skipped"  # AI 꺼짐, 지식베이스 없음
    issues = {i["field"] for i in run.step("consistency").result["issues"]}
    assert {"expected_subjects", "consent_plan", "data_period_end"} <= issues
    assert run.step("classify").result["doc_type"] == "protocol"
    assert Path(run.step("convert").result["markdown_path"]).exists()

    # CRF 안에 실제 환자 주민번호 → 사무국 알림
    crf = _attach(db, p, r, "crf.txt", "증례기록서\n환자 900101-1234567 010-2222-3333\n".encode(), "crf")
    run2 = ps.start_document_pipeline(db, r, crf.id)
    db.refresh(run2)
    assert run2.step("pii").result["severe"] is True
    assert run2.step("extract").status == "skipped"
    assert db.query(Notification).filter(Notification.message.contains("개인정보 주의")).count() >= 1

    # 같은 파일 재분석 시 인식 캐시 사용
    run3 = ps.start_protocol_pipeline(db, r, p.id)
    db.refresh(run3)
    assert run3.step("recognize").result.get("cached") is True
    assert run3.step("prereview").status == "succeeded"


def test_pipeline_auto_disabled(settings, db, users):
    from irb_automation.pipeline import service as ps

    settings.ai_pipeline_auto = False
    assert ps.start_document_pipeline(db, users["researcher"], 1) is None


def test_pipeline_with_ai_and_knowledge(settings, db, users, fake_ai, monkeypatch):
    from irb_automation.models import AuditLog
    from irb_automation.pipeline import service as ps

    _sync(settings)
    fake_kb = types.SimpleNamespace(
        related_for_findings=lambda db, findings, k=3: {f["code"]: [{"title": "가이드라인", "snippet": "근거"}] for f in findings},
        similar_protocols=lambda db, protocol, k=5: [{"protocol_id": 999, "title": "유사 과제", "score": 0.8}],
    )
    monkeypatch.setattr(ps, "_knowledge", lambda: fake_kb)
    run = ps.start_import_pipeline(db, users["researcher"], "hanbit.docx", samples.build_docx())
    db.refresh(run)
    status = {s.name: (s.status, s.message) for s in run.steps}
    assert all(v[0] == "succeeded" for v in status.values()), status
    fields = ps.latest_fields(run)
    assert fields["expected_subjects"]["value"] == 1200  # 규칙(0.9) 우선, AI 1300은 대안
    assert fields["expected_subjects"].get("alternative") == 1300
    assert run.step("ai_summary").result["summary"] == "요약"
    assert run.step("related").result["similar"][0]["title"] == "유사 과제"
    assert run.step("forms").result["suggestions"]
    sent = db.query(AuditLog).filter(AuditLog.action == "ai_transmit").all()
    assert sent and all("sha256" in a.details and "text" not in a.details for a in sent)


def test_import_apply_creates_protocol(settings, db, users):
    from irb_automation.pipeline import service as ps

    _sync(settings)
    r = users["researcher"]
    run = ps.start_import_pipeline(db, r, "saesol.hwpx", samples.build_hwpx())
    db.refresh(run)
    assert run.status == "succeeded"
    protocol = ps.apply_import(db, r, run, {"title", "expected_subjects", "consent_plan", "study_start"})
    db.commit()
    assert protocol.title.startswith("소아 천식") and protocol.expected_subjects == 350
    assert protocol.consent_plan == "obtain" and protocol.study_start == date(2026, 3, 1)
    assert protocol.status.value == "draft"
    assert [d.doc_type for d in protocol.documents] == ["protocol"]
    from irb_automation.workflow.service import WorkflowError

    with pytest.raises(WorkflowError):
        ps.apply_import(db, r, run, {"title"})

    # 기존 과제에 빈 칸만 / 덮어쓰기
    protocol.inclusion_criteria = ""
    protocol.expected_subjects = 10
    applied = ps.apply_to_protocol(db, r, protocol, run, {"inclusion_criteria", "expected_subjects"}, "fill")
    assert applied == ["inclusion_criteria"] and protocol.expected_subjects == 10
    ps.apply_to_protocol(db, r, protocol, run, {"expected_subjects"}, "overwrite")
    assert protocol.expected_subjects == 350


def test_background_dispatch_after_commit(settings, db, users):
    import time as _t

    from irb_automation.database import SessionLocal
    from irb_automation.pipeline import service as ps
    from irb_automation.pipeline.models import PipelineRun

    run = ps.start_import_pipeline(db, users["researcher"], "daon.hwp", samples.build_hwp5())
    rid = run.id
    assert run.status == "queued"
    db.commit()  # 커밋 후 백그라운드 스레드에서 실행
    status = None
    for _ in range(100):
        with SessionLocal() as s:
            status = s.get(PipelineRun, rid).status
        if status in ("succeeded", "partial", "failed"):
            break
        _t.sleep(0.1)
    assert status == "succeeded"


def test_seed_and_ruleset_aliases(settings, db, users):
    from irb_automation.pipeline import service as ps

    settings.pipeline_sync = True
    assert ps.seed_pipeline(db) == 1 and ps.seed_pipeline(db) == 0
    f = extract_fields("1. 임상연구과제명\n별칭으로 찾은 과제\n2. 후향적 의무기록 조사기간: 2020.01.01 ~ 2020.12.31")
    assert f["title"]["value"] == "별칭으로 찾은 과제" and f["data_period_end"]["value"] == "2020-12-31"
