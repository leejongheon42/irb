"""Compare the specific data definitions of a retrospective records study."""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Final

from .consent_disclosures import sentences
from .facts import _DATE, date_ranges, parse_date
from .study_consistency import Citation, _candidate

if TYPE_CHECKING:
    from .checks import QCDoc

_HEADING: Final = re.compile(r"(?m)^(?:#+[ \t]+(?:\d+[.)]?[ \t]+)?|\d+[.)][ \t]+)(.+)$")
_RECORDS: Final = re.compile(r"후향적[^\n.!?]{0,50}(?:기록|코호트)|(?:기존|과거)[^\n.!?]{0,30}(?:의무|진료|퇴원)[^\n.!?]{0,12}기록")
_ELIGIBILITY: Final = re.compile(r"대상\s*퇴원\s*기간|퇴원\s*(?:일|자)?\s*(?:선정|대상)\s*기간")
_WINDOW: Final = re.compile(r"퇴원\s*(?:다음\s*날|후)[^\n.!?]{0,40}?(?<!\d)(\d+)\s*일\s*이내")
_OUTCOME: Final = re.compile(r"(?:일|1)\s*차\s*(?:평가\s*변수|결과)")
_VISIT: Final = re.compile(r"방문|내원")
_KEY: Final = re.compile(r"^연결\s*키")
_EXCLUSIVE_KEY: Final = re.compile(r"(?:만|단독)(?:\s*열|\s*접근|\s*조회|\s*열람|\s*권한|의\s*권한)")
_ANALYST: Final = re.compile(r"분석\s*(?:담당(?:자)?|연구자|연구원|자|팀)")
_GRANT: Final = re.compile(r"(?P<recipient>[^.!?\n|]{1,60}?)(?:에게|[은는이가])\s*(?:읽기|열람|접근|조회)\s*권한을\s*(?:부여한다|부여합니다|준다|갖는다|가진다|허용한다)")
_DENIED: Final = re.compile(r"(?:전달|제공|부여|열람|접근|조회)하지\s*않|(?:열|읽|접근할|열람할)\s*수\s*없|금지")
_ANALYSIS_FILE: Final = re.compile(r"^(?:가명\s*)?분석\s*파일(?:은|이|\s*[|:：])")
_AFTER_END: Final = re.compile(r"연구\s*종료\s*(?:후\s*)?(\d+)\s*년")
_DISPOSAL: Final = re.compile(_DATE + r"\s*(?:에\s*)?[^.\n]{0,45}(?:폐기|삭제|파기)")
_CONVENIENCE: Final = re.compile(r"(?:시간|업무|작업|부담)[^\n.!?]{0,20}(?:줄[이일여]|절약|단축|경감)|제때|일정\s*준수|업무\s*편의")
_WAIVER_BARRIERS: Final = re.compile(
    r"보호자\s*번호|(?:번호|연락처)[^\n.!?]{0,25}(?:변경|부정확|없|확인되지)"
    r"|(?:연락|재접촉)[^\n.!?]{0,25}(?:불가|곤란|어려|노출)|사생활\s*노출|(?:정보|신원)\s*노출"
    r"|선택\s*편향|(?:동의|연락)\s*가능자만\s*포함"
)


def _section(text: str, title: str) -> str:
    headings = list(_HEADING.finditer(text))
    return next((text[heading.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)].strip()
                 for i, heading in enumerate(headings) if re.search(title, heading[1])), "")


def _table_rows(text: str) -> Iterator[tuple[str, str]]:
    for line in text.splitlines():
        label, separator, value = line.partition("|")
        if separator and label.strip() and value.strip():
            yield label.strip(), line.strip()


def _finding(doc: QCDoc, code: str, title: str, detail: str, suggestion: str, rule: str, flag: str,
             quotes: list[Citation], *, exact: bool = False) -> dict:
    issue = _candidate(doc, f"RETRO_{code}", title, detail, suggestion, rule, flag, quotes)
    if exact:
        issue.update(result="mismatch", reason="", question="", reference_blocked=True)
    return issue


def _eligibility_findings(plan: QCDoc, application: QCDoc) -> Iterator[dict]:
    selection = _section(plan.text, r"선정\s*(?:기준|및\s*제외)|대상자\s*(?:선정|기준)")
    ranges = [(a, b, quote) for a, b, _, quote in date_ranges(selection)
              if a and b and a <= b and "퇴원" in quote]
    if len(ranges) != 1:
        return
    start, end, reference = ranges[0]
    for a, b, _, quote in date_ranges(application.text):
        if a and b and a <= b and (a, b) != (start, end) and _ELIGIBILITY.search(quote.split("|")[0]):
            yield _finding(application, "ELIGIBILITY_PERIOD", "대상자 선정에 쓰는 퇴원 기간 불일치",
                           f"신청서는 {a}~{b}, 계획서의 적격 퇴원 기간은 {start}~{end}입니다.",
                           f"신청서의 대상 퇴원 기간을 {start}~{end}로 대조·정정하고 결과 조회 종료일은 그대로 유지하십시오.",
                           "E03", "선정·제외 기준", [Citation(application, quote), Citation(plan, reference)], exact=True)


def _outcome_findings(plan: QCDoc, extraction: QCDoc, rows: list[tuple[str, str]]) -> Iterator[dict]:
    outcomes = _section(plan.text, r"(?:결과|평가\s*변수|평가\s*지표).*(?:정의|측정)|노출.*결과")
    references = [(int(window[1]), quote) for quote in sentences(outcomes) if _OUTCOME.search(quote)
                  and _VISIT.search(quote) and (window := _WINDOW.search(quote))]
    if len(references) != 1:
        return
    days, reference = references[0]
    examples = _section(extraction.text, r"(?:기입|작성|기록)\s*예시")
    for label, quote in rows:
        window = _WINDOW.search(quote) if _VISIT.search(label) else None
        if not window:
            continue
        # Only inspect a column that explicitly combines the visit interval and outcome.
        table = [line.strip() for line in examples.splitlines() if "|" in line]
        header = next((i for i, line in enumerate(table) if re.search(r"간격\s*[·/]\s*(?:방문\s*)?여부", line)), None)
        outside: list[Citation] = []
        if header is not None:
            column = next(i for i, cell in enumerate(table[header].split("|")) if re.search(r"간격.*여부", cell))
            for line in table[header + 1:]:
                cells = line.split("|")
                marked_visit = re.fullmatch(r"\s*(\d+)\s*일\s*[·/]\s*1\s*", cells[column]) if len(cells) > column else None
                if marked_visit and int(marked_visit[1]) > days:
                    outside.append(Citation(extraction, line))
        different_window = days != int(window[1])
        if not different_window and not outside:
            continue
        detail = (f"계획서는 퇴원 후 {days}일 이내, 추출 명세는 {window[1]}일 이내 방문을 사용합니다."
                  if different_window else f"추출 명세에 {days}일 관찰창이 명시되어 있으나 예시에는 범위 밖 방문이 1로 표시됩니다.")
        correction = f"첫 방문 간격과 방문 여부를 계획서의 {days}일 관찰창에 맞추십시오."
        if outside:
            correction += " 인용된 관찰창 밖 방문 예시는 간격을 공란(—)으로, 결과를 0으로 정정하십시오."
        yield _finding(extraction, "OUTCOME_WINDOW", "일차 방문 결과의 관찰창 불일치",
                       detail, correction,
                       "E05", "수행 절차·검체", [Citation(extraction, quote), Citation(plan, reference), *outside[:5]],
                       exact=True)


def _field_key(field: str) -> str:
    compact = re.sub(r"\s+|\([^)]*\)", "", field)
    return re.sub(r"^(?:\d+일|퇴원전)|여부$", "", compact).casefold()


def _field_findings(plan: QCDoc, extraction: QCDoc, rows: list[tuple[str, str]]) -> Iterator[dict]:
    collection = _section(plan.text, r"수집\s*(?:항목|변수)|자료\s*수집\s*항목")
    declared = {part for label, _ in _table_rows(collection) for field in re.split(r"[,，/]", label)
                if (part := _field_key(field)) not in ("항목", "필드", "변수", "구분")}
    if not declared:
        return
    plan_rows = [Citation(plan, quote) for label, quote in _table_rows(collection)
                 if _field_key(label) not in ("항목", "필드", "변수", "구분")][:5]
    example_header = next((line for line in _section(extraction.text, r"(?:기입|작성|기록)\s*예시").splitlines()
                           if line.startswith("연구번호") and "|" in line), "")
    for label, quote in rows:
        for field in re.split(r"[,，/]", label):
            key = _field_key(field)
            if (key in ("항목", "필드", "변수", "구분") or len(key) < 2
                    or any(key == known or (len(key) >= 3 and key in known) for known in declared)):
                continue
            evidence = [Citation(extraction, quote), *plan_rows]
            if key in {_field_key(cell) for cell in example_header.split("|")}:
                evidence.append(Citation(extraction, example_header))
            yield _finding(extraction, "UNDECLARED_FIELD", f"계획서에 없는 추출 필드 확인: {field.strip()}",
                           f"추출 명세에 '{field.strip()}' 항목이 있으나 계획서의 수집 항목표에는 없습니다.",
                           "추출 명세와 예시 기록에서 해당 항목을 제거하거나, 수집의 필요성과 이용 범위를 검토해 반영하십시오.",
                           "E06", "개인정보·이용·제공", evidence)


def _access_findings(plan: QCDoc, management: QCDoc, waiver: QCDoc | None) -> Iterator[dict]:
    reference = next((line for paragraph in plan.text.splitlines() for line in sentences(paragraph) if _KEY.search(line)
                      and (exclusive := _EXCLUSIVE_KEY.search(line)) and not _ANALYST.search(line[:exclusive.start()])), "")
    if not reference:
        return
    for line in management.text.splitlines():
        if not _KEY.search(line) or not any((grant := _GRANT.search(s)) and _ANALYST.search(grant["recipient"])
                                            and not _DENIED.search(s) for s in sentences(line)):
            continue
        citations = [Citation(management, line), Citation(plan, reference)]
        if waiver:
            restriction = next((s.strip() for s in sentences(waiver.text)
                                if "연결키" in s and _ANALYST.search(s) and _DENIED.search(s)), "")
            if restriction:
                citations.append(Citation(waiver, restriction))
        yield _finding(management, "LINK_KEY_ACCESS", "연결키 열람 권한에 분석 역할이 추가됨",
                       "계획서는 연결키의 열람을 제한하지만, 자료관리계획서는 분석 역할에도 읽기 권한을 부여합니다.",
                       "연결키 열람 권한에서 분석 역할을 제외하고, 원천 자료 대조는 계획서의 연결키 담당자가 수행하도록 정리하십시오.",
                       "E06", "개인정보·이용·제공", citations)


def _retention_findings(plan: QCDoc, management: QCDoc) -> Iterator[dict]:
    def schedules(doc: QCDoc) -> list[tuple[str, str, str]]:
        return [(years[1], when.isoformat(), line) for paragraph in doc.text.splitlines() for line in sentences(paragraph)
                if _ANALYSIS_FILE.search(line) and (years := _AFTER_END.search(line))
                and (disposal := _DISPOSAL.search(line)) and (when := parse_date(*disposal.groups()))]

    planned, recorded = schedules(plan), schedules(management)
    if len(planned) != 1 or len(recorded) != 1 or planned[0][:2] == recorded[0][:2]:
        return
    years, date, quote = recorded[0]
    target_years, target_date, reference = planned[0]
    yield _finding(management, "ANALYSIS_RETENTION", "가명 분석 파일의 보관·폐기 일정 불일치",
                   f"자료관리계획서는 종료 후 {years}년·{date}, 연구계획서는 종료 후 {target_years}년·{target_date}입니다.",
                   f"분석 파일의 일정을 종료 후 {target_years}년·{target_date}에 맞추고, 연결키의 별도 폐기일은 유지하십시오.",
                   "E07", "보관·폐기", [Citation(management, quote), Citation(plan, reference)], exact=True)


def _waiver_findings(plan: QCDoc, waiver: QCDoc) -> Iterator[dict]:
    rationale = _section(waiver.text, r"동의.*(?:어려운\s*사유|곤란|불가능)|면제\s*(?:요청\s*)?사유\s*$")
    planned = _section(plan.text, r"동의\s*면제")
    barriers = [s.strip() for s in sentences(planned) if _WAIVER_BARRIERS.search(s)]
    if rationale and barriers and _CONVENIENCE.search(rationale) and not _WAIVER_BARRIERS.search(rationale):
        yield _finding(waiver, "WAIVER_RATIONALE", "동의면제 요청 사유에 연구별 소명 부족",
                       "면제 사유의 해당 항목에는 일정·작업 편의가 적혀 있고, 계획서에는 개별 재접촉의 제약이 따로 기재돼 있습니다.",
                       "계획서에 적힌 연락의 제약, 재접촉 시 정보 노출과 선택 편향 등 해당 근거를 면제 사유에 구체화하십시오. 면제 여부는 심의에서 판단합니다.",
                       "E04", "동의 경로", [Citation(waiver, rationale), *(Citation(plan, s) for s in barriers[:4])])


def retrospective_findings(plan: QCDoc, docs: list[QCDoc]) -> Iterator[dict]:
    """Keep each comparison tied to its document role and its own research scope."""
    if not _RECORDS.search(plan.text):
        return
    waivers = [doc for doc in docs if doc.doc_type == "waiver_request"]
    waiver = waivers[0] if len(waivers) == 1 else None
    for doc in docs:
        if doc.doc_type == "application":
            yield from _eligibility_findings(plan, doc)
        if doc.doc_type == "crf":
            rows = list(_table_rows(_section(doc.text, r"(?:추출|수집)\s*(?:필드|항목|변수)")))
            yield from _outcome_findings(plan, doc, rows)
            yield from _field_findings(plan, doc, rows)
        if doc.doc_type == "deid_plan":
            yield from _access_findings(plan, doc, waiver)
            yield from _retention_findings(plan, doc)
        if doc.doc_type == "waiver_request":
            yield from _waiver_findings(plan, doc)
