"""감사 추적(Audit trail).

모든 상태 변경·문서 열람·데이터 반출 이벤트를 기록하며, 각 행은 직전 행의 해시를 포함하는
해시 체인으로 연결되어 사후 위변조 여부를 검증할 수 있다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, User
from .timeutil import now


def _row_digest(prev_hash: str, entry: AuditLog) -> str:
    payload = json.dumps(
        {
            "prev": prev_hash,
            "ts": entry.timestamp.isoformat(),
            "actor": entry.actor_id,
            "action": entry.action,
            "entity": [entry.entity_type, entry.entity_id],
            "details": entry.details,
            "ip": entry.ip_address,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def record(
    db: Session,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    details: dict[str, Any] | None = None,
    ip: str = "",
) -> AuditLog:
    last = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
    prev_hash = last.row_hash if last else ""
    entry = AuditLog(
        timestamp=now(),
        actor_id=actor.id if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        ip_address=ip,
        prev_hash=prev_hash,
    )
    entry.row_hash = _row_digest(prev_hash, entry)
    db.add(entry)
    db.flush()
    return entry


def verify_chain(db: Session) -> tuple[bool, int | None]:
    """해시 체인 검증. (정상 여부, 최초 불일치 행 id)"""
    prev = ""
    for entry in db.scalars(select(AuditLog).order_by(AuditLog.id)):
        if entry.prev_hash != prev or _row_digest(prev, entry) != entry.row_hash:
            return False, entry.id
        prev = entry.row_hash
    return True, None


ACTION_LABELS = {
    "login": "로그인",
    "login_failed": "로그인 실패",
    "protocol_create": "과제 생성",
    "protocol_update": "과제 수정",
    "document_upload": "서류 업로드",
    "document_download": "서류 열람",
    "submit": "심의 신청",
    "prereview": "자동 사전검토",
    "request_revision": "보완 요청",
    "assign_reviewers": "심의위원 배정",
    "review_submit": "심의의견 제출",
    "decision": "심의 결정",
    "start_amendment": "변경 신청 시작",
    "status_change": "상태 변경",
    "expire": "승인 만료",
    "comment": "의견 등록",
    "deid_upload": "가명처리 원본 등록",
    "deid_process": "가명처리 수행",
    "deid_release": "가명정보 제공",
    "deid_download": "가명정보 다운로드",
    "deid_delete_input": "원본 파일 파기",
    "user_create": "사용자 생성",
    "user_update": "사용자 수정",
    "letter_view": "통지서 출력",
    "esign": "전자서명",
    "revision_response": "보완 답변 작성",
    "document_generate": "서류 자동 작성",
    "meeting_create": "회의 생성",
    "meeting_agenda": "안건 구성",
    "meeting_attendance": "출석 확인",
    "meeting_vote": "안건 표결",
    "meeting_close": "회의 종료",
    "output_submit": "반출 심사 요청",
    "output_review": "반출 심사 결정",
    "output_download": "반출 결과 다운로드",
    "agreement_create": "계약 등록",
    "agreement_terminate": "계약 해지",
    "destruction_certify": "파기 확인",
    "drb_review": "가명처리 적정성 검토",
    "feasibility_query": "대상자 수 사전 조회",
    "export": "자료 내보내기",
    "template_upload": "기관 서식 등록",
    "template_mapping": "기관 서식 매핑 저장",
    "template_activate": "기관 서식 활성화",
    "template_deactivate": "기관 서식 비활성화",
    "template_fill": "기관 서식 채우기",
    "template_download": "기관 서식 원본 다운로드",
    "knowledge_search": "지식 검색",
    "knowledge_answer": "근거 질의응답",
    "knowledge_upload": "지식 문서 등록",
    "knowledge_update": "지식 문서 변경",
    "knowledge_delete": "지식 문서 삭제",
    "knowledge_reindex": "지식 재색인",
    # 웹 문서 제출
    "document_reject": "서류 제출 거부(검증 실패)",
    "document_review": "서류 검토",
    "document_comment": "서류 의견",
    "document_preview": "서류 미리보기",
    "document_bundle": "심의자료 합본 다운로드",
    "document_stamp": "승인 도장본 생성",
    "ai_transmit": "AI 전송",
    "pipeline_start": "문서 분석 시작",
    "pipeline_finish": "문서 분석 완료",
    "pipeline_apply": "분석 결과 적용",
}

# === 기관 양식(forms) ===
ACTION_LABELS.update({
    "profile_update": "기관 프로필 변경",
    "form_save": "양식 정의 저장",
    "form_activate": "양식 활성화",
    "form_deactivate": "양식 비활성화",
    "form_pack_import": "양식 팩 가져오기",
    "form_pack_export": "양식 팩 내보내기",
    "online_form_save": "웹 서식 임시저장",
    "online_form_submit": "웹 서식 제출",
})
