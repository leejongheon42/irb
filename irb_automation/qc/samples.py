"""시연·테스트용 샘플 서류(가상). 의도적으로 넣은 문제:

- 계획서 v3.0 ↔ 동의서: 투여 용량(1일 1회 100mg ↔ 1일 2회 200mg), 방문 횟수(8회 ↔ 6회) 불일치
- 동의서 파일명 v3.0 ↔ 본문 Version 2.0
- '피험자'/'연구대상자', '부작용'/'이상반응' 혼용, 오탈자('연구대상지')
- 동의서에 '자발적 참여·철회 권리' 누락
- 표·머리글·바닥글이 있어 자동 교정 후 서식 보존 여부를 확인할 수 있다
"""
from __future__ import annotations

from io import BytesIO


def _doc(header: str, footer: str):
    from docx import Document
    from docx.shared import Pt

    d = Document()
    st = d.styles["Normal"]
    st.font.size = Pt(10.5)
    sec = d.sections[0]
    sec.header.paragraphs[0].text = header
    sec.footer.paragraphs[0].text = footer
    return d


def protocol_docx() -> bytes:
    d = _doc("Mira-001 임상연구 계획서 | Version 3.0", "본 문서는 대외비입니다 · 버전 3.0 · 2026.11.01")
    d.add_heading("연구계획서", 0)
    t = d.add_table(rows=4, cols=2)
    t.style = "Table Grid"
    for row, (k, v) in zip(t.rows, [("연구 제목", "제2형 당뇨병 환자에서 Mira-001의 안전성 및 유효성 평가"),
                                     ("연구책임자", "정연구"), ("버전", "v3.0"), ("작성일", "2026. 11. 01.")]):
        row.cells[0].text, row.cells[1].text = k, v
    for h, body in [
        ("1. 연구 목적", "본 연구의 목적은 시험약 Mira-001의 안전성 및 유효성을 평가하는 것이다."),
        ("2. 연구 배경 및 필요성", "기존 치료제로 혈당 조절이 불충분한 환자에게 새로운 치료 선택지가 필요하다."),
        ("3. 연구 대상", "선정 기준: 만 19세 이상 제2형 당뇨병 환자. 제외 기준: 투석 환자, 임신부."),
        ("4. 연구 방법", "시험약: Mira-001\n투여: 1일 1회 100mg 경구 투여\n시험 기간은 총 12주이며, 총 8회 방문한다."),
        ("5. 대상자 수 및 산출 근거", "목표 연구대상자 수는 총 120명이며 검정력 80%를 근거로 산출하였다."),
        ("6. 연구 기간", "연구 기간: 2026.12.01 ~ 2027.11.30"),
        ("7. 수집 항목 및 통계 분석", "수집 항목: 연령, 성별, HbA1c, 이상반응. 통계 분석은 혼합효과모형으로 한다."),
        ("8. 위험과 이익", "예상되는 이상반응은 두통, 오심이며 이익은 혈당 개선이다."),
        ("9. 개인정보 보호", "수집 자료는 가명처리하여 원내 보안 서버에 보관한다."),
        ("10. 자료 보관 및 파기", "연구 종료 후 3년간 보관하고 이후 복구 불가능하게 파기한다."),
        ("11. 동의 취득", "연구 참여 전 서면 동의를 받는다."),
    ]:
        d.add_heading(h, 2)
        for line in body.split("\n"):
            d.add_paragraph(line)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def icf_docx() -> bytes:
    d = _doc("연구대상자 설명문 및 동의서 | Version 2.0", "기관생명윤리위원회 승인 후 사용 · Version 2.0")
    d.add_heading("연구대상자 설명문 및 동의서", 0)
    for h, body in [
        ("연구 목적", "본 임상시험의 목적은 시험약 Mira-001의 안전성 및 유효성을 평가하는 것입니다."),
        ("참여 방법", "귀하는 본 시험에 피험자로서 참여하게 됩니다. 시험약: Mira-001\n"
                     "시험약은 1일 2회 200mg 경구 투여합니다.\n참여 기간은 총 12주이며, 총 6회 방문이 예정되어 있습니다."),
        ("예상되는 위험과 이익", "예상되는 부작용으로는 두통, 오심 등이 있습니다. 혈당 개선의 이익이 있을 수 있습니다."),
        ("개인정보 보호", "귀하의 개인정보는 비밀이 보장되며 연구대상지 식별이 불가능하도록 관리됩니다."),
        ("보상", "연구와 관련된 손상이 발생하면 규정에 따라 보상합니다. 별도 비용 부담은 없습니다."),
        ("연락처", "연구책임자 정연구 02-1234-5678 / 기관생명윤리위원회 02-1234-0000"),
        ("자료 보관", "수집된 자료는 3년간 보관 후 폐기합니다."),
        ("동의", "본인은 위 내용을 충분히 이해하였습니다.\n연구대상자 성명:            서명:            날짜:     년   월   일"),
    ]:
        d.add_heading(h, 2)
        for line in body.split("\n"):
            d.add_paragraph(line)
    out = BytesIO()
    d.save(out)
    return out.getvalue()


SAMPLES = [("Protocol_v3.0.docx", "protocol", protocol_docx), ("ICF_v3.0.docx", "consent_form", icf_docx)]
