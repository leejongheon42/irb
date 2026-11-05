"""가명처리 데모용 합성(가상) 환자 데이터 생성기. 실제 환자 정보가 아님."""
from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SURNAMES = "김이박최정강조윤장임한오서신권황안송류홍"
GIVEN = ["민준", "서연", "도윤", "하은", "지호", "수아", "예준", "지우", "시우", "서윤", "주원", "하린", "지민", "윤서", "현우", "채원"]
ADDR = [
    ("서울특별시", ["강남구", "송파구", "종로구", "마포구"]),
    ("경기도", ["성남시 분당구", "수원시 영통구", "고양시 일산동구"]),
    ("부산광역시", ["해운대구", "부산진구"]),
    ("대전광역시", ["유성구", "서구"]),
]
ROADS = ["테헤란로", "올림픽로", "중앙로", "대학로", "판교역로"]
DX = [("I10", "본태성 고혈압"), ("E11.9", "제2형 당뇨병"), ("C34.1", "폐암"), ("J45.9", "천식"), ("N18.5", "만성 신장병 5기")]


def rrn(birth: date, male: bool) -> str:
    g = (1 if male else 2) if birth.year < 2000 else (3 if male else 4)
    return f"{birth:%y%m%d}-{g}{random.randint(0, 999999):06d}"


def main(path: str, n: int = 300, seed: int = 42) -> None:
    random.seed(seed)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["환자번호", "성명", "주민등록번호", "생년월일", "성별", "연락처", "주소", "입원일자", "퇴원일자", "진단코드", "진단명", "수축기혈압", "HbA1c", "경과기록"])
        for i in range(n):
            male = random.random() < 0.5
            birth = date(1940, 1, 1) + timedelta(days=random.randint(0, 365 * 60))
            name = random.choice(SURNAMES) + random.choice(GIVEN)
            sido, sggs = random.choice(ADDR)
            sgg = random.choice(sggs)
            admit = date(2023, 1, 1) + timedelta(days=random.randint(0, 600))
            dc = admit + timedelta(days=random.randint(1, 20))
            code, dxname = random.choice(DX)
            phone = f"010-{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"
            note = f"{name} 환자 {dxname}으로 입원. 보호자 연락처 {phone}. {admit:%Y-%m-%d} 내원."
            w.writerow([
                f"{10000000 + i}", name, rrn(birth, male), birth.isoformat(), "M" if male else "F", phone,
                f"{sido} {sgg} {random.choice(ROADS)} {random.randint(1, 300)}",
                admit.isoformat(), dc.isoformat(), code, dxname,
                random.randint(100, 180), round(random.uniform(5.0, 11.0), 1), note,
            ])
    print(f"생성 완료: {out} ({n}행)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/synthetic_patients.csv")
