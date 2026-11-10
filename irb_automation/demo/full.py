"""전체 파이프라인 테스트: 샘플 계획서 하나로 연구자 흐름 전체를 자동 실행하고 단계별 결과를 남긴다.

  ① 기존 계획서 AI 인식 → ② 과제 생성 → ③ IRB 사전점검 → ④ 서류 자동 완성(초안·작성·점검)
  → ⑤ 개인 서류 첨부 → ⑥ 오류 주입 서류 점검 → ⑦ 자동 교정 → ⑧ 재점검 → ⑨ e-IRB ZIP
  → ⑩ 제출 기록 → ⑪ 보완 요청 입력 → ⑫ 답변 초안·저장 → ⑬ 보완 답변서 → ⑭ 재제출 → ⑮ 승인 입력

각 단계는 독립적으로 기록되며, 앞 단계가 실패하면 그 결과가 필요한 뒤 단계는 건너뛴다.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from .. import audit
from ..models import Protocol, User
from ..timeutil import now
from .models import DemoRun
from .samples import BY_KEY, REVISION_REQUEST

log = logging.getLogger(__name__)


LABELS = {"record": "과제 정보 불일치", "terminology": "용어 혼용", "typo": "오탈자", "cross": "교차 문서 불일치",
          "version": "버전 불일치"}


class _Skip(Exception):
    pass


class Runner:
    def __init__(self, db: Session, user: User, use_ai: bool):
        self.db, self.user, self.use_ai = db, user, use_ai
        self.steps: list[dict[str, Any]] = []
        self.protocol: Protocol | None = None
        self.qc_first = None
        self.qc_fixed = None

    def step(self, title: str, fn: Callable[[], tuple[str, str, str]], needs_protocol: bool = True) -> None:
        started = time.monotonic()
        entry: dict[str, Any] = {"n": len(self.steps) + 1, "title": title}
        try:
            if needs_protocol and self.protocol is None:
                raise _Skip("앞 단계에서 과제가 만들어지지 않아 건너뛰었습니다.")
            status, message, link = fn()
            self.db.commit()
        except _Skip as e:
            self.db.rollback()
            status, message, link = "skipped", str(e), ""
        except Exception as e:  # 한 단계 실패가 전체 테스트를 멈추지 않게
            log.exception("demo step failed: %s", title)
            self.db.rollback()
            status, message, link = "failed", f"{type(e).__name__}: {e}", ""
        entry.update(status=status, message=message, link=link, seconds=round(time.monotonic() - started, 1))
        self.steps.append(entry)


def run_full_pipeline(db: Session, user: User, *, use_ai: bool = False, ip: str = "") -> DemoRun:
    from ..package import revision as rv
    from ..package import service as pkg
    from ..pipeline import service as ps
    from ..qc import autofix
    from ..qc import service as qcs
    from ..submission import service as subs
    from ..workflow import service as wf

    r = Runner(db, user, use_ai)
    demo = DemoRun(created_by=user.id, status="running", steps=[])
    db.add(demo)
    db.commit()
    state: dict[str, Any] = {}

    def s1():
        sample = BY_KEY["import_docx"]
        run = ps.start_import_pipeline(db, user, sample.filename, sample.build(), sync=True)
        state["import_run"] = run
        fields = ps.latest_fields(run)
        ok = [k for k, v in fields.items() if v.get("value") not in (None, "", [])]
        st = "ok" if run.status in ("succeeded", "partial") and ok else "warn"
        return st, f"'{sample.filename}' 분석 {run.status} · 추출 항목 {len(ok)}개", f"/pipeline/{run.id}"

    def s2():
        run = state.get("import_run")
        if run is None:
            raise _Skip("가져오기 결과가 없습니다.")
        selected = set(ps.latest_fields(run).keys())
        p = ps.apply_import(db, user, run, selected)
        p.title = "[파이프라인 테스트] " + (p.title or "샘플 과제")
        if not p.study_start:
            p.study_start = date.today() + timedelta(days=30)
        if not p.study_end:
            p.study_end = date.today() + timedelta(days=400)
        if not p.expected_subjects:
            p.expected_subjects = 1200
        p.identifiers_collected = p.identifiers_collected or ["mrn"]
        db.flush()
        r.protocol = p
        demo.protocol_id = p.id
        return "ok", f"과제 생성: 대상자 {p.expected_subjects:,}명 · 기간 {p.study_start} ~ {p.study_end}", f"/protocols/{p.id}"

    def s3():
        res = wf.preview(db, r.protocol)
        crit = sum(1 for f in res["findings"] if f["severity"] == "critical")
        return "ok", f"예상 {res['review_type_label']} · 위험도 {res['risk']['score']}점 · 필수 보완 {crit}건", \
            f"/protocols/{r.protocol.id}#precheck"

    def s4():
        out = pkg.autocomplete(db, user, r.protocol, use_ai=use_ai)
        qr = out["qc_run"]
        state["first_package_qc"] = qr
        msg = (f"초안 {len(out['drafted'])}개 · 서류 {len(out['generated'])}건 작성"
               + (f" · 실패 {len(out['failed'])}건" if out["failed"] else "")
               + (f" · 점검 {qr.score}점 {qr.verdict}" if qr else ""))
        fmts = {Path(g["filename"]).suffix for g in out["generated"]}
        st = "ok" if out["generated"] and not out["failed"] else "warn"
        if ".html" in fmts:
            st, msg = "warn", msg + " · LibreOffice가 없어 일부 서류를 HTML로 작성"
        return st, msg, f"/protocols/{r.protocol.id}/package"

    def s5():
        names = []
        for key, doc_type in (("cv", "cv"), ("training", "training_cert"), ("coi", "coi")):
            smp = BY_KEY[key]
            subs.store_document(db, user, r.protocol, doc_type=doc_type, filename=smp.filename, raw=smp.build(),
                                run_hook=False)
            names.append(smp.filename)
        return "ok", "첨부: " + ", ".join(names), f"/protocols/{r.protocol.id}#documents"

    def s6():
        from ..submission.versions import latest_by_type

        db.refresh(r.protocol)
        cur = latest_by_type(list(r.protocol.documents)).get("protocol")
        if cur is None or not cur.has_file or not cur.filename.lower().endswith(".docx"):
            raise _Skip("DOCX 연구계획서가 없어 오류 주입을 건너뛰었습니다(LibreOffice 필요).")
        raw = Path(cur.stored_path).read_bytes()
        n = r.protocol.expected_subjects
        inject = [{"old": f"{n:,}명", "new": f"{n + 300:,}명", "all": False},
                  {"old": "연구대상자", "new": "피험자", "all": False},
                  {"old": "개인정보", "new": "개인정부", "all": False}]
        broken, rep, _ = autofix.apply(cur.filename, raw, inject, mode="clean", highlight=False)
        subs.store_document(db, user, r.protocol, doc_type="protocol", filename=cur.filename, raw=broken,
                            run_hook=False)
        db.flush()
        qr = qcs.run_for_protocol(db, user, r.protocol, use_ai=use_ai)
        r.qc_first = qr
        hit = {i["category"] for i in qr.issues}
        found = [x for x in ("record", "terminology", "typo") if x in hit]
        st = "ok" if len(found) == 3 else "warn"
        return st, (f"주입한 오류 {rep.total}곳(대상자 수·용어·오탈자) → 점검 {qr.score}점 {qr.verdict}, "
                    f"Critical {qr.counts['critical']} · Major {qr.counts['major']} · Minor {qr.counts['minor']} "
                    f"(탐지: {', '.join(LABELS[x] for x in found) or '없음'})"), f"/qc/{qr.id}"

    def s7():
        qr = r.qc_first or state.get("first_package_qc")
        if qr is None:
            raise _Skip("점검 결과가 없습니다.")
        ids = [i["id"] for i in qcs.fixable_issues(qr)]
        if not ids:
            return "warn", "자동 교정 가능한 이슈가 없습니다.", f"/qc/{qr.id}"
        outs = qcs.apply_fixes(db, user, qr, ids, font="맑은 고딕")
        applied = sum(o.get("applied", 0) for o in outs)
        saved = sum(1 for o in outs if o.get("version"))
        return "ok", f"교정 {applied}곳 · 새 버전 저장 {saved}건 · 변경 추적본/수정 완료본 생성", f"/qc/{qr.id}#outputs"

    def s8():
        db.refresh(r.protocol)
        qr = qcs.run_for_protocol(db, user, r.protocol, use_ai=use_ai)
        r.qc_fixed = qr
        before = r.qc_first.score if r.qc_first else None
        left = {i["category"] for i in qr.issues} & {"record", "terminology", "typo", "cross", "version"}
        st = "ok" if not left else "warn"
        msg = f"재점검 {qr.score}점 {qr.verdict}" + (f" (교정 전 {before}점)" if before is not None else "")
        msg += (f" · 남은 이슈: {', '.join(LABELS.get(x, x) for x in sorted(left))}" if left else " · 주입한 오류 모두 해소")
        return st, msg, f"/qc/{qr.id}"

    def s9():
        import io
        import zipfile

        data, name = pkg.build_zip(db, user, r.protocol)
        files = zipfile.ZipFile(io.BytesIO(data)).namelist()
        return "ok", f"{name} · 파일 {len(files)}개 · {len(data) // 1024}KB", f"/protocols/{r.protocol.id}/package.zip"

    def s10():
        sub = wf.record_submission(db, user, r.protocol, external_ref="DEMO-E-0001", force=True)
        return "ok", f"{sub.kind.label} 제출 기록 · 예상 {sub.review_type.label}", f"/protocols/{r.protocol.id}#history"

    def s11():
        items = wf.record_revision_request(db, user, r.protocol, REVISION_REQUEST)
        state["items"] = items
        return "ok", f"보완 요청 {len(items)}개 항목으로 분리", f"/protocols/{r.protocol.id}/revision"

    def s12():
        items = state.get("items") or wf.open_revision_items(db, r.protocol)
        drafts = rv.draft_responses(r.protocol, items, use_ai=use_ai, db=db, user=user)
        wf.respond_revision_items(db, user, r.protocol, {i.id: drafts[i.id]["response"] for i in items})
        src = {d["source"] for d in drafts.values()}
        return "ok", f"답변 {len(items)}건 저장({'AI 초안' if 'ai' in src else '표준 문안'}) · 고칠 서류 안내 포함", \
            f"/protocols/{r.protocol.id}/revision"

    def s13():
        from ..documents import generate_and_attach

        doc = generate_and_attach(db, user, r.protocol, "revision_response")
        return "ok", f"보완 답변서 첨부: {doc.filename}", f"/documents/{doc.id}/preview"

    def s14():
        sub = wf.record_submission(db, user, r.protocol, force=True)
        return "ok", f"{sub.kind.label} 기록", f"/protocols/{r.protocol.id}#history"

    def s15():
        sub = wf.record_result(db, user, r.protocol, "approved", irb_no="DEMO-IRB-0001", note="파이프라인 테스트 승인")
        return "ok", f"IRB 결과 '{sub.result_label}' · 승인 만료 {r.protocol.expires_at}", f"/protocols/{r.protocol.id}"

    plan = [
        ("기존 계획서 AI 문서 인식(다른 병원 양식 DOCX)", s1, False),
        ("추출 정보로 새 과제 생성", s2, False),
        ("IRB 사전점검(예상 심의유형·필수 보완)", s3, True),
        ("제출 서류 패키지 자동 완성(초안·서류 작성·점검)", s4, True),
        ("개인 서류 첨부(이력서·교육 이수증·이해상충)", s5, True),
        ("오류 주입 후 서류 교차 점검", s6, True),
        ("자동 교정(변경 추적본·수정 완료본·새 버전)", s7, True),
        ("교정본 재점검", s8, True),
        ("e-IRB 업로드용 ZIP 생성", s9, True),
        ("e-IRB 제출 기록", s10, True),
        ("IRB 보완 요청 입력(항목 분리)", s11, True),
        ("보완 답변 초안 작성·저장", s12, True),
        ("보완 답변서 자동 작성", s13, True),
        ("보완 재제출 기록", s14, True),
        ("IRB 승인 결과 입력", s15, True),
    ]
    for title, fn, needs in plan:
        r.step(title, fn, needs_protocol=needs)
    demo = db.get(DemoRun, demo.id)
    demo.steps = r.steps
    demo.protocol_id = r.protocol.id if r.protocol else demo.protocol_id
    demo.finished_at = now()
    counts = {k: sum(1 for s in r.steps if s["status"] == k) for k in ("ok", "warn", "failed", "skipped")}
    demo.status = "failed" if counts["failed"] else ("warn" if counts["warn"] or counts["skipped"] else "ok")
    demo.summary = {**counts, "ai": use_ai,
                    "qc_before": r.qc_first.score if r.qc_first else None,
                    "qc_after": r.qc_fixed.score if r.qc_fixed else None}
    audit.record(db, user, "demo_pipeline", "demo_run", demo.id, demo.summary, ip)
    db.commit()
    return demo
