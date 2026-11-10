"""관련 정보 검색(지식베이스·근거 인용 Q&A)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.database import SessionLocal
from irb_automation.knowledge import index as ix
from irb_automation.knowledge import service as ks
from irb_automation.knowledge import sources as kns
from irb_automation.knowledge.models import KnowledgeQuery, KnowledgeSource
from irb_automation.models import Protocol, ProtocolStatus
from irb_automation.workflow import service as wf

from .conftest import csrf, good_facts, login


# ------------------------------------------------------------------ 토크나이저·하이라이트
def test_article_normalization_and_bigrams():
    assert ix.tokenize("제28조의2") == ix.tokenize("28조의2") == ix.tokenize("제 28 조의 2") == ["a28_2"]
    assert ix.tokenize("제16조제3항") == ["a16", "a16p3"]
    assert ix.tokenize("16조 3항") == ["a16", "a16p3"]
    toks = ix.tokenize("가명정보 RRN_COLLECTION 처리합니다")
    assert {"가명", "명정", "정보", "rrn_collection", "rrn", "collection"} <= set(toks)
    assert "니다" not in toks and "합니" not in toks  # 어미 제외


def test_highlight_escapes_html():
    out = str(ix.highlight("<script>alert(1)</script> 가명정보 처리", ["가명정보"]))
    assert "&lt;script&gt;" in out and "<mark>가명정보</mark>" in out and "<script>" not in out
    out = str(ix.highlight("개인정보 보호법 제 28 조의 2에 따라", ix.query_terms("28조의2")))
    assert "<mark>" in out


def test_query_terms_strip_particles():
    assert "가명정보" in ix.query_terms("가명정보는 동의가 필요한가")


def test_split_into_chunks_by_headings():
    text = "제1조(목적) 이 지침은 반출 심사 절차를 정한다.\n\n제2조(최소 셀) 최소 셀 크기는 10으로 한다.\n\n## 부칙\n시행일은 공포일로 한다."
    chunks = kns.split_into_chunks(text, "SOP")
    heads = [h for h, _ in chunks]
    assert heads[0].startswith("제1조") and any(h.startswith("제2조") for h in heads) and "부칙" in heads
    long = "가" * 3000
    assert len(kns.split_into_chunks(long, "x")) >= 3


# ------------------------------------------------------------------ 내장 지식·검색 순위
def test_seed_is_idempotent_and_versioned(db, monkeypatch):
    first = kns.seed_knowledge(db)
    assert first["created"] > 40
    again = kns.seed_knowledge(db)
    assert again["created"] == 0 and again["updated"] == 0
    docs = kns.builtin_documents()
    docs[0]["version"] = "changed"
    monkeypatch.setattr(kns, "builtin_documents", lambda: docs)
    assert kns.seed_knowledge(db)["updated"] == 1


@pytest.mark.parametrize("query,expected_ref", [
    ("제28조의2 가명정보", "개인정보 보호법 제28조의2"),
    ("28조의2", "개인정보 보호법 제28조의2"),
    ("제 24 조의 2 주민등록번호 처리 제한", "개인정보 보호법 제24조의2"),
    ("개인정보 보호법 제28조의3 결합", "개인정보 보호법 제28조의3"),
])
def test_article_query_ranks_matching_law_first(db, query, expected_ref):
    hits = ks.search(db, query, k=5, kinds=["law"])
    assert hits and hits[0]["ref"] == expected_ref, [h["ref"] for h in hits]


def test_semantic_queries_find_relevant_sources(db):
    refs = [h["ref"] for h in ks.search(db, "동의 면제 요건", k=5)]
    assert any("제16조" in r for r in refs)
    kinds = [h["kind"] for h in ks.search(db, "결과 반출 소규모 셀", k=3)]
    assert "guideline" in kinds or "faq" in kinds
    hits = ks.search(db, "자유기술 경과기록 스크러빙", k=3)
    assert hits[0]["kind"] in ("finding_guide", "guideline")
    assert ks.search(db, "", k=3) == []
    assert ks.search(db, "가명정보", k=3, kinds=["faq"]) and all(
        h["kind"] == "faq" for h in ks.search(db, "가명정보", k=3, kinds=["faq"]))


def test_hit_has_contract_keys(db):
    h = ks.search(db, "가명정보 결합", k=1)[0]
    assert {"source_id", "chunk_id", "kind", "title", "ref", "snippet", "score", "url"} <= set(h)


# ------------------------------------------------------------------ 선례 색인·권한
def _set_precedents(db, users):
    p1 = db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()
    p2 = db.scalars(select(Protocol).where(Protocol.title.contains("자살"))).one()
    p1.protocol_no = "IRB-T-0001"
    p2.protocol_no = "IRB-T-0002"
    p1.status = p2.status = ProtocolStatus.SUBMITTED
    p2.pi_id = users["researcher2"].id
    p2.co_investigators = []
    db.flush()
    return p1, p2


def test_precedent_visibility_policy(db, users):
    p1, p2 = _set_precedents(db, users)
    q = "정신건강의학과 경과기록 자살 위험 예측 언어모델"
    by_admin = ks.search(db, q, k=5, kinds=["protocol"], user=users["admin"])
    assert any(h["title"].startswith("IRB-T-0002") for h in by_admin)  # 관리자는 기관 선례 전체
    by_pi = ks.search(db, q, k=5, kinds=["protocol"], user=users["researcher2"])
    assert any(h["title"].startswith("IRB-T-0002") for h in by_pi)  # 본인 과제
    by_other = ks.search(db, q, k=5, kinds=["protocol"], user=users["researcher"])
    assert not any(h["title"].startswith("IRB-T-0002") for h in by_other)  # 타 과제 비노출
    assert any(h["kind"] == "law" for h in ks.search(db, "가명정보", k=5, user=users["researcher"]))


def test_decisions_indexed_incrementally(db, users):
    p = db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()
    assert ks.search(db, "SGLT2", k=5, kinds=["protocol"]) == []
    wf.record_submission(db, users["researcher"], p, force=True)
    items = wf.record_revision_request(db, users["researcher"], p, "1. 가명처리 주체를 명시하십시오.")
    wf.respond_revision_items(db, users["researcher"], p, {items[0].id: "데이터관리자가 가명처리함을 명시하였습니다."})
    wf.record_submission(db, users["researcher"], p, force=True)
    wf.record_result(db, users["researcher"], p, "exempt", note="가명정보만 원내 분석")
    hits = ks.search(db, "SGLT2 심의면제 확인 가명처리 주체", k=8, kinds=["decision", "protocol"])
    kinds = {h["kind"] for h in hits}
    assert {"decision", "protocol"} <= kinds
    assert any("심의면제 확인" in h["title"] for h in hits if h["kind"] == "decision")


def test_similar_protocols(db, users):
    p1, p2 = _set_precedents(db, users)
    new = Protocol(title="당뇨병 환자에서 SGLT2 억제제 처방과 신기능 변화", pi_id=users["researcher"].id,
                   objectives="SGLT2 억제제를 처방받은 제2형 당뇨병 환자의 급성 신손상과 eGFR 변화를 분석한다.",
                   methods="CDW 처방 자료를 성향점수 매칭으로 분석한다.", data_sources=["cdw"])
    db.add(new)
    db.flush()
    items = ks.similar_protocols(db, new, k=3)
    assert items and items[0]["protocol_id"] == p1.id
    assert {"protocol_id", "protocol_no", "title", "status", "decision", "score"} <= set(items[0])
    assert all(i["protocol_id"] != new.id for i in items)
    # 자기 자신(색인된 과제)은 결과에서 제외
    assert all(i["protocol_id"] != p1.id for i in ks.similar_protocols(db, p1, k=3))
    # 권한 필터: 타 과제는 연구자에게 비노출
    assert all(i["protocol_id"] != p2.id for i in ks.similar_protocols(db, new, k=5, user=users["researcher"]))


# ------------------------------------------------------------------ 검토 의견 → 근거
def test_related_for_findings(db):
    from irb_automation.engine.prereview import run_prereview

    result = run_prereview(good_facts(identifiers_collected=["rrn"], sensitive_categories=["mental_health"],
                                      identifier_justification="연계를 위해 필요함을 소명함"))
    related = ks.related_for_findings(db, result["findings"], k=3)
    rrn = related["RRN_COLLECTION"]
    assert rrn[0]["kind"] == "finding_guide" and "주민등록번호" in rrn[0]["title"]
    assert any(h["ref"] == "개인정보 보호법 제24조의2" for h in rrn)
    assert related["SENSITIVE_MENTAL_HEALTH"][0]["title"].endswith("민감정보 포함")
    assert all(len(v) <= 3 for v in related.values())
    assert ks.related_for_findings(db, []) == {}


# ------------------------------------------------------------------ 업로드 문서
def test_upload_markdown_sop_lifecycle(db, users):
    raw = "# 제1조(목적)\n이 지침은 결과 반출 절차를 정한다.\n\n# 제2조(최소 셀)\n반출 결과표의 최소 셀 크기는 10으로 한다.".encode()
    src = kns.add_uploaded_source(db, users["admin"], title="결과 반출 SOP", kind="sop", ref="SOP-07",
                                  filename="sop.md", raw=raw)
    assert len(src.chunks) == 2 and Path(src.file_path).exists()
    hits = ks.search(db, "최소 셀 크기 10", k=3, kinds=["sop"])
    assert hits and hits[0]["source_id"] == src.id and hits[0]["heading"].startswith("제2조")
    kns.set_active(db, src, False, users["admin"])
    assert not ks.search(db, "최소 셀 크기 10", k=3, kinds=["sop"])
    kns.set_active(db, src, True, users["admin"])
    assert kns.reindex_source(db, src, users["admin"]) == 2
    path = src.file_path
    kns.delete_source(db, src, users["admin"])
    assert not Path(path).exists() and not ks.search(db, "최소 셀 크기 10", k=3, kinds=["sop"])
    with pytest.raises(kns.KnowledgeError):
        kns.add_uploaded_source(db, None, title="x", kind="sop", filename="a.exe", raw=b"MZ")
    pasted = kns.add_uploaded_source(db, None, title="붙여넣기", kind="faq", text="질문: 반출은? 답: 반출 심사를 받는다.")
    assert pasted.chunks
    builtin = db.scalars(select(KnowledgeSource).where(KnowledgeSource.key.like("builtin:%"))).first()
    with pytest.raises(kns.KnowledgeError):
        kns.delete_source(db, builtin)


def test_upload_pdf_uses_docproc_when_available(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "irb_automation.docproc", None)
    monkeypatch.setitem(sys.modules, "irb_automation.docproc.text", None)
    with pytest.raises(kns.KnowledgeError, match="docproc"):
        kns.add_uploaded_source(db, None, title="PDF", kind="guideline", filename="g.pdf", raw=b"%PDF-1.4")
    pkg = types.ModuleType("irb_automation.docproc")
    pkg.__path__ = []
    mod = types.ModuleType("irb_automation.docproc.text")
    mod.extract_text = lambda filename, raw, **kw: types.SimpleNamespace(
        text="제1조(목적) 스캔 문서에서 인식한 지침 본문입니다.", pages=1, method="ocr", warnings=["OCR 인식"])
    pkg.text = mod
    monkeypatch.setitem(sys.modules, "irb_automation.docproc", pkg)
    monkeypatch.setitem(sys.modules, "irb_automation.docproc.text", mod)
    src = kns.add_uploaded_source(db, None, title="PDF 지침", kind="guideline", filename="g.pdf", raw=b"%PDF-1.4")
    assert src.meta["warnings"] == ["OCR 인식"] and "인식한 지침" in src.chunks[0].text


# ------------------------------------------------------------------ 질의응답
def test_answer_extractive_without_ai(db, users):
    r = ks.answer(db, "동의 면제를 받으려면 무엇을 소명해야 하나요?", user=users["researcher"])
    assert r["mode"] == "extractive" and r["citations"]
    assert all({"title", "ref", "cited_text", "source_id"} <= set(c) for c in r["citations"])
    assert "[1]" in r["answer"]
    empty = ks.answer(db, "zzzz qqqq", user=users["researcher"])
    assert empty["citations"] == [] and "찾지 못했습니다" in empty["answer"]
    assert db.scalars(select(KnowledgeQuery)).first() is not None


def _fake_ai(monkeypatch, response=None, raise_exc=None, enabled=True):
    calls = []
    pkg = types.ModuleType("irb_automation.ai")
    pkg.__path__ = []
    client = types.ModuleType("irb_automation.ai.client")

    class AIError(Exception):
        pass

    client.AIError = AIError
    client.ai_enabled = lambda: enabled

    def text_document_block(text, title, *, citations=False):
        return {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text},
                "title": title, "citations": {"enabled": citations}}

    def create_message(**kwargs):
        calls.append(kwargs)
        if raise_exc:
            raise AIError("down")
        return response

    client.text_document_block = text_document_block
    client.create_message = create_message
    pkg.client = client
    monkeypatch.setitem(sys.modules, "irb_automation.ai", pkg)
    monkeypatch.setitem(sys.modules, "irb_automation.ai.client", client)
    return calls, AIError


def test_answer_ai_citations_and_pii_masking(db, users, monkeypatch):
    ns = types.SimpleNamespace
    response = ns(content=[
        ns(type="text", text="동의 면제는 두 요건을 모두 갖춰야 합니다.", citations=[
            ns(type="char_location", cited_text="다음 두 요건을 모두 갖춘 경우", document_index=0,
               document_title="doc", start_char_index=0, end_char_index=10)]),
        ns(type="text", text=" 최종 판단은 IRB가 합니다.", citations=None),
    ], stop_reason="end_turn")
    calls, _ = _fake_ai(monkeypatch, response=response)
    q = "홍길동 환자 연락처 010-1234-5678 인데 동의 면제 요건이 뭔가요?"
    r = ks.answer(db, q, user=users["researcher"])
    assert r["mode"] == "ai" and r["pii_masked"]
    assert r["citations"][0]["cited_text"] == "다음 두 요건을 모두 갖춘 경우"
    assert r["citations"][0]["source_id"] == r["hits"][0]["source_id"]
    assert r["answer"].startswith("동의 면제는 두 요건을 모두 갖춰야 합니다. [1]")
    sent = str(calls[0]["messages"])
    assert "010-1234-5678" not in sent and "홍길동" not in sent
    docs = [b for b in calls[0]["messages"][0]["content"] if b["type"] == "document"]
    assert docs and all(b["citations"]["enabled"] for b in docs)
    assert "output_schema" not in calls[0] or calls[0]["output_schema"] is None
    stored = db.scalars(select(KnowledgeQuery).order_by(KnowledgeQuery.id.desc())).first()
    assert "010-1234-5678" not in stored.question and stored.mode == "ai"


def test_answer_falls_back_when_ai_fails(db, monkeypatch):
    _fake_ai(monkeypatch, raise_exc=True)
    r = ks.answer(db, "가명정보 결합 절차", record=False)
    assert r["mode"] == "extractive" and r["citations"] and r["warnings"]
    _fake_ai(monkeypatch, enabled=False)
    r = ks.answer(db, "가명정보 결합 절차", record=False)
    assert r["mode"] == "extractive" and not r["warnings"]


def test_answer_without_user_uses_policy_knowledge_only(db, users):
    _set_precedents(db, users)
    r = ks.answer(db, "정신건강의학과 경과기록 자살 위험 예측", record=False)
    assert all(h["kind"] not in ("protocol", "decision") for h in r["hits"])


# ------------------------------------------------------------------ 화면·API
def test_web_pages_and_permissions(app):
    with TestClient(app) as c:
        login(c, "researcher")
        assert c.get("/knowledge").status_code == 200
        r = c.get("/knowledge", params={"q": "제28조의2 가명정보"})
        assert r.status_code == 200 and "<mark>" in r.text and "제28조의2" in r.text
        assert c.get("/knowledge", params={"q": "가명정보", "kind": "faq"}).status_code == 200
        t = csrf(c)
        r = c.post("/knowledge/ask", data={"csrf_token": t, "question": "주민등록번호를 수집해도 되나요?"})
        assert r.status_code == 200 and "인용 근거" in r.text
        assert c.post("/knowledge/ask", data={"question": "x"}).status_code == 403  # CSRF
        with SessionLocal() as db:
            sid = db.scalars(select(KnowledgeSource.id).where(KnowledgeSource.key == "builtin:law:pipa-28-2")).one()
        assert "제28조의2" in c.get(f"/knowledge/source/{sid}").text
        assert c.get("/admin/knowledge").status_code == 403

        login(c, "admin")
        assert c.get("/admin/knowledge").status_code == 200
        t = csrf(c)
        r = c.post("/admin/knowledge", data={"csrf_token": t, "title": "기관 FAQ", "kind": "faq",
                                             "text": "제1조(질문) 반출 기준은?\n답: 최소 셀 10 이상만 반출한다."},
                   follow_redirects=True)
        assert "색인했습니다" in r.text
        r = c.post("/admin/knowledge", data={"csrf_token": t, "title": "파일", "kind": "sop"},
                   files={"file": ("sop.txt", "제1조(목적) 테스트 지침".encode("cp949"), "text/plain")},
                   follow_redirects=True)
        assert "색인했습니다" in r.text
        assert "선례 재색인 완료" in c.post("/admin/knowledge/reindex-precedents", data={"csrf_token": t},
                                        follow_redirects=True).text
        assert "내장 지식 갱신" in c.post("/admin/knowledge/reseed", data={"csrf_token": t}, follow_redirects=True).text
        with SessionLocal() as db:
            src = db.scalars(select(KnowledgeSource).where(KnowledgeSource.title == "기관 FAQ")).one()
        assert "중지" in c.post(f"/admin/knowledge/{src.id}/toggle", data={"csrf_token": t}, follow_redirects=True).text
        assert c.post(f"/admin/knowledge/{src.id}/delete", data={"csrf_token": t},
                      follow_redirects=True).status_code == 200


def test_precedent_source_page_hidden_from_other_researchers(app, users):
    with SessionLocal() as db:
        p2 = db.scalars(select(Protocol).where(Protocol.title.contains("자살"))).one()
        p2.protocol_no = "IRB-T-0002"
        p2.status = ProtocolStatus.SUBMITTED
        p2.pi_id = db.scalars(select(type(users["researcher2"])).where(
            type(users["researcher2"]).username == "researcher2")).one().id
        p2.co_investigators = []
        db.commit()
        kns.reindex_protocols(db, force=True)
        db.commit()
        sid = db.scalars(select(KnowledgeSource.id).where(KnowledgeSource.key == f"protocol:{p2.id}")).one()
    with TestClient(app) as c:
        login(c, "researcher")
        assert c.get(f"/knowledge/source/{sid}").status_code == 404
        login(c, "admin")
        assert c.get(f"/knowledge/source/{sid}").status_code == 200


def test_api_search_and_answer(app):
    with TestClient(app) as c:
        assert c.get("/api/knowledge/search", params={"q": "가명정보"}).status_code == 401
        login(c, "researcher")
        r = c.get("/api/knowledge/search", params={"q": "제28조의3 결합", "k": 3, "kind": "law"})
        assert r.status_code == 200
        body = r.json()
        assert body[0]["ref"] == "개인정보 보호법 제28조의3" and "highlight" not in body[0]
        r = c.post("/api/knowledge/answer", json={"question": "가명정보 처리 기록에는 무엇을 남기나요?"})
        assert r.status_code == 200 and r.json()["mode"] == "extractive" and r.json()["citations"]
        assert c.post("/api/knowledge/answer", json={"question": ""}).status_code == 422
