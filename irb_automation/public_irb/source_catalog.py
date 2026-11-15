"""Discovery metadata for the source files referenced by the public IRB plan."""
from datetime import date

from .sources import SourceKey, SourceRegistration

ROOT = "https://app.notion.com/p/"
PAGES = {
    "research": "0c87ecb51a42834d80e281126c8b360c",
    "consent": "6017ecb51a4282d094c58139c1fc6c28",
    "result": "2f07ecb51a4282c2ba07012c7f209664",
    "extra": "2d87ecb51a42831f8f8201fc3a43e2c0",
    "evaluation": "20a7ecb51a428274aedc81ca1fe8ef58",
    "overview": "0637ecb51a4283899a4801c8123f35bf",
}
SOURCE_CATALOG = tuple(SourceRegistration(
    key=SourceKey(series=series, number=number, version=version), title=title, owner=owner,
    origin=ROOT + PAGES[page], original_filename=f"{title}.{ext}", applies_to=(kind,),
    reference_date=date(2026, 9, 30) if ext == "xlsx" else None,
) for series, number, version, title, owner, page, kind, ext in (
    ("권고서식", "38", "4.0", "실험연구 연구계획서", "researcher", "research", "intervention", "hwp"),
    ("권고서식", "39", "4.0", "조사연구 연구계획서", "researcher", "research", "survey", "hwp"),
    ("권고서식", "40", "4.0", "인체유래물 연구계획서", "researcher", "research", "specimen", "hwp"),
    ("권고서식", "41", "3.0", "배아줄기세포주 이용 연구계획서", "researcher", "research", "stem_cell", "hwp"),
    ("권고서식", "43", "2.0", "개인정보 이용 연구계획서", "researcher", "research", "records", "hwp"),
    ("권고서식", "44", "5.0", "인간대상연구 설명문 및 동의서", "researcher", "consent", "human", "hwp"),
    ("권고서식", "45", "5.0", "인체유래물 연구 설명문 및 동의서", "researcher", "consent", "specimen", "hwp"),
    ("권고서식", "46", "2.0", "인간대상연구 아동용 승낙서", "researcher", "consent", "human", "hwp"),
    ("권고서식", "47", "2.0", "인체유래물연구 아동용 승낙서", "researcher", "consent", "specimen", "hwp"),
    ("권고서식", "57", "1.0", "결과보고서", "researcher", "result", "final_report", "hwp"),
    ("권고서식", "3", "확인 대기", "심의면제 자가점검표", "researcher", "consent", "exemption", "pdf"),
    ("권고서식", "11", "확인 대기", "서면동의면제 자가점검표", "researcher", "consent", "waiver_request", "hwp"),
    ("권고서식", "4-1", "4.0", "변경대비표 e-IRB용", "researcher", "extra", "amendment", "hwp"),
    ("권고서식", "8", "확인 대기", "변경대비표", "researcher", "extra", "amendment", "hwp"),
    ("별지", "8", "확인 대기", "연구계획위반이탈사례 보고서", "researcher", "extra", "deviation", "hwp"),
    ("별지", "10-1", "확인 대기", "이해상충공개서", "researcher", "extra", "coi_update", "hwp"),
    ("별지", "12", "확인 대기", "심의면제신청서", "researcher", "consent", "exemption", "hwp"),
    ("별지", "22", "확인 대기", "서면동의면제사유서", "researcher", "consent", "waiver_request", "hwp"),
    ("별지", "24", "확인 대기", "기관 비소속 연구자 협약서", "researcher", "extra", "individual", "hwp"),
    ("공용위원회 별지", "28-3", "4.3", "폐기 및 이관 심의 신청서", "researcher", "extra", "disposal_transfer", "hwp"),
    ("시행규칙 별지", "34", "확인 대기", "인체유래물 연구 동의서", "researcher", "consent", "direct_collection", "hwp"),
    ("권고서식", "10-5", "2.0", "신규과제 평가점검표", "committee", "evaluation", "stem_cell", "hwp"),
    ("권고서식", "10-6", "확인 대기", "개인정보 제공 평가점검표", "committee", "evaluation", "info_provision", "hwp"),
    ("권고서식", "10-7", "확인 대기", "인체유래물 제공 평가점검표", "committee", "evaluation", "specimen_provision", "hwp"),
    ("권고서식", "10-8", "확인 대기", "폐기 및 이관 평가점검표", "committee", "evaluation", "disposal_transfer", "hwp"),
    ("별지", "20", "3.0", "현장조사 점검표", "committee", "evaluation", "inspection", "hwp"),
    ("별지", "13", "확인 대기", "심의면제확인서", "committee", "evaluation", "exemption", "hwp"),
    ("지침", "SOP", "5.5", "공용위원회 표준운영지침서", "reference", "overview", "all", "pdf"),
    ("조회자료", "agreement-list", "2026-09-30", "협약 기관 목록", "reference", "overview", "agreement", "xlsx"),
))
