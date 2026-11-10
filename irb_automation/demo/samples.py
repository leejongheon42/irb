"""샘플 데이터 자료실: 기능별로 바로 시험할 수 있는 가상 파일(실제 인물·환자 정보 없음).

파일은 요청할 때 만들어지므로 저장소에 바이너리를 두지 않는다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

REVISION_REQUEST = """1. 연구계획서와 연구대상자 설명문의 연구 기간 표기가 다릅니다. 일치시키십시오.
2. 등록번호(식별정보)를 이용하는 사유와 연계 후 가명처리 절차를 구체적으로 기재하십시오.
3. 자료 보관 기간과 파기 방법을 명시하십시오.
4. 동의 면제 사유 중 '연구대상자의 위험이 극히 낮은 근거'를 보완하십시오."""


@lru_cache(maxsize=1)
def _gen():
    if str(SAMPLES_DIR) not in sys.path:
        sys.path.insert(0, str(SAMPLES_DIR))
    import generate_documents  # samples/generate_documents.py

    return generate_documents


def _pdf(title: str, lines: list[str]) -> bytes:
    from ..docproc.convert import text_to_pdf

    return text_to_pdf("\n".join(lines), title)


def _cv() -> bytes:
    return _pdf("연구책임자 이력서(샘플)", [
        "연구책임자 이력서", "성명: 정연구 (가상 인물)", "소속: ○○대학교병원 내분비내과 조교수",
        "학력: ○○대학교 의과대학 졸업, 동 대학원 의학박사", "연구 경력: 당뇨병 코호트 연구 다수",
        "연구윤리 교육: 2026년 이수", "작성일: 2026. 11. 01.", "서명: 정연구"])


def _training() -> bytes:
    return _pdf("연구윤리 교육 이수증(샘플)", [
        "연구윤리 교육 이수증", "성명: 정연구 (가상 인물)", "과정: 인간대상연구 연구자 기본 과정(8시간)",
        "이수일: 2026. 03. 15.", "유효기간: 2028. 03. 14.", "발급 기관: (가상) 생명윤리 교육 센터"])


def _coi() -> bytes:
    return _pdf("이해상충 공개서(샘플)", [
        "이해상충 공개서", "연구책임자: 정연구 (가상 인물)", "본 연구와 관련하여 공개할 재정적·비재정적 이해관계가 없습니다.",
        "작성일: 2026. 11. 01.", "서명: 정연구"])


def _patients() -> bytes:
    path = SAMPLES_DIR / "synthetic_patients.csv"
    if path.exists():
        return path.read_bytes()
    from ..deid.pipeline import write_csv  # pragma: no cover - 저장소에 항상 있음

    return write_csv(["환자번호", "성명", "나이"], [["P001", "홍길동", "45"]])


def _qc_protocol() -> bytes:
    from ..qc.samples import protocol_docx

    return protocol_docx()


def _qc_icf() -> bytes:
    from ..qc.samples import icf_docx

    return icf_docx()


def _tpl_application() -> bytes:
    from ..doctemplates.samples import sample_application_docx

    return sample_application_docx()


def _tpl_waiver() -> bytes:
    from ..doctemplates.samples import sample_waiver_docx

    return sample_waiver_docx()


@dataclass(frozen=True)
class Sample:
    key: str
    filename: str
    title: str
    purpose: str
    use_url: str
    use_label: str
    build: Callable[[], bytes]
    media_type: str = "application/octet-stream"
    group: str = ""


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SAMPLES: list[Sample] = [
    Sample("import_docx", "한빛대병원_연구계획서.docx", "다른 병원 양식 연구계획서 (DOCX)",
           "표·번호 제목형 양식. '기존 계획서로 시작'에 올리면 항목을 뽑아 새 과제를 만듭니다.",
           "/protocols/import", "기존 계획서로 시작", lambda: _gen().build_docx(), DOCX, "AI 문서 인식"),
    Sample("import_hwpx", "새솔의료원_심의신청서.hwpx", "다른 병원 양식 신청서 (HWPX, 한글)",
           "로마숫자·원문자형(Ⅰ. ①) 한글 양식. 소아 대상·동의 취득 과제입니다.",
           "/protocols/import", "기존 계획서로 시작", lambda: _gen().build_hwpx(), "application/hwp+zip", "AI 문서 인식"),
    Sample("import_hwp", "다온병원_연구계획서.hwp", "다른 병원 양식 계획서 (HWP 5.0)",
           "구형 한글 바이너리 형식도 인식되는지 확인합니다.",
           "/protocols/import", "기존 계획서로 시작", lambda: _gen().build_hwp5(), "application/x-hwp", "AI 문서 인식"),
    Sample("import_pdf", "한빛대병원_연구계획서.pdf", "다른 병원 양식 계획서 (PDF)",
           "텍스트층이 있는 PDF입니다.", "/protocols/import", "기존 계획서로 시작",
           lambda: _gen().build_pdf(), "application/pdf", "AI 문서 인식"),
    Sample("qc_protocol", "Protocol_v3.0.docx", "점검용 연구계획서 v3.0 (오류 없음, 기준 문서)",
           "동의서와 함께 올리면 교차 대조의 기준이 됩니다.", "/qc", "서류 품질 점검", _qc_protocol, DOCX, "서류 점검"),
    Sample("qc_icf", "ICF_v3.0.docx", "점검용 동의서 (오류 7종 포함)",
           "투여 용량·방문 횟수 불일치, 파일명↔본문 버전 불일치, 용어 혼용, 오탈자, 철회 권리 누락이 들어 있습니다.",
           "/qc", "서류 품질 점검", _qc_icf, DOCX, "서류 점검"),
    Sample("cv", "연구책임자_이력서.pdf", "연구책임자 이력서 (개인 서류)",
           "패키지의 '직접 올려야 함' 서류를 채울 때 씁니다.", "/protocols", "과제 서류함에 올리기", _cv,
           "application/pdf", "개인 서류"),
    Sample("training", "연구윤리_교육이수증.pdf", "연구윤리 교육 이수증 (개인 서류)", "", "/protocols", "과제 서류함에 올리기",
           _training, "application/pdf", "개인 서류"),
    Sample("coi", "이해상충_공개서.pdf", "이해상충 공개서 (개인 서류)", "", "/protocols", "과제 서류함에 올리기",
           _coi, "application/pdf", "개인 서류"),
    Sample("revision", "IRB_보완요청_예시.txt", "IRB 보완 요청문 예시",
           "과제 화면의 '보완 요청을 받았다면' 칸에 붙여넣어 보완 대응을 시험합니다.", "/protocols", "과제 화면에서 붙여넣기",
           lambda: REVISION_REQUEST.encode("utf-8"), "text/plain; charset=utf-8", "보완 대응"),
    Sample("patients", "가상환자_추출자료.csv", "가상 환자 추출 자료 (CSV)",
           "주민번호·연락처·주소 등 가상 식별정보가 든 자료. 가명처리 도구에 올려 봅니다.", "/deid/new", "가명처리 도구",
           _patients, "text/csv; charset=utf-8", "가명처리"),
    Sample("tpl_application", "기관서식_심의의뢰서.docx", "기관 서식 예시: 심의의뢰서 (자리표시자 포함)",
           "{{연구제목}}·«연구책임자» 같은 자리표시자가 든 병원 서식. 관리자가 '기관 서식'에 올려 매핑을 시험합니다.",
           "/admin/templates", "기관 서식(관리자)", _tpl_application, DOCX, "기관 서식(관리자)"),
    Sample("tpl_waiver", "기관서식_동의면제사유서.docx", "기관 서식 예시: 동의 면제 사유서", "",
           "/admin/templates", "기관 서식(관리자)", _tpl_waiver, DOCX, "기관 서식(관리자)"),
]
BY_KEY = {s.key: s for s in SAMPLES}


def groups() -> list[tuple[str, list[Sample]]]:
    out: dict[str, list[Sample]] = {}
    for s in SAMPLES:
        out.setdefault(s.group, []).append(s)
    return list(out.items())
