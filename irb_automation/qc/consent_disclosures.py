"""Keep participant contact and injury coverage tied to their own context."""
from __future__ import annotations

import re
from collections.abc import Iterator

_SENTENCE_BREAK = re.compile(r"(?<=[.!?。])\s+|\n(?=\s*(?:#|\d+[.)]\s))")
_COMMITTEE = re.compile(r"기관\s*생명\s*윤리\s*위원회|연구\s*윤리\s*위원회|\bIRB\b|권리\s*문의", re.I)
_CONTACT = re.compile(r"\d{2,4}\s*[-)]\s*\d{3,4}\s*-\s*\d{4}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\[(?:전화번호|이메일)\]")
_INVESTIGATOR = re.compile(r"연구\s*책임자|담당\s*(?:연구자|연구원)|연구\s*담당자")
_INJURY = re.compile(r"손상|상해|연구(?:로|에)?\s*(?:인한|관련)\s*피해|부작용|이상\s*(?:증상|반응)")
_CARE = re.compile(r"필요한\s*진료|의료진|치료")
_COVERAGE = re.compile(r"치료\s*비(?:용)?[^.!?\n]{0,35}(?:부담|지원|지급)|(?:보상|배상)[^.!?\n]{0,35}(?:신청|청구|절차|규정|제공|지급|받|없)|(?:보험|배상\s*책임)\s*(?:가입|적용|범위)")
_RESEARCH_COST = re.compile(r"교통비|사례비|참여\s*보상")


def sentences(text: str) -> list[str]:
    return _SENTENCE_BREAK.split(text)


def _contact_windows(text: str) -> Iterator[str]:
    for sentence in sentences(text):
        lines = sentence.splitlines()
        for index, line in enumerate(lines):
            yield line
            if index + 1 < len(lines) and _COMMITTEE.search(line):
                following = lines[index + 1]
                if re.match(r"\s*(?:\(?\d{2,4}[-)]|전화|이메일|연락처|\[(?:전화번호|이메일)\])", following):
                    yield f"{line}\n{following}"


def has_committee_contact(text: str) -> bool:
    for window in _contact_windows(text):
        for contact in _CONTACT.finditer(window):
            before = window[:contact.start()]
            committee = list(_COMMITTEE.finditer(before))
            investigator = list(_INVESTIGATOR.finditer(before))
            if committee and (not investigator or committee[-1].end() > investigator[-1].end()):
                return True
    return False


def has_injury_coverage(text: str) -> bool:
    for sentence in sentences(text):
        if _INJURY.search(sentence) and _COVERAGE.search(sentence) and not _RESEARCH_COST.search(sentence):
            return True
    return False


def disclosure_evidence(item_id: str, text: str) -> str:
    if item_id == "irb_contact":
        lines = reversed(text.splitlines())
        return next((line.strip() for line in lines if _INVESTIGATOR.search(line) and _CONTACT.search(line)),
                    next((line.strip() for line in text.splitlines() if re.search(r"연락처\s*및\s*질문|문의\s*안내", line)), ""))
    return next((sentence.strip() for sentence in sentences(text) if _INJURY.search(sentence) and _CARE.search(sentence)), "")
