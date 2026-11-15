"""MVP(/mvp) 서류 심사 시험용 데모 서류 세트 생성기. 모든 인물·기관·과제는 가상이다.

  python samples/generate_mvp_demo.py [출력 디렉터리]    (기본: samples/mvp_demo)

시나리오별 폴더를 만든다. 폴더의 파일을 /mvp 화면에 한 번에 올려 심사하면 된다.
  01_양호_제출세트      오류가 거의 없는 계획서·동의서·개인 서류 → 높은 점수/통과 기대
  02_결함_제출세트      계획서↔동의서 불일치·버전 오류·필수 항목 누락 등 → 낮은 점수/보완 기대
  03_아동연구_미비세트  아동 대상인데 법정대리인·아동 승낙 누락, 자리표시자 미기입
  04_업로드_거부시험    형식·내용 검증에서 걸러지는 파일(위장·빈 파일·실행 파일·ZIP)
"""
from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from irb_automation.demo import samples as demo  # noqa: E402
from irb_automation.qc import samples as qc  # noqa: E402


def _doc(header: str, footer: str):
    return qc._doc(header, footer)


def _save(d) -> bytes:
    out = BytesIO()
    d.save(out)
    return out.getvalue()


def _fill(d, sections: list[tuple[str, str]]) -> None:
    for heading, body in sections:
        d.add_heading(heading, 2)
        for line in body.split("\n"):
            d.add_paragraph(line)


# ------------------------------------------------------------- 01 양호: 서로 일치하는 계획서·동의서
def clean_protocol() -> bytes:
    return qc.protocol_docx()  # v3.0, 100mg 1일 1회, 8회 방문, 120명


def clean_icf() -> bytes:
    d = _doc("연구대상자 설명문 및 동의서 | Version 3.0", "기관생명윤리위원회 승인 후 사용 · Version 3.0")
    d.add_heading("연구대상자 설명문 및 동의서", 0)
    _fill(d, [
        ("연구 목적", "본 연구의 목적은 시험약 Mira-001의 안전성 및 유효성을 평가하는 것입니다."),
        ("참여 방법", "귀하는 본 연구에 연구대상자로 참여하게 됩니다. 시험약: Mira-001\n"
                     "시험약은 1일 1회 100mg 경구 투여합니다.\n참여 기간은 총 12주이며, 총 8회 방문이 예정되어 있습니다.\n"
                     "연구 기간은 2026.12.01 ~ 2027.11.30이며 예상 연구대상자 수는 총 120명입니다."),
        ("예상되는 위험과 불편", "예상되는 이상반응으로는 두통, 오심 등이 있으며 이상반응이 있으면 즉시 연구자에게 알려 주십시오."),
        ("예상되는 이익", "혈당 개선의 이익이 있을 수 있으나 보장되지는 않습니다."),
        ("다른 치료 방법", "참여하지 않더라도 기존 치료제 등 다른 치료 방법을 선택할 수 있습니다."),
        ("자발적 참여와 철회", "귀하의 참여는 전적으로 자발적이며 언제든지 이유 없이 참여를 철회할 수 있습니다. "
                             "철회하려면 연구책임자에게 연락해 요청하시면 됩니다. 참여를 거부하거나 철회해도 불이익이 없습니다."),
        ("개인정보 보호", "귀하의 개인정보는 가명처리되어 비밀이 보장되며 원내 보안 서버에서만 관리됩니다."),
        ("보상과 비용", "연구와 관련된 손상이 발생하면 규정에 따라 보상합니다. 별도 비용 부담이나 사례비는 없습니다."),
        ("기록 열람", "모니터링 요원과 IRB는 비밀 보장 하에 의무기록을 열람할 수 있습니다."),
        ("자료 보관", "수집된 자료는 연구 종료 후 3년간 보관한 뒤 복구 불가능하게 폐기합니다."),
        ("새로운 정보", "연구 참여 의사에 영향을 줄 새로운 정보가 생기면 알려 드립니다."),
        ("연락처", "연구책임자 정연구 02-1234-5678 / 기관생명윤리위원회 02-1234-0000"),
        ("동의", "본인은 위 내용을 충분히 이해하였으며 자발적으로 동의합니다.\n"
               "연구대상자 성명:            서명:            날짜:     년   월   일\n"
               "연구자 성명:                서명:            날짜:     년   월   일"),
    ])
    return _save(d)


# ------------------------------------------------------------- 02 결함: 기존 오류 7종 샘플 + 필수 서류 누락
def flawed_protocol() -> bytes:
    return qc.protocol_docx()


def flawed_icf() -> bytes:
    return qc.icf_docx()  # 용량·방문 횟수 불일치, 버전 2.0, 용어 혼용, 오탈자, 철회 권리 누락


# ------------------------------------------------------------- 03 아동 대상 + 자리표시자
def child_protocol() -> bytes:
    d = _doc("소아 천식 연구계획서 | Version 1.0", "대외비 · v1.0 · 2026.11.05")
    d.add_heading("연구계획서", 0)
    t = d.add_table(rows=4, cols=2)
    t.style = "Table Grid"
    for row, (k, v) in zip(t.rows, [("연구 제목", "소아 천식 환자의 흡입 스테로이드 순응도 평가"),
                                     ("연구책임자", "박소아"), ("버전", "v1.0"), ("작성일", "2026. 11. 05.")]):
        row.cells[0].text, row.cells[1].text = k, v
    _fill(d, [
        ("1. 연구 목적", "만 6세 이상 18세 미만 소아 천식 환자의 흡입 스테로이드 순응도와 응급실 방문의 관련성을 평가한다."),
        ("2. 연구 배경 및 필요성", "소아 천식의 재발 악화는 순응도와 관련이 있으나 국내 자료가 부족하다."),
        ("3. 연구 대상", "선정 기준: 만 6세 이상 18세 미만 천식 환자. 제외 기준: 선천성 심장질환 환자."),
        ("4. 연구 방법", "보호자와 아동에게 설문조사를 시행하고 진료기록을 함께 수집한다."),
        ("5. 대상자 수 및 산출 근거", "목표 대상자 수는 OOO명이며 산출 근거는 추후 기재한다."),
        ("6. 연구 기간", "연구 기간: 승인일 ~ (기재 예정)"),
        ("7. 수집 항목 및 통계 분석", "수집 항목: 연령, 성별, 진단명, 응급실 방문 횟수, 보호자 연락처. 통계 분석은 TBD."),
        ("8. 개인정보 보호", "[작성 필요]"),
        ("9. 동의 취득", "보호자의 서면 동의를 받는다."),
    ])
    return _save(d)


def child_icf() -> bytes:
    d = _doc("연구대상자 설명문 및 동의서 | Version 1.0", "Version 1.0")
    d.add_heading("연구대상자 설명문 및 동의서", 0)
    _fill(d, [
        ("연구 목적", "본 연구는 소아 천식 환자의 약 사용 습관과 응급실 방문의 관련성을 알아보는 연구입니다."),
        ("참여 방법", "설문에 응답하고 진료기록 열람에 동의하게 됩니다. 연구 기간은 2027년 12월까지입니다."),
        ("위험과 이익", "특별한 위험은 없으며 직접적인 이익은 없습니다."),
        ("연락처", "연구책임자 박소아 (연락처: 000-0000-0000)"),
        ("동의", "연구대상자 성명:            서명:            날짜:     년   월   일"),
    ])
    return _save(d)


# ------------------------------------------------------------- 04 업로드 거부 시험
def fake_pdf() -> bytes:
    return "이 파일은 PDF가 아니라 텍스트입니다.".encode("utf-8")


def fake_exe() -> bytes:
    return b"MZ\x90\x00" + b"\x00" * 200


def zip_bundle() -> bytes:
    import zipfile

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("연구계획서.txt", "압축 파일 안의 서류")
    return buf.getvalue()


SETS: dict[str, list[tuple[str, callable]]] = {
    "01_양호_제출세트": [
        ("Protocol_v3.0.docx", clean_protocol), ("ICF_v3.0.docx", clean_icf),
        ("연구책임자_이력서.pdf", demo._cv), ("연구윤리_교육이수증.pdf", demo._training), ("이해상충_공개서.pdf", demo._coi),
    ],
    "02_결함_제출세트": [("Protocol_v3.0.docx", flawed_protocol), ("ICF_v3.0.docx", flawed_icf)],
    "03_아동연구_미비세트": [("소아천식_연구계획서_v1.0.docx", child_protocol), ("소아천식_동의서_v1.0.docx", child_icf)],
    "04_업로드_거부시험": [
        ("가짜_연구계획서.pdf", fake_pdf), ("빈_파일.txt", lambda: b""),
        ("setup.exe", fake_exe), ("서류묶음.zip", zip_bundle),
    ],
}


def main(out_dir: str = "samples/mvp_demo") -> None:
    out = Path(out_dir)
    for folder, files in SETS.items():
        (out / folder).mkdir(parents=True, exist_ok=True)
        for name, build in files:
            (out / folder / name).write_bytes(build())
            print(f"생성: {out / folder / name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/mvp_demo")
