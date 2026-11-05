"""한국 의료데이터 개인식별정보 탐지 패턴."""
from __future__ import annotations

import re

# 값 패턴 ----------------------------------------------------------------------
RRN = re.compile(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\s?-?\s?([1-8])\d{6}(?!\d)")
MOBILE = re.compile(r"(?<!\d)01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
LANDLINE = re.compile(r"(?<!\d)0(?:2|[3-6][1-5]|70)[-.\s)]\s?\d{3,4}[-.\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)")
PASSPORT = re.compile(r"(?<![A-Za-z0-9])[MSRODG]\d{8}(?![A-Za-z0-9])")
CARD = re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)")
DATE = re.compile(
    r"(?<!\d)(19|20)\d{2}[-./년]\s?(0?[1-9]|1[0-2])[-./월]\s?(0?[1-9]|[12]\d|3[01])일?(?!\d)"
)
DATE_COMPACT = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
ADDRESS = re.compile(
    r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|충청|전라|경상)"
    r"[가-힣]*\s+[가-힣]+(시|군|구)(\s+[가-힣0-9]+(읍|면|동|로|길|가))?"
)
ADDRESS_DETAIL = re.compile(r"[가-힣0-9]+(로|길)\s?\d+(-\d+)?|\d+동\s?\d+호|[가-힣]+(아파트|빌라|오피스텔)")
ZIP = re.compile(r"^\d{5}$")

# 텍스트 스크러빙 대상 (순서 중요: 긴 패턴 먼저)
TEXT_SCRUB_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("rrn", RRN, "[주민번호]"),
    ("card", CARD, "[카드번호]"),
    ("mobile", MOBILE, "[전화번호]"),
    ("landline", LANDLINE, "[전화번호]"),
    ("email", EMAIL, "[이메일]"),
    ("passport", PASSPORT, "[여권번호]"),
    ("ip", IPV4, "[IP]"),
    ("address", ADDRESS_DETAIL, "[주소]"),
    ("address", ADDRESS, "[주소]"),
    ("date", DATE, "[날짜]"),
]

# 한국인 성씨(상위 빈도) - 이름 추정에 사용
SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부가복태목형피두감호"
)
COMPOUND_SURNAMES = ("남궁", "황보", "제갈", "선우", "독고", "사공", "서문", "동방")
NAME_LIKE = re.compile(r"^[가-힣]{2,4}$")


def looks_like_korean_name(value: str) -> bool:
    v = value.strip()
    if not NAME_LIKE.match(v):
        return False
    if v[:2] in COMPOUND_SURNAMES and len(v) >= 3:
        return True
    return v[0] in SURNAMES


# 헤더 패턴 -------------------------------------------------------------------
# (탐지 유형, 분류, 헤더 정규식, 권고 처리)
HEADER_RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    ("rrn", "direct", re.compile(r"주민|rrn|ssn|resident.?(reg|no)|jumin", re.I), "delete"),
    ("foreigner_no", "direct", re.compile(r"외국인.?등록", re.I), "delete"),
    ("passport", "direct", re.compile(r"여권|passport", re.I), "delete"),
    ("name", "direct", re.compile(r"^(환자)?(성명|이름|환자명|성함)$|^(pt_?|patient_?)?name$|^nm$|full.?name", re.I), "delete"),
    ("mrn", "direct", re.compile(r"등록번호|환자번호|병록번호|차트번호|^mrn$|chart.?no|patient.?(id|no)|^pt_?(id|no)$|^pid$|unit.?no", re.I), "pseudonymize_hmac"),
    ("phone", "direct", re.compile(r"전화|연락처|휴대|핸드폰|phone|^tel|mobile|cell", re.I), "delete"),
    ("email", "direct", re.compile(r"이메일|메일|e-?mail", re.I), "delete"),
    ("insurance_no", "direct", re.compile(r"보험(증)?번호|insurance.?(no|id)", re.I), "delete"),
    ("account_no", "direct", re.compile(r"계좌|카드번호|account|card.?no", re.I), "delete"),
    ("ip", "direct", re.compile(r"^ip$|ip.?addr", re.I), "delete"),
    ("address", "quasi", re.compile(r"주소|거주지|address|^addr", re.I), "address_generalize"),
    ("zip", "quasi", re.compile(r"우편번호|zip|postal", re.I), "zip_generalize"),
    ("birth_date", "quasi", re.compile(r"생년|생일|출생|birth|dob", re.I), "birth_to_age_band"),
    ("age", "quasi", re.compile(r"^(나이|연령|만나이)|^age", re.I), "age_band"),
    ("sex", "quasi", re.compile(r"^(성별|sex|gender)$", re.I), "keep"),
    ("region", "quasi", re.compile(r"지역|시도|시군구|region|sido|sigungu|city", re.I), "keep"),
    ("occupation", "quasi", re.compile(r"직업|occupation|job", re.I), "keep"),
    ("nationality", "quasi", re.compile(r"국적|nationality", re.I), "keep"),
    ("date", "quasi", re.compile(r"(일자|일시|날짜|입원일|퇴원일|진료일|수술일|내원일|사망일|검사일|처방일)$|date|_dt$|^dt_|datetime", re.I), "date_shift"),
    ("diagnosis", "sensitive", re.compile(r"진단|상병|질환|병명|diagnos|icd|kcd|disease|dx", re.I), "keep"),
    ("free_text", "free_text", re.compile(r"메모|소견|기록|판독|경과|요약|비고|note|text|comment|remark|memo|report|narrative", re.I), "text_scrub"),
]

CATEGORY_LABELS = {
    "direct": "직접 식별자",
    "quasi": "준식별자",
    "sensitive": "민감속성",
    "free_text": "자유기술",
    "other": "일반 속성",
}

TYPE_LABELS = {
    "rrn": "주민등록번호",
    "foreigner_no": "외국인등록번호",
    "passport": "여권번호",
    "name": "성명",
    "mrn": "환자번호",
    "phone": "전화번호",
    "email": "이메일",
    "insurance_no": "건강보험증번호",
    "account_no": "계좌·카드번호",
    "ip": "IP주소",
    "address": "주소",
    "zip": "우편번호",
    "birth_date": "생년월일",
    "age": "나이",
    "sex": "성별",
    "region": "지역",
    "occupation": "직업",
    "nationality": "국적",
    "date": "날짜",
    "diagnosis": "진단·상병",
    "free_text": "자유기술",
    "numeric": "수치",
    "categorical": "범주형",
    "unknown": "기타",
}
