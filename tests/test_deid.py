from __future__ import annotations

import csv
import io
from datetime import date

from irb_automation.deid import transforms as T
from irb_automation.deid.detector import analyze_table
from irb_automation.deid.pipeline import (
    default_config,
    process,
    read_csv_bytes,
    write_csv,
)
from irb_automation.deid.privacy import k_anonymity, suppress_below_k

POLICY = {"k_threshold": 5, "l_threshold": 2, "max_risk_record_ratio": 0.05, "age_band_years": 10,
          "age_top_code": 90, "date_shift_max_days": 30, "address_level": "sido"}

HEADERS = ["환자번호", "성명", "주민등록번호", "생년월일", "성별", "연락처", "주소", "입원일자", "퇴원일자", "진단명", "HbA1c", "경과기록"]


def make_rows(n=200):
    rows = []
    names = ["홍길동", "김민준", "이서연", "박도윤", "최하은"]
    sidos = ["서울특별시 강남구 테헤란로 1", "경기도 성남시 분당구 판교역로 2"]
    dx = ["본태성 고혈압", "제2형 당뇨병"]
    for i in range(n):
        nm = names[i % 5]
        phone = f"010-{1000 + i:04d}-{2000 + i:04d}"
        birth = date(1950 + (i % 40), 1 + i % 12, 1 + i % 27)
        rows.append([
            str(10000000 + i), nm, f"{birth:%y%m%d}-{1 + i % 2}{i:06d}", birth.isoformat(), "MF"[i % 2], phone,
            sidos[i % 2], f"2023-{1 + i % 12:02d}-10", f"2023-{1 + i % 12:02d}-20", dx[i % 2], f"{5 + (i % 50) / 10:.1f}",
            f"{nm} 환자 {dx[i % 2]}으로 입원. 연락처 {phone}.",
        ])
    return rows


def test_detector_classifies_columns():
    a = analyze_table(HEADERS, make_rows())
    by = {c["name"]: c for c in a["columns"]}
    assert by["주민등록번호"]["detected_type"] == "rrn" and by["주민등록번호"]["category"] == "direct"
    assert by["연락처"]["detected_type"] == "phone"
    assert by["성명"]["detected_type"] == "name"
    assert by["환자번호"]["suggested_action"] == "pseudonymize_hmac"
    assert by["생년월일"]["suggested_action"] == "birth_to_age_band"
    assert by["주소"]["detected_type"] == "address"
    assert by["입원일자"]["suggested_action"] == "date_shift"
    assert by["진단명"]["category"] == "sensitive"
    assert by["경과기록"]["category"] == "free_text" and by["경과기록"]["pii_in_text"]
    # 화면 표시용 예시는 마스킹되어야 함
    assert all("*" in s for s in by["주민등록번호"]["samples"])


def test_value_based_detection_without_header_hint():
    rows = [[f"010-{1000 + i}-{3000 + i}", f"{['김', '이', '박'][i % 3]}철수{i % 7}"[:3]] for i in range(50)]
    a = analyze_table(["col_a", "col_b"], rows)
    assert a["columns"][0]["detected_type"] == "phone"


def test_masking_and_generalization():
    assert T.mask("홍길동", "name") == "홍**"
    assert T.mask("010-1234-5678", "phone").endswith("5678") and "1234" not in T.mask("010-1234-5678", "phone")
    assert T.age_band(47, 5, 90) == "45-49"
    assert T.age_band(93, 5, 90) == "90+"
    assert T.generalize_address("서울 강남구 테헤란로 123") == "서울특별시 강남구"
    assert T.generalize_address("경기도 성남시 분당구 판교역로 2") == "경기도 성남시 분당구"
    assert T.generalize_address("경기도 성남시 분당구 판교역로 2", "sido") == "경기도"
    assert T.generalize_date("2023-05-17", "year_month") == "2023-05"
    assert T.generalize_date("2023년 5월 17일", "year") == "2023"
    assert T.top_bottom_code("250", None, 200) == ">=200"
    assert T.round_numeric("123.7", 10) == "120"


def test_pseudonymizer_is_deterministic_and_project_scoped():
    a = T.Pseudonymizer("secret", "protocol:1")
    b = T.Pseudonymizer("secret", "protocol:2")
    assert a.token("10000001") == a.token("1000-0001")  # 정규화
    assert a.token("10000001") != b.token("10000001")  # 과제 간 연결 방지
    off = a.offset_days("p1", 30)
    assert off != 0 and -30 <= off <= 30
    assert off == a.offset_days("p1", 30)


def test_date_shift_preserves_intervals_per_subject():
    p = T.Pseudonymizer("s", "x")
    d = p.offset_days("subject-1", 30)
    admit, discharge = T.shift_date("2023-01-10", d), T.shift_date("2023-01-20", d)
    assert (date.fromisoformat(discharge) - date.fromisoformat(admit)).days == 10
    assert admit != "2023-01-10"


def test_scrub_text():
    text, n = T.scrub_text("홍길동 환자 연락처 010-1234-5678, 주민번호 800101-1234567. 고혈압 환자.", ["홍길동"])
    assert "홍길동" not in text and "010-1234-5678" not in text and "800101-1234567" not in text
    assert "고혈압 환자" in text  # 임상 용어는 보존
    assert n >= 3
    text, _ = T.scrub_text("김철수님이 내원하심")
    assert "김철수" not in text


def test_k_anonymity_and_suppression():
    rows = [["A", "1"]] * 6 + [["B", "1"]] * 2
    k = k_anonymity(rows, [0], 5)
    assert k["min_k"] == 2 and k["records_below_k"] == 2
    kept, removed = suppress_below_k(rows, [0], 5)
    assert removed == 2 and len(kept) == 6


def test_pipeline_end_to_end():
    rows = make_rows()
    a = analyze_table(HEADERS, rows)
    cfg = default_config(a, POLICY)
    res = process(HEADERS, rows, cfg, secret="k", project_salt="protocol:7", policy=POLICY, analysis=a)
    out = dict(zip(res.headers, res.rows[0]))
    for removed in ("성명", "주민등록번호", "연락처"):
        assert removed not in res.headers
    assert out["환자번호"].startswith("P") and out["환자번호"] != rows[0][0]
    assert out["생년월일"].endswith("9") or out["생년월일"].endswith("+")  # 연령 구간
    assert out["주소"] in ("서울특별시", "경기도")
    assert "010-" not in out["경과기록"] and rows[0][1] not in out["경과기록"]
    assert res.report["residual_identifiers"] == []
    assert res.report["k_anonymity"]["applicable"]
    assert res.report["verdict"] in ("적정", "조건부 적정")
    # 원본 대비 날짜 간격 보존
    i_adm, i_dc = res.headers.index("입원일자"), res.headers.index("퇴원일자")
    for r in res.rows[:20]:
        assert (date.fromisoformat(r[i_dc]) - date.fromisoformat(r[i_adm])).days == 10


def test_pipeline_flags_kept_identifier():
    rows = make_rows(50)
    a = analyze_table(HEADERS, rows)
    cfg = default_config(a, POLICY)
    cfg["columns"]["주민등록번호"] = {"action": "keep", "params": {}}
    res = process(HEADERS, rows, cfg, secret="k", project_salt="p", policy=POLICY, analysis=a)
    assert res.report["verdict"] == "부적정"
    assert any(x["column"] == "주민등록번호" for x in res.report["residual_identifiers"])


def test_serial_mapping_is_separate():
    rows = make_rows(20)
    a = analyze_table(HEADERS, rows)
    cfg = default_config(a, POLICY)
    cfg["columns"]["환자번호"] = {"action": "serial", "params": {"prefix": "S"}}
    res = process(HEADERS, rows, cfg, secret="k", project_salt="p", policy=POLICY, analysis=a)
    assert res.rows[0][res.headers.index("환자번호")] == "S000001"
    assert res.mappings["환자번호"][rows[0][0]] == "S000001"


def test_cp949_csv_roundtrip():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["성명", "나이"])
    w.writerow(["홍길동", "40"])
    raw = buf.getvalue().encode("cp949")
    headers, rows, enc = read_csv_bytes(raw)
    assert enc == "cp949" and headers == ["성명", "나이"] and rows[0][0] == "홍길동"
    assert write_csv(headers, rows).startswith(b"\xef\xbb\xbf")  # 엑셀 호환 UTF-8 BOM
