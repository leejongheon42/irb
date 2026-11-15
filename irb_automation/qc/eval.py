"""Small, offline extraction benchmark; not a clinical accuracy estimate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, TypeAdapter

from .facts import extract


class ExtractionCase(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    expected: tuple[tuple[str, str], ...]


class EvaluationReport(TypedDict):
    scope: str
    cases: int
    expected_facts: int
    detected_facts: int
    false_positives: int
    recall: float
    precision: float
    reproducibility: float
    repetitions: int


class DecisionCase(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    docs: tuple[str, ...]
    profile: dict[str, str | bool | list[str]] | None = None
    expected: tuple[tuple[str, str], ...] = ()
    must_not: tuple[tuple[str, str], ...] = ()


def evaluate_decisions() -> dict[str, int | list[str]]:
    """Check outcomes and prohibited findings on synthetic bundles."""
    from .checks import QCDoc, run_rules
    from .profile import build_profile, profile_conditions
    from .units import annotate

    raw = Path(__file__).with_name("decision_cases.json").read_text(encoding="utf-8")
    cases = TypeAdapter(list[DecisionCase]).validate_json(raw)
    failures = []
    false_positives = 0
    for case in cases:
        docs = [QCDoc(f"d{i}", f"d{i}_v3.0.txt", "protocol" if i == 0 else "consent_form", body)
                for i, body in enumerate(case.docs)]
        profile = build_profile(case.profile, None, docs)
        runs = []
        for _ in range(3):
            first, third = run_rules(docs, None, profile_conditions(profile), profile)
            runs.append([(issue["code"], annotate(issue)["result"]) for issue in first + third])
        observed = runs[0]
        forbidden = sum(1 for code, result in observed if (code, result) in case.must_not or (code, "*") in case.must_not)
        false_positives += forbidden
        if forbidden or any(observed.count(pair) != case.expected.count(pair) for pair in case.expected) or not (
            runs[0] == runs[1] == runs[2]
        ):
            failures.append(case.name)
    return {"cases": len(cases), "failed": failures, "false_positives": false_positives}


def evaluate() -> EvaluationReport:
    """Compare extracted facts with independently labeled, synthetic examples."""
    raw = Path(__file__).with_name("eval_cases.json").read_text(encoding="utf-8")
    cases = TypeAdapter(list[ExtractionCase]).validate_json(raw)
    true_positive = false_positive = expected_count = reproducible = 0
    for case in cases:
        expected = set(case.expected)
        runs = [{(f.key, f.value) for f in extract(case.text)} for _ in range(3)]
        true_positive += len(expected & runs[0])
        false_positive += len(runs[0] - expected)
        expected_count += len(expected)
        reproducible += int(runs[0] == runs[1] == runs[2])
    return {
        "scope": "synthetic_fact_extraction_only", "cases": len(cases),
        "expected_facts": expected_count, "detected_facts": true_positive,
        "false_positives": false_positive,
        "recall": true_positive / expected_count if expected_count else 1.0,
        "precision": true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0,
        "reproducibility": reproducible / len(cases) if cases else 1.0, "repetitions": 3,
    }


def main(output: str | None = None) -> int:
    """Print JSON and optionally persist the same report."""
    extraction = evaluate()
    decisions = evaluate_decisions()
    report = {**extraction, "decision_evaluation": decisions}
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if extraction["recall"] >= 0.9 and extraction["precision"] >= 0.9 and extraction["reproducibility"] == 1 and not decisions["failed"] else 1
