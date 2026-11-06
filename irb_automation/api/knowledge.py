"""관련 정보 검색 API (로그인 필요).

GET  /api/knowledge/search?q=...&k=5&kind=law&kind=guideline
POST /api/knowledge/answer  {"question": "...", "k": 6}
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..knowledge import service as ks
from ..models import User
from ..web.deps import client_ip, current_user

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _plain(hit: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in hit.items() if k != "highlight"}


@router.get("/search")
def api_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    k: int = Query(5, ge=1, le=50),
    kind: list[str] | None = Query(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    hits = ks.search(db, q, k=k, kinds=kind or None, user=user)
    ks.record_query(db, user, q, "search", len(hits), client_ip(request))
    db.commit()
    return [_plain(h) for h in hits]


class AnswerIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    k: int = Field(6, ge=1, le=10)


@router.post("/answer")
def api_answer(body: AnswerIn, request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    result = ks.answer(db, body.question, k=body.k, user=user, ip=client_ip(request))
    db.commit()
    result["hits"] = [_plain(h) for h in result.get("hits", [])]
    return result
