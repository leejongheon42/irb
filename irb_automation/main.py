"""FastAPI 애플리케이션."""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager, suppress
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api.knowledge import router as knowledge_api_router
from .api.routes import router as api_router
from .config import get_settings
from .database import SessionLocal, init_db, sync_secrets
from .web import (
    routes_admin,
    routes_deid,
    routes_demo,
    routes_doctemplates,
    routes_forms,
    routes_knowledge,
    routes_main,
    routes_mvp,
    routes_package,
    routes_pipeline,
    routes_protocols,
    routes_public_irb,
    routes_reports,
    routes_submission,
    routes_super_admin,
)
from .web.deps import LoginRequired, current_user_optional, render

log = logging.getLogger("irb")
VIEWER_FRAME_PATH = re.compile(r"^/mvp/review/\d+/(documents/[^/]+|evidence/\d+/\d+\.png)$")


async def _scheduler_loop(interval: int) -> None:
    from .workflow.service import run_scheduler

    while True:
        try:
            with SessionLocal() as db:
                stats = run_scheduler(db)
                db.commit()
                log.info("scheduler: %s", stats)
        except Exception:  # pragma: no cover - 운영 안정성
            log.exception("scheduler failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    from .knowledge.service import ensure_ready

    with SessionLocal() as db:  # 내장 지식을 시작 시 한 번 적재(요청마다 동시에 적재하는 경합 방지)
        try:
            ensure_ready(db)
            db.commit()
        except Exception:  # pragma: no cover - 지식 검색은 첫 사용 시 다시 시도한다
            log.exception("knowledge seeding failed")
    if settings.builtin_accounts:
        from .seed import ensure_builtin_accounts

        with SessionLocal() as db:
            created = ensure_builtin_accounts(db)
            db.commit()
        if created:
            log.info("builtin accounts: %s", ", ".join(created))
    if settings.super_admin_password:
        from .seed import ensure_super_admin

        with SessionLocal() as db:
            try:
                created_super_admin = ensure_super_admin(db, settings.super_admin_password)
            except ValueError as error:
                log.error("%s", error)
            else:
                db.commit()
                if created_super_admin:
                    log.info("super_admin account created")
    task = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(_scheduler_loop(settings.scheduler_interval_seconds))
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    settings = get_settings()
    sync_secrets(settings)  # PostgreSQL: 세션·가명처리 키를 DB에서 불러와 재배포 후에도 유지
    app = FastAPI(
        title="IRB 자동화 솔루션",
        description="병원 의료데이터 활용 연구를 위한 IRB 심의 자동화 API",
        version=__version__,
        lifespan=lifespan,
    )
    @app.middleware("http")
    async def ai_tier_for_session(request: Request, call_next):
        # 세션 미들웨어보다 먼저 등록해 그 안쪽에서 실행된다(request.session 사용 가능).
        from .ai import tier

        token = tier.set_current(request.session.get("ai_tier") if request.session.get("uid") else None)
        try:
            return await call_next(request)
        finally:
            tier.reset(token)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="irb_session",
        max_age=60 * 60 * 8,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
            )
        return response

    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(routes_main.router)
    app.include_router(routes_mvp.router)
    app.include_router(routes_submission.router)  # /documents/{id}/preview 등 세부 경로를 먼저 등록
    app.include_router(routes_demo.router)
    app.include_router(routes_package.router)
    app.include_router(routes_pipeline.router)  # /protocols/import 가 /protocols/{pid} 보다 먼저
    app.include_router(routes_protocols.router)
    app.include_router(routes_public_irb.router)
    app.include_router(routes_deid.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_super_admin.router)
    app.include_router(routes_reports.router)
    app.include_router(routes_doctemplates.router)
    app.include_router(routes_knowledge.router)
    app.include_router(routes_forms.router)
    app.include_router(api_router)
    app.include_router(knowledge_api_router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if VIEWER_FRAME_PATH.match(request.url.path):
            # 문서 뷰어 창의 iframe 안에서 열리는 원본·강조 이미지 요청. 기본 오류 화면은 프레임 표시가 막혀
            # Chrome이 깨진 문서 아이콘만 보여 주므로, 같은 출처 프레임에 표시되는 짧은 안내를 돌려준다.
            message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else (
                "문서를 찾을 수 없습니다. 심사 결과 화면을 새로고침한 뒤 다시 열어 주세요.")
            body = (f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{exc.status_code}</title>'
                    f'<body><p>{escape(message)}</p></body></html>')
            return HTMLResponse(body, status_code=exc.status_code, headers={
                "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'"})
        db = SessionLocal()
        try:
            user = current_user_optional(request, db)
            return render(request, "error.html", db, user, code=exc.status_code, detail=exc.detail,
                          status_code=exc.status_code)
        finally:
            db.close()

    return app


app = create_app()
