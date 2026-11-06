"""기관 양식: 프로필·과제번호 형식, 양식 스키마, 커스텀 항목, 웹 작성 서식, 양식 팩."""
from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.engine.ruleset import get_ruleset
from irb_automation.forms import online as of
from irb_automation.forms.models import FormDefinition, OnlineFormResponse
from irb_automation.forms.packs import export_pack, import_pack, parse_pack, read_bundled, seed_forms
from irb_automation.forms.profile import (
    format_protocol_no,
    get_profile,
    invalidate_cache,
    next_protocol_no,
    validate_format,
)
from irb_automation.forms.schema import (
    SchemaError,
    activate_definition,
    active_protocol_fields,
    get_online_spec,
    save_definition,
    validate_schema,
)
from irb_automation.models import Document, Protocol
from irb_automation.workflow import diff as wdiff
from irb_automation.workflow import service as wf

from .conftest import PASSWORD, csrf, login

SCHEMA = {
    "sections": [{
        "title": "행정",
        "fields": [
            {"key": "grant_number", "label": "연구비 과제번호", "type": "text", "required": True,
             "show_when": {"field": "funding_source"}},
            {"key": "fee_paid", "label": "심의비 납부", "type": "yesno", "required": True},
            {"key": "center", "label": "연구센터", "type": "select", "material": True,
             "options": [{"value": "a", "label": "가센터"}, {"value": "b", "label": "나센터"}]},
            {"key": "rows", "label": "행 수", "type": "number", "min": 1, "max": 100},
        ],
    }]
}


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _protocol(db, users, **kw) -> Protocol:
    p = wf.create_protocol(db, users["researcher"], {"title": "양식 테스트", **kw})
    db.flush()
    return p


def _codes(result):
    return {f["code"]: f for f in result["findings"]}


# ------------------------------------------------------------------ 프로필·과제번호
def test_protocol_no_format_and_sequence(db, users):
    assert format_protocol_no("IRB-{YYYY}-{SEQ:4}", 2026, 7) == "IRB-2026-0007"
    assert format_protocol_no("{YY}-{SEQ:3}-C", 2026, 12) == "26-012-C"
    assert format_protocol_no("H{SEQ}", 2026, 3) == "H3"
    assert validate_format("IRB-{YYYY}") and validate_format("{SEQ}{SEQ}") and validate_format("A-{FOO}-{SEQ}")
    assert validate_format("{YY}-{SEQ:3}-C") == []

    # 기존 IRB-YYYY- 번호와 충돌 없이 최댓값+1 (중간 결번이 있어도 중복 없음)
    for no in ("IRB-2026-0001", "IRB-2026-0005", "IRB-2025-0009"):
        db.add(Protocol(pi_id=users["researcher"].id, title=no, protocol_no=no))
    db.flush()
    assert next_protocol_no(db, 2026) == "IRB-2026-0006"
    assert next_protocol_no(db, 2027) == "IRB-2027-0001"

    get_profile(db).protocol_no_format = "{YY}-{SEQ:3}-C"
    db.flush()
    assert next_protocol_no(db, 2026) == "26-001-C"
    db.add(Protocol(pi_id=users["researcher"].id, title="x", protocol_no="26-001-C"))
    db.flush()
    assert next_protocol_no(db, 2026) == "26-002-C"


# ------------------------------------------------------------------ 스키마 검증
def test_schema_validation_errors():
    assert validate_schema("protocol", SCHEMA) == []
    bad = {"sections": [{"title": "x", "fields": [
        {"key": "title", "label": "예약어"},
        {"key": "Bad-Key", "label": "형식"},
        {"key": "dup", "label": "a"}, {"key": "dup", "label": "b"},
        {"key": "s", "label": "s", "type": "select"},
        {"key": "t", "label": "t", "type": "color"},
        {"key": "n", "label": "n", "type": "number", "min": 5, "max": 1},
        {"key": "w", "label": "w", "show_when": {"field": "nowhere"}},
    ]}]}
    errors = " | ".join(validate_schema("protocol", bad))
    for needle in ("예약어", "영문 소문자", "중복", "options", "지원하지 않는 형식", "min이 max", "존재하지 않는 항목"):
        assert needle in errors, needle
    assert validate_schema("online:x1", {"sections": SCHEMA["sections"]})  # label 누락
    assert validate_schema("weird", SCHEMA)
    # 웹 서식은 기본 과제 항목명(coi_detail 등)을 key로 쓸 수 있다
    assert validate_schema("online:coi", {"label": "COI", "sections": [
        {"title": "a", "fields": [{"key": "coi_detail", "label": "내용"}]}]}) == []


def test_definition_versions_and_single_active(db, users):
    admin = users["coordinator"]
    d1 = save_definition(db, admin, "protocol", "v1", SCHEMA, activate=True)
    d2 = save_definition(db, admin, "protocol", "v2", SCHEMA, activate=True)
    assert (d1.version, d2.version) == (1, 2)
    db.refresh(d1)
    assert not d1.active and d2.active
    activate_definition(db, admin, d1)
    db.refresh(d2)
    assert d1.active and not d2.active
    with pytest.raises(SchemaError):
        save_definition(db, admin, "protocol", "bad", {"sections": [{"title": "x", "fields": [{"key": "1x"}]}]})
    assert [f.key for f in active_protocol_fields(db)] == ["grant_number", "fee_paid", "center", "rows"]


# ------------------------------------------------------------------ 커스텀 항목 → 사전검토·스냅샷 diff
def test_custom_fields_findings_and_diff(db, users):
    save_definition(db, users["coordinator"], "protocol", "기관 항목", SCHEMA, activate=True)
    p = _protocol(db, users)
    codes = _codes(wf.preview(db, p))
    req = [f for f in codes.values() if f["code"] == "CUSTOM_REQUIRED"]
    assert codes["CUSTOM_REQUIRED"]["severity"] == "critical"
    # 연구비 지원기관이 없으면 grant_number(조건부)는 필수 아님 → fee_paid만
    result = wf.preview(db, p)
    fields = {f["field"] for f in result["findings"] if f["code"] == "CUSTOM_REQUIRED"}
    assert fields == {"custom__fee_paid"} and req

    wf.update_protocol(db, users["researcher"], p, {"funding_source": "보건복지부"})
    fields = {f["field"] for f in wf.preview(db, p)["findings"] if f["code"] == "CUSTOM_REQUIRED"}
    assert fields == {"custom__fee_paid", "custom__grant_number"}

    wf.update_protocol(db, users["researcher"], p, {"custom_fields": {"grant_number": "HI22", "fee_paid": False,
                                                                       "center": "a", "rows": 500}})
    codes = _codes(wf.preview(db, p))
    assert "CUSTOM_REQUIRED" not in codes  # yesno '아니오'도 입력으로 인정
    assert "범위" in codes["CUSTOM_INVALID"]["message"] or "이하" in codes["CUSTOM_INVALID"]["message"]

    before = wf.snapshot_protocol(p)
    assert before["custom_fields"]["center"] == "a" and before["custom_field_meta"]["center"]["label"] == "연구센터"
    wf.update_protocol(db, users["researcher"], p, {"custom_fields": {"grant_number": "HI22", "fee_paid": True,
                                                                       "center": "b", "rows": 50}})
    changes = wdiff.diff_snapshots(before, wf.snapshot_protocol(p), get_ruleset())
    by_field = {c["field"]: c for c in changes}
    assert by_field["custom__center"]["before"] == "가센터" and by_field["custom__center"]["after"] == "나센터"
    assert by_field["custom__center"]["material"] is True and by_field["custom__center"]["label"] == "[기관] 연구센터"
    assert by_field["custom__fee_paid"]["after"] == "예"
    assert "custom__grant_number" not in by_field


def test_custom_fields_keep_values_of_retired_fields(db, users):
    save_definition(db, users["coordinator"], "protocol", "v1", SCHEMA, activate=True)
    p = _protocol(db, users, custom_fields={"legacy": "keep", "center": "a"})
    wf.update_protocol(db, users["researcher"], p,
                       {"custom_fields": {"center": "b"}, "_custom_defined": ["grant_number", "fee_paid", "center", "rows"]})
    assert p.custom_fields == {"legacy": "keep", "center": "b"}


# ------------------------------------------------------------------ 웹 작성 서식
def test_online_form_per_member_flow(db, users):
    assert seed_forms(db) is True and seed_forms(db) is False
    pi, co = users["researcher"], users["researcher2"]
    p = _protocol(db, users, co_investigators=[co.id])
    db.commit()
    spec = get_online_spec(db, "coi")
    assert spec.per_member and spec.label == "이해상충 공개서"

    # 필수 항목 누락 → 검증 오류(임시저장은 됨)
    with pytest.raises(of.OnlineFormInvalid) as e:
        of.submit(db, pi, p, spec, {"has_financial_interest": True}, PASSWORD, True)
    assert {"relationship_types", "financial_detail"} <= set(e.value.errors)
    with pytest.raises(of.OnlineFormError):
        of.submit(db, pi, p, spec, {"has_financial_interest": False}, PASSWORD, False)  # 서약 미동의
    from irb_automation.esign import SignatureError

    with pytest.raises(SignatureError):
        of.submit(db, pi, p, spec, {"has_financial_interest": False}, "wrong-password", True)
    db.rollback()
    p = db.get(Protocol, p.id)

    r, doc = of.submit(db, pi, p, spec, {"has_financial_interest": False}, PASSWORD, True)
    assert r.status == "submitted" and r.version == 1 and r.signature_id
    assert doc.doc_type == "coi" and doc.filename.startswith("이해상충 공개서(웹작성)_정연구")
    html = open(doc.stored_path, encoding="utf-8").read()
    assert "이해상충 공개서" in html and "전자서명" in html and "아니오" in html

    # 1/2만 제출 → 체크리스트에서 coi 미제출 + 미제출 연구진 표시
    result = wf.preview(db, p)
    codes = _codes(result)
    assert "한공동" in codes["ONLINE_FORM_INCOMPLETE"]["message"]
    coi_item = next(i for i in result["checklist"] if i["doc"] == "coi") if any(
        i["doc"] == "coi" for i in result["checklist"]) else None
    if coi_item:
        assert not coi_item["provided"]

    # 공동연구자는 다른 사람 응답을 덮어쓰지 않고 본인 응답을 만든다
    r2, _ = of.submit(db, co, p, spec, {"has_financial_interest": False}, PASSWORD, True)
    assert r2.id != r.id and r2.user_id == co.id
    db.expire(p, ["documents"])
    result = wf.preview(db, p)
    assert "ONLINE_FORM_INCOMPLETE" not in _codes(result)
    if coi_item:
        assert next(i for i in result["checklist"] if i["doc"] == "coi")["provided"]
    status = {s["spec"].doc_type: s for s in of.form_status(db, p)}
    assert status["coi"]["complete"] and status["coi"]["submitted"] == 2

    # 재제출 → 버전 증가, 문서 새 버전
    r3, doc3 = of.submit(db, pi, p, spec, {"has_financial_interest": False, "nonfinancial": "없음"}, PASSWORD, True)
    assert r3.id == r.id and r3.version == 2 and doc3.version > doc.version


def test_online_form_permissions(db, users):
    seed_forms(db)
    p = _protocol(db, users)
    pledge = get_online_spec(db, "researcher_pledge")
    assert pledge.signer == "pi"
    ok, _ = of.can_fill(users["coordinator"], p, pledge)
    assert not ok  # 사무국은 열람만
    ok, _ = of.can_fill(users["researcher"], p, pledge)
    assert ok
    p.status = wf.S.SUBMITTED
    ok, reason = of.can_fill(users["researcher"], p, pledge)
    assert not ok and "상태" in reason


def test_required_online_form_and_required_custom_from_pack_b(db, users):
    pack = parse_pack(read_bundled("sample_hospital_b"))
    import_pack(db, users["coordinator"], pack, activate=True, apply_profile=True)
    invalidate_cache()
    assert get_profile(db).protocol_no_format == "{YY}-{SEQ:3}-B"
    assert next_protocol_no(db, 2026) == "26-001-B"
    p = _protocol(db, users)
    codes = _codes(wf.preview(db, p))
    assert codes["ONLINE_FORM_REQUIRED"]["severity"] == "critical"
    fields = {f["field"] for f in wf.preview(db, p)["findings"] if f["code"] == "CUSTOM_REQUIRED"}
    assert {"custom__dept_head_approval", "custom__irb_fee_category", "custom__analysis_env"} <= fields
    assert "custom__fee_paid" not in fields  # show_when(industry) 아님

    spec = get_online_spec(db, "data_access_request")
    answers = {"data_scope": "2020~2023 당뇨", "extraction_method": "center", "delivery": "file",
               "justification": "분석 필요", "applicant": "정연구"}
    of.submit(db, users["researcher"], p, spec, answers, PASSWORD, True)
    db.expire(p, ["documents"])
    remaining = [f for f in wf.preview(db, p)["findings"] if f["code"] == "ONLINE_FORM_REQUIRED"]
    assert not any("자료 이용" in f["message"] for f in remaining)


def test_pack_export_import_roundtrip(db, users):
    seed_forms(db)
    save_definition(db, users["coordinator"], "protocol", "수정본", SCHEMA, activate=True)
    text = export_pack(db)
    pack = parse_pack(text)
    kinds = {f["kind"]: f for f in pack["forms"]}
    assert kinds["protocol"]["schema"] == SCHEMA and "online:coi" in kinds
    assert pack["profile"]["protocol_no_format"] == "IRB-{YYYY}-{SEQ:4}"
    result = import_pack(db, users["coordinator"], pack, activate=True)
    assert {d.kind for d in result["forms"]} == set(kinds)
    active = db.scalars(select(FormDefinition).where(FormDefinition.kind == "protocol",
                                                     FormDefinition.active.is_(True))).all()
    assert len(active) == 1 and active[0].version == 3 and active[0].schema == SCHEMA
    with pytest.raises(SchemaError):
        parse_pack("forms: [{kind: protocol, name: x, schema: {sections: [{title: a, fields: [{key: title, label: t}]}]}}]")
    with pytest.raises(SchemaError):
        parse_pack("::: not yaml [")


# ------------------------------------------------------------------ HTTP
def _token(page_text: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page_text).group(1)


def test_http_admin_and_online_forms(app, db):
    with TestClient(app) as c:
        login(c, "researcher")
        assert c.get("/admin/forms").status_code == 403
        assert c.get("/admin/profile").status_code == 403

        login(c, "coordinator")
        page = c.get("/admin/forms")
        assert page.status_code == 200 and "이해상충 공개서" in page.text  # 기본 팩 자동 로드
        t = csrf(c)
        r = c.post("/admin/profile", data={"csrf_token": t, "institution_name": "테스트병원", "irb_name": "테스트병원 IRB",
                                           "protocol_no_format": "{YY}-{SEQ:3}-T", "letter_footer": "", "contact": ""},
                   follow_redirects=False)
        assert r.status_code == 303
        bad = c.post("/admin/profile", data={"csrf_token": t, "institution_name": "x", "irb_name": "y",
                                             "protocol_no_format": "NO-SEQ"})
        assert bad.status_code == 400 and "일련번호" in bad.text
        assert "테스트병원 IRB" in c.get("/").text  # 화면 상단 기관명은 프로필 값

        # YAML 편집: 검증 오류 → 400, 미리보기, 저장
        yaml_text = "sections:\n  - title: 행정\n    fields:\n      - {key: title, label: x}\n"
        r = c.post("/admin/forms/save", data={"csrf_token": t, "kind": "protocol", "name": "x", "schema": yaml_text,
                                              "action": "save"})
        assert r.status_code == 400 and "예약어" in r.text
        ok_yaml = ("sections:\n  - title: 행정\n    fields:\n"
                   "      - {key: fee_paid, label: 심의비 납부, type: yesno, required: true}\n")
        r = c.post("/admin/forms/save", data={"csrf_token": t, "kind": "protocol", "name": "x", "schema": ok_yaml,
                                              "action": "preview"})
        assert r.status_code == 200 and 'name="custom__fee_paid"' in r.text
        r = c.post("/admin/forms/save", data={"csrf_token": t, "kind": "protocol", "name": "심의비", "schema": ok_yaml,
                                              "action": "save", "activate": "on"}, follow_redirects=False)
        assert r.status_code == 303
        export = c.get("/admin/forms/export")
        assert export.status_code == 200 and "fee_paid" in export.text

        # 연구자: 신청서의 기관 추가 항목 + 웹 서식
        login(c, "researcher")
        t = csrf(c)
        form_page = c.get("/protocols/new")
        assert "8. 기관 추가 항목" in form_page.text and 'name="custom__fee_paid"' in form_page.text
        r = c.post("/protocols/new", data={"csrf_token": t, "_custom": "1", "title": "기관 항목 과제",
                                           "custom__fee_paid": "yes"}, follow_redirects=False)
        pid = int(r.headers["location"].rsplit("/", 1)[1])
        db.expire_all()
        assert db.get(Protocol, pid).custom_fields == {"fee_paid": True}
        detail = c.get(f"/protocols/{pid}")
        assert "기관 추가 항목" in detail.text and "웹 작성 서식" in detail.text

        assert c.get(f"/protocols/{pid}/online-forms").status_code == 200
        fill = c.get(f"/protocols/{pid}/online-forms/security_pledge")
        assert fill.status_code == 200 and 'value="정연구"' in fill.text  # prefill
        data = {"csrf_token": t, "f__agree_purpose": "on", "f__signer_name": "정연구"}
        r = c.post(f"/protocols/{pid}/online-forms/security_pledge/submit", data={**data, "password": PASSWORD,
                                                                                    "attest": "on"})
        assert r.status_code == 400 and "필수 항목" in r.text
        full = {**data, **{f"f__{k}": "on" for k in ("agree_no_reid", "agree_location", "agree_no_transfer",
                                                     "agree_destroy")}, "f__signer_phone": "010-1111-2222"}
        r = c.post(f"/protocols/{pid}/online-forms/security_pledge/submit",
                   data={**full, "password": "wrong", "attest": "on"}, follow_redirects=True)
        assert "비밀번호" in r.text
        r = c.post(f"/protocols/{pid}/online-forms/security_pledge/submit",
                   data={**full, "password": PASSWORD, "attest": "on"}, follow_redirects=False)
        assert r.status_code == 303
        db.expire_all()
        docs = db.scalars(select(Document).where(Document.protocol_id == pid)).all()
        assert any(d.doc_type == "security_pledge" and "(웹작성)" in d.filename for d in docs)
        resp = db.scalars(select(OnlineFormResponse).where(OnlineFormResponse.protocol_id == pid)).one()
        pr = c.get(f"/protocols/{pid}/online-forms/security_pledge/print?response_id={resp.id}")
        assert pr.status_code == 200 and "개인정보 보호 및 보안 서약서" in pr.text

        # 사무국: 열람만 가능(제출 불가)
        login(c, "coordinator")
        t = csrf(c)
        view = c.get(f"/protocols/{pid}/online-forms/researcher_pledge")
        assert view.status_code == 200 and "전자서명 후 제출" not in view.text
        r = c.post(f"/protocols/{pid}/online-forms/researcher_pledge/submit",
                   data={"csrf_token": t, "password": PASSWORD, "attest": "on"}, follow_redirects=True)
        assert "연구책임자" in r.text
        # 다른 연구자(비구성원)는 과제 자체 열람 불가
        login(c, "researcher2")
        assert c.get(f"/protocols/{pid}/online-forms").status_code == 403
    assert date.today()
