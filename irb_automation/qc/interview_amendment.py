"""Compare optional interview recording across revised submissions and operating instructions."""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Final, NamedTuple

from .consent_disclosures import sentences
from .extra_facts import _CURRENCY
from .structure import split_sections
from .study_consistency import Citation, _candidate

if TYPE_CHECKING:
    from .checks import QCDoc

_OPTIONAL: Final = re.compile(r"(?:거절|미선택|동의하지\s*않)[^.!?\n]{0,35}(?:필기|손으로\s*적)")
_REQUIRED_FOR_RECRUITMENT: Final = re.compile(
    r"녹음[^.!?\n]{0,12}동의[^.!?\n]{0,25}(?:분만|사람만|자만|경우에만)[^.!?\n]{0,25}(?:신청|참여|모집)"
)
_WITHDRAWAL_WINDOW: Final = re.compile(
    r"면담(?:일)?\s*다음\s*날[^.!?\n]{0,60}?(?<!\d)(?P<days>\d+)\s*일\s*(?:이내|째\s*까지)"
    r"(?=[^.!?\n]{0,110}(?:철회|삭제|지웁|지워|빼\s*달))"
)
_COMPLETED: Final = re.compile(r"완료자|면담(?:을)?\s*(?:마친|완료한)|면담\s*완료")
_RECONTACT: Final = re.compile(r"추가\s*연락|재접촉|재면담|연락[^.!?\n]{0,55}(?:추가\s*방문|다시\s*(?:녹음|만나))")
_NEGATION: Final = re.compile(r"하지\s*않|하지\s*못|보내지\s*않|금지|없(?:다|음|습니다)|제외한다")
_REOPEN_INTERVIEW: Final = re.compile(r"추가\s*방문|재면담|다시\s*(?:녹음|남기|만나)")
_OUTSIDE_AUDIO: Final = re.compile(r"외부[^.!?\n]{0,55}(?:음성|원음|녹음|전사)(?=[^.!?\n]{0,60}(?:업로드|전송|보내))")
_INTERNAL_TRANSCRIPT: Final = re.compile(r"(?:기관|연구소)\s*(?:내부|안)|내부\s*단말")
_AUDIO_DEADLINE: Final = re.compile(
    r"^(?:원음|(?:녹음|음성)\s*파일)[^.!?\n]{0,75}면담[^.!?\n]{0,20}?(?P<days>\d+)\s*일"
    r"(?=[^.!?\n]{0,55}(?:삭제|폐기|파기))"
)
_IMMEDIATE_REVIEW: Final = re.compile(r"(?:검수|확인)[^.!?\n]{0,15}(?:즉시|바로)")
_MID_INTERVIEW_STOP: Final = re.compile(r"중도\s*(?:종료|중단)|(?:도중|중간)에?\s*그만|완료\s*[·/]\s*중단|마치기\s*전에\s*그만")
_PAYMENT: Final = re.compile(_CURRENCY)
_OPS_TITLE: Final = re.compile(r"면담[\s_]*운영[\s_]*(?:지침|계획)")


class Observation(NamedTuple):
    value: int
    citation: Citation


def _policy_lines(text: str) -> Iterator[str]:
    for line in text.splitlines():
        yield from (sentence.strip() for sentence in sentences(line) if sentence.strip())


def _sections(doc: QCDoc, title: str) -> list[str]:
    return [section.body for section in split_sections(doc.text)
            if re.search(title, section.title) and not re.search(r"모의|가상|예시", section.title)]


def _unique_observation(doc: QCDoc, text: str, pattern: re.Pattern[str], group: str) -> Observation | None:
    observations = [Observation(int(hit[group]), Citation(doc, line))
                    for line in _policy_lines(text) if (hit := pattern.search(line))]
    return observations[0] if len({entry.value for entry in observations}) == 1 else None


def _finding(doc: QCDoc, code: str, title: str, detail: str, suggestion: str, rule: str, flag: str,
             quotes: list[Citation], *, explicit: bool = False) -> dict:
    finding = _candidate(doc, f"INTERVIEW_{code}", title, detail, suggestion, rule, flag, quotes)
    if explicit:
        finding.update(result="mismatch", reason="", question="", reference_blocked=True)
    return finding


def _recruitment_findings(plan: QCDoc, recruitment: QCDoc, optional: str) -> Iterator[dict]:
    for line in _policy_lines(recruitment.text):
        if _REQUIRED_FOR_RECRUITMENT.search(line) and not _NEGATION.search(line):
            yield _finding(recruitment, "RECORDING_REQUIREMENT", "모집공고가 녹음 동의를 참여 조건으로 제시함",
                           "계획서가 보장하는 필기 참여를 모집 단계에서 제한합니다.",
                           "녹음은 선택이고 거절해도 같은 면담과 사례비 조건으로 필기 참여가 가능하다고 안내하십시오.",
                           "E04", "동의 경로", [Citation(recruitment, line), Citation(plan, optional)])


def _withdrawal_findings(plan: QCDoc, consent: QCDoc, choices: list[QCDoc]) -> Iterator[dict]:
    reference = _unique_observation(plan, plan.text, _WITHDRAWAL_WINDOW, "days")
    participant = _unique_observation(consent, consent.text, _WITHDRAWAL_WINDOW, "days")
    if not reference or not participant or reference.value == participant.value:
        return
    other = [fact.citation for doc in choices if (fact := _unique_observation(doc, doc.text, _WITHDRAWAL_WINDOW, "days"))
             and fact.value == reference.value]
    yield _finding(consent, "WITHDRAWAL_WINDOW", "참여자 설명문의 자료 철회 기한 불일치",
                   f"면담 다음 날부터 계획서는 {reference.value}일, 설명문은 {participant.value}일을 안내합니다.",
                   f"자료 이용 철회 기한을 면담 다음 날부터 {reference.value}일 이내로 맞추고 면담 중 즉시 녹음 중단은 별도로 유지하십시오.",
                   "E04", "동의 경로", [participant.citation, reference.citation, *other], explicit=True)


def _operating_findings(plan: QCDoc, operations: QCDoc) -> Iterator[dict]:
    contact = "\n".join(_sections(operations, r"변경\s*시행|(?:집단|대상자)(?:별)?\s*연락|재접촉"))
    reference = next((line for line in _policy_lines(plan.text) if _COMPLETED.search(line)
                      and _RECONTACT.search(line) and _NEGATION.search(line)), "")
    for line in _policy_lines(contact):
        if reference and _COMPLETED.search(line) and _RECONTACT.search(line) and _REOPEN_INTERVIEW.search(line) and not _NEGATION.search(line):
            yield _finding(operations, "COMPLETED_RECONTACT", "변경 대상에서 제외된 완료자에게 재연락·추가 면담을 지시함",
                           "운영지침은 이미 면담을 마친 대상자의 추가 방문을 제안하지만, 계획서는 재접촉을 제한합니다.",
                           "완료자의 재연락·추가 방문 지시를 삭제하고 기존 필기자료만 사용하십시오. 추가선택은 이미 동의한 면담 전 참여자에게 받으십시오.",
                           "E05", "수행 절차·검체", [Citation(operations, line), Citation(plan, reference)])

    recording = "\n".join(_sections(operations, r"녹음|전사|폐기"))
    planned_lines = list(_policy_lines(plan.text))
    external = next((line for line in planned_lines if _OUTSIDE_AUDIO.search(line) and _NEGATION.search(line)), "")
    internal = next((line for line in planned_lines if "전사" in line and _INTERNAL_TRANSCRIPT.search(line)), "")
    for line in _policy_lines(recording):
        if internal and _OUTSIDE_AUDIO.search(line) and not _NEGATION.search(line):
            citations = [Citation(operations, line), Citation(plan, internal)]
            if external and external != internal:
                citations.append(Citation(plan, external))
            yield _finding(operations, "EXTERNAL_TRANSCRIPTION", "운영지침의 외부 음성 전사가 내부 전사 계획과 다름",
                           "운영지침에서 음성을 외부 서비스로 보내도록 하여 전사 주체와 자료 전달 범위가 바뀝니다.",
                           "외부 업로드 지시를 삭제하고 계획서에 기재된 담당자가 기관 내부에서 전사하고 검수하도록 정리하십시오.",
                           "E06", "개인정보·이용·제공", citations)

    reference_deadline = _unique_observation(plan, plan.text, _AUDIO_DEADLINE, "days")
    deadline = _unique_observation(operations, recording, _AUDIO_DEADLINE, "days")
    if reference_deadline and deadline:
        shortened_by_review = bool(_IMMEDIATE_REVIEW.search(reference_deadline.citation.quote))
        missing_review_limit = shortened_by_review and not _IMMEDIATE_REVIEW.search(deadline.citation.quote)
        if reference_deadline.value != deadline.value or missing_review_limit:
            limit = f"면담 후 {reference_deadline.value}일 이내"
            if shortened_by_review:
                limit = f"검수 완료 즉시, 늦어도 {limit}"
            yield _finding(operations, "AUDIO_RETENTION", "원음 보관·폐기 일정이 계획서와 다름",
                           f"운영지침의 원음 보관 기한은 면담 후 {deadline.value}일이고 계획서는 {limit} 삭제입니다.",
                           f"운영지침과 폐기대장에 원음을 {limit} 삭제하도록 정정하십시오.",
                           "E07", "보관·폐기", [deadline.citation, reference_deadline.citation], explicit=True)


def _early_payment(doc: QCDoc, lines: Iterator[str]) -> Observation | None:
    observed = []
    for line in lines:
        amounts = list(_PAYMENT.finditer(line))
        if len(amounts) != 1 or not _MID_INTERVIEW_STOP.search(line) or re.search(r"예산|손상\s*보상", line):
            continue
        amount = amounts[0]
        value = int(amount["amount"].replace(",", "")) * (10_000 if amount["unit"] else 1)
        observed.append(Observation(value, Citation(doc, line.strip())))
    return observed[0] if len({entry.value for entry in observed}) == 1 else None


def _payment_findings(plan: QCDoc, operations: QCDoc, participants: list[QCDoc]) -> Iterator[dict]:
    instructions = "\n".join(_sections(operations, r"중도\s*(?:종료|중단)|사례비\s*지급|지급\s*기록"))
    actual = _early_payment(operations, _policy_lines(instructions))
    reference = _early_payment(plan, _policy_lines(plan.text))
    if not actual or not reference or actual.value == reference.value:
        return
    corroborating = [entry.citation for doc in participants
                     if (entry := _early_payment(doc, _policy_lines(doc.text))
                         or _early_payment(doc, iter(doc.text.splitlines()))) and entry.value == reference.value]
    yield _finding(operations, "EARLY_STOP_PAYMENT", "면담 중도 종료 시 사례비 불일치",
                   f"운영지침은 중도 종료 시 {actual.value:,}원, 계획서는 {reference.value:,}원을 지급하도록 합니다.",
                   f"면담 시작 후 중단해도 {reference.value:,}원을 지급하도록 지급 기록을 정정하십시오.",
                   "E08", "비용·보상", [actual.citation, reference.citation, *corroborating], explicit=True)


def interview_findings(plan: QCDoc, docs: list[QCDoc]) -> Iterator[dict]:
    """Compare revised, operative terms for optional recording and its consequences."""
    if "면담" not in plan.text or "녹음" not in plan.text:
        return
    optional = next((line for line in _policy_lines(plan.text) if _OPTIONAL.search(line)), "")
    if not optional:
        return
    choices = [doc for doc in docs if doc.doc_type == "recording_choice"]
    participants = [doc for doc in docs if doc.doc_type in ("consent_form", "recruitment")]
    for doc in participants:
        if doc.doc_type == "recruitment":
            yield from _recruitment_findings(plan, doc, optional)
        else:
            yield from _withdrawal_findings(plan, doc, choices)
    for doc in docs:
        if doc.doc_type in ("questionnaire", "crf", "other") and _OPS_TITLE.search(f"{doc.name}\n{doc.text[:500]}"):
            yield from _operating_findings(plan, doc)
            yield from _payment_findings(plan, doc, participants)
