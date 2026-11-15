"""Export the current QC review record as a Word document."""
from io import BytesIO

from docx import Document

from .models import QCRun

_RESULTS = {"match": "일치", "mismatch": "불일치", "missing": "필수내용 누락",
            "needs_check": "확인 필요", "na": "해당 없음"}


def render_report(run: QCRun) -> bytes:
    doc = Document()
    doc.add_heading("서류 점검 결과", 0)
    doc.add_paragraph(f"점검 #{run.id} · {run.title} · 기준 {run.criteria_version}")
    score = "산출 불가" if run.counts.get("na") else f"{run.score}점"
    doc.add_paragraph(f"QC 상태 {run.verdict} · 확정 점수 {score}")
    if run.counts.get("projected") is not None:
        doc.add_paragraph(f"예상 점수 {run.counts['projected']}점")
    if run.counts.get("coverage") is not None:
        doc.add_paragraph(f"검사 완료율 {run.counts['coverage']:.0%}")
    doc.add_paragraph("문서 점검 결과는 IRB 승인·면제 확인을 뜻하지 않습니다.")
    doc.add_paragraph(" / ".join(f"{k.title()} {run.counts.get(k, 0)}건" for k in ("critical", "major", "minor")))
    doc.add_heading("제출 문서", level=1)
    for entry in run.docs:
        doc.add_paragraph(f"{entry['name']} · {entry['label']} · " + " / ".join(entry.get("warnings", [])))
    if run.profile:
        doc.add_heading("적용 조건과 출처", level=1)
        for key, source in run.profile.get("source", {}).items():
            doc.add_paragraph(f"{key}: {run.profile.get(key)} ({source})")
    if run.checks:
        doc.add_heading("검사 단위", level=1)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for cell, text in zip(table.rows[0].cells, ("규칙·항목", "범위", "판정", "문서별 값"), strict=True):
            cell.text = text
        for check in run.checks:
            cells = table.add_row().cells
            values = (f"{check['rule']} · {check.get('label') or check['field']}", check["scope"],
                      _RESULTS[check["result"]], "\n".join(f"{loc['name']}: {loc['norm']}" for loc in check.get("locations", [])))
            for cell, value in zip(cells, values, strict=True):
                cell.text = str(value)
    doc.add_heading("검토 이슈", level=1)
    for issue in run.issues:
        doc.add_heading(f"{issue['id']}. {issue['title']}", level=2)
        doc.add_paragraph(" · ".join(str(issue[k]) for k in ("rule_id", "rule_source", "rule_version",
                          "severity", "confidence", "review") if issue.get(k)))
        if issue.get("flags"):
            doc.add_paragraph(" · ".join(issue["flags"]))
        for key in ("doc_name", "detail", "evidence", "basis", "section", "reason", "question", "review_note"):
            if issue.get(key):
                doc.add_paragraph(str(issue[key]))
        for location in issue.get("locations", []):
            doc.add_paragraph(f"{location['name']}: {location['quote']} ({location['norm']})")
        for location in issue.get("ai_evidence", []):
            doc.add_paragraph(f"AI 인용 · {location['name']}: {location['quote']}")
        if issue.get("check_answer"):
            doc.add_paragraph(f"확인 기록: {issue['check_answer']['note']}")
    with BytesIO() as buffer:
        doc.save(buffer)
        return buffer.getvalue()
