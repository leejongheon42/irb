"""가명처리 파이프라인: 읽기 → 탐지 → 처리 → 재식별 위험 평가 → 적정성 판정."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import transforms as T
from .detector import analyze_table, scan_text_pii
from .privacy import k_anonymity, l_diversity, suppress_below_k

ENCODINGS = ("utf-8-sig", "cp949", "euc-kr")
PSEUDONYM_ACTIONS = {"pseudonymize_hmac", "serial"}
REMOVING_ACTIONS = {"delete"} | PSEUDONYM_ACTIONS | {"mask"}


def decode_bytes(raw: bytes) -> tuple[str, str]:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("지원하지 않는 문자 인코딩입니다 (UTF-8, CP949/EUC-KR 지원).")


def read_csv_bytes(raw: bytes) -> tuple[list[str], list[list[str]], str]:
    text, enc = decode_bytes(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("빈 파일입니다.")
    headers = [h.strip() for h in rows[0]]
    if len(set(headers)) != len(headers):
        raise ValueError("중복된 컬럼명이 있습니다.")
    return headers, rows[1:], enc


def read_csv_path(path: Path) -> tuple[list[str], list[list[str]], str]:
    return read_csv_bytes(path.read_bytes())


def write_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def default_config(analysis: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    subject = None
    for col in analysis["columns"]:
        action = col["suggested_action"]
        params: dict[str, Any] = {}
        if action == "date_shift":
            params = {"max_days": int(policy.get("date_shift_max_days", 30))}
        elif action in ("birth_to_age_band", "age_band"):
            params = {"width": int(policy.get("age_band_years", 5)), "top_code": int(policy.get("age_top_code", 90))}
        elif action == "address_generalize":
            params = {"level": policy.get("address_level", "sigungu")}
        columns[col["name"]] = {"action": action, "params": params}
        if subject is None and col["detected_type"] in ("mrn",):
            subject = col["name"]
    if subject is None:
        for col in analysis["columns"]:
            if col["detected_type"] in ("rrn", "name"):
                subject = col["name"]
                break
    # 날짜 이동은 값 자체를 교란하므로 동질집합 계산(k-익명성) 대상에서 기본 제외한다.
    quasi = [
        c["name"]
        for c in analysis["columns"]
        if c["category"] == "quasi" and columns[c["name"]]["action"] != "date_shift"
    ]
    return {
        "columns": columns,
        "subject_column": subject,
        "quasi_identifiers": quasi,
        "sensitive_attributes": analysis.get("sensitive_attributes", []),
        "suppress_below_k": False,
        "k_threshold": int(policy.get("k_threshold", 5)),
        "reference_date": date.today().isoformat(),
    }


@dataclass
class ProcessResult:
    headers: list[str]
    rows: list[list[str]]
    mappings: dict[str, dict[str, str]] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)


def process(
    headers: list[str],
    rows: list[list[str]],
    config: dict[str, Any],
    *,
    secret: str,
    project_salt: str,
    policy: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> ProcessResult:
    col_cfg: dict[str, dict[str, Any]] = config.get("columns", {})
    pseudo = T.Pseudonymizer(secret, project_salt)
    ref_date = date.fromisoformat(config.get("reference_date") or date.today().isoformat())
    subject_col = config.get("subject_column")
    subject_idx = headers.index(subject_col) if subject_col in headers else None

    identifier_idx = [
        i for i, h in enumerate(headers) if col_cfg.get(h, {}).get("action") in REMOVING_ACTIONS
    ]
    out_idx = [i for i, h in enumerate(headers) if col_cfg.get(h, {}).get("action", "keep") != "delete"]
    out_headers = [headers[i] for i in out_idx]

    serial_maps: dict[str, dict[str, str]] = {}
    scrub_count = 0
    errors: dict[str, int] = {}
    out_rows: list[list[str]] = []

    for r_no, row in enumerate(rows):
        row = row + [""] * (len(headers) - len(row))
        subject_key = row[subject_idx] if subject_idx is not None else f"row{r_no}"
        known_ids = [row[i] for i in identifier_idx]
        new_row: list[str] = []
        for i in out_idx:
            h = headers[i]
            cfg = col_cfg.get(h, {"action": "keep"})
            action = cfg.get("action", "keep")
            params = cfg.get("params") or {}
            value = row[i]
            if not value.strip() and action != "keep":
                new_row.append("")
                continue
            try:
                if action == "keep":
                    out = value
                elif action == "pseudonymize_hmac":
                    out = params.get("prefix", "P") + pseudo.token(value, int(params.get("length", 16)))
                elif action == "serial":
                    m = serial_maps.setdefault(h, {})
                    key = value.strip()
                    if key not in m:
                        m[key] = f"{params.get('prefix', 'S')}{len(m) + 1:06d}"
                    out = m[key]
                elif action == "mask":
                    out = T.mask(value, params.get("kind", ""))
                elif action == "date_shift":
                    days = pseudo.offset_days(subject_key, int(params.get("max_days", policy.get("date_shift_max_days", 30))))
                    out = T.shift_date(value, days)
                elif action == "date_generalize":
                    out = T.generalize_date(value, params.get("unit", "year_month"))
                elif action == "birth_to_age_band":
                    d, _ = T.parse_date(value)
                    if d is None:
                        raise ValueError("date")
                    out = T.age_band(
                        T.age_at(d, ref_date),
                        int(params.get("width", policy.get("age_band_years", 5))),
                        int(params.get("top_code", policy.get("age_top_code", 90))),
                    )
                elif action == "age_band":
                    out = T.age_band(
                        int(float(value)),
                        int(params.get("width", policy.get("age_band_years", 5))),
                        int(params.get("top_code", policy.get("age_top_code", 90))),
                    )
                elif action == "address_generalize":
                    out = T.generalize_address(value, params.get("level", policy.get("address_level", "sigungu")))
                elif action == "zip_generalize":
                    digits = "".join(ch for ch in value if ch.isdigit())
                    out = digits[:3] + "**" if len(digits) >= 3 else ""
                elif action == "numeric_round":
                    out = T.round_numeric(value, float(params.get("base", 1)))
                elif action == "top_bottom_code":
                    lo = params.get("lower")
                    hi = params.get("upper")
                    out = T.top_bottom_code(
                        value,
                        float(lo) if lo not in (None, "") else None,
                        float(hi) if hi not in (None, "") else None,
                    )
                elif action == "text_scrub":
                    out, n = T.scrub_text(value, known_ids)
                    scrub_count += n
                else:
                    raise ValueError(f"unknown action {action}")
                if out == "" and value.strip() and action not in ("keep",):
                    errors[h] = errors.get(h, 0) + 1
            except (ValueError, TypeError):
                errors[h] = errors.get(h, 0) + 1
                out = ""  # 처리 불가 값은 안전하게 비움
            new_row.append(out)
        out_rows.append(new_row)

    # ---------------------------------------------------------- 재식별 위험 평가
    k = int(config.get("k_threshold") or policy.get("k_threshold", 5))
    qi_names = [q for q in config.get("quasi_identifiers", []) if q in out_headers]
    sa_names = [s for s in config.get("sensitive_attributes", []) if s in out_headers]
    qi_idx = [out_headers.index(q) for q in qi_names]
    sa_idx = [out_headers.index(s) for s in sa_names]

    raw_qi_idx = [headers.index(q) for q in config.get("quasi_identifiers", []) if q in headers]
    k_raw = k_anonymity(rows, raw_qi_idx, k)
    k_before = k_anonymity(out_rows, qi_idx, k)
    suppressed = 0
    if config.get("suppress_below_k") and qi_idx:
        out_rows, suppressed = suppress_below_k(out_rows, qi_idx, k)
    k_after = k_anonymity(out_rows, qi_idx, k)
    l_div = l_diversity(out_rows, qi_idx, sa_idx)
    l_named = {out_headers[i]: v for i, v in l_div.get("per_attribute", {}).items()}

    # ---------------------------------------------------------- 잔존 식별정보 점검
    residual: list[dict[str, Any]] = []
    input_direct = set((analysis or {}).get("direct_identifiers", []))
    for name in out_headers:
        action = col_cfg.get(name, {}).get("action", "keep")
        if name in input_direct and action == "keep":
            residual.append({"column": name, "issue": "직접 식별자가 처리되지 않고 유지됨"})
    out_analysis = analyze_table(out_headers, out_rows[:2000])
    for col in out_analysis["columns"]:
        action = col_cfg.get(col["name"], {}).get("action", "keep")
        if action in PSEUDONYM_ACTIONS:
            continue
        if col["category"] == "direct" and col["detected_type"] not in ("unknown",):
            residual.append({"column": col["name"], "issue": f"처리 후에도 {col['detected_label']} 패턴 탐지"})
        if action == "text_scrub":
            idx = out_headers.index(col["name"])
            left = scan_text_pii([r[idx] for r in out_rows[:2000]])
            left.pop("date", None)
            if left:
                residual.append({"column": col["name"], "issue": "텍스트 내 개인정보 잔존: " + ", ".join(f"{a} {b}건" for a, b in left.items())})

    # ---------------------------------------------------------- 적정성 판정
    max_ratio = float(policy.get("max_risk_record_ratio", 0.05))
    l_threshold = int(policy.get("l_threshold", 2))
    issues: list[str] = []
    warnings: list[str] = []
    if residual:
        issues.append("잔존 식별정보가 있습니다.")
    if k_after["applicable"] and k_after["ratio_below_k"] > max_ratio:
        issues.append(
            f"k={k} 미만 동질집합에 속한 레코드 비율 {k_after['ratio_below_k']:.1%}가 허용 기준 {max_ratio:.0%}를 초과합니다."
        )
    elif k_after["applicable"] and k_after["records_below_k"]:
        warnings.append(f"k={k} 미만 레코드 {k_after['records_below_k']}건 - 결과 공표 시 소규모 셀 억제 필요")
    if not k_after["applicable"]:
        warnings.append("준식별자가 지정되지 않아 k-익명성을 평가하지 않았습니다.")
    for name, l_val in l_named.items():
        if l_val < l_threshold:
            warnings.append(f"민감속성 '{name}'의 l-다양성({l_val})이 기준({l_threshold}) 미만 - 동질성 공격 위험")
    if errors:
        warnings.append("처리 불가 값 공란 처리: " + ", ".join(f"{c} {n}건" for c, n in errors.items()))

    if rows and not out_rows:
        issues.append("레코드 삭제 결과 남은 데이터가 없습니다. 준식별자 범주화 수준을 높이십시오.")
    elif rows and suppressed / len(rows) > 0.2:
        warnings.append(f"레코드 삭제 비율 {suppressed / len(rows):.0%} - 데이터 유용성 저하 및 선택편향 가능성")
    shifted = [h for h in out_headers if col_cfg.get(h, {}).get("action") == "date_shift"]
    if shifted:
        warnings.append("날짜 이동 컬럼(" + ", ".join(shifted) + ")은 교란 처리되어 k-익명성 평가에서 제외됨 - 원본 일자 노출 금지")

    verdict = "부적정" if issues else ("조건부 적정" if warnings else "적정")

    report = {
        "input_rows": len(rows),
        "output_rows": len(out_rows),
        "suppressed_records": suppressed,
        "input_columns": len(headers),
        "output_columns": len(out_headers),
        "deleted_columns": [h for h in headers if col_cfg.get(h, {}).get("action") == "delete"],
        "column_actions": [
            {"column": h, "action": col_cfg.get(h, {}).get("action", "keep"), "description": T.describe(col_cfg.get(h, {"action": "keep"}))}
            for h in headers
        ],
        "subject_column": subject_col,
        "quasi_identifiers": qi_names,
        "sensitive_attributes": sa_names,
        "k_anonymity_raw": k_raw,
        "k_anonymity_before_suppression": k_before,
        "k_anonymity": k_after,
        "l_diversity": l_named,
        "text_scrubbed": scrub_count,
        "processing_errors": errors,
        "residual_identifiers": residual,
        "issues": issues,
        "warnings": warnings,
        "verdict": verdict,
        "policy": {"k_threshold": k, "l_threshold": l_threshold, "max_risk_record_ratio": max_ratio},
        "mapping_tables": sorted(serial_maps),
    }
    return ProcessResult(out_headers, out_rows, serial_maps, report)
