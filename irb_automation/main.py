"""FastAPI 애플리케이션."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api.knowledge import router as knowledge_api_router
from .api.routes import router as api_router
from .config import get_settings
from .database import SessionLocal, init_db
from .web import (
    routes_admin,
    routes_deid,
    routes_doctemplates,
    routes_forms,
    routes_governance,
    routes_knowledge,
    routes_main,
    routes_meetings,
    routes_protocols,
    routes_reports,
    routes_submission,
)
from .web.deps import LoginRequired, current_user_optional, render

log = logging.getLogger("irb")


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
    app = FastAPI(
        title="IRB 자동화 솔루션",
        description="병원 의료데이터 활용 연구를 위한 IRB 심의 자동화 API",
        version=__version__,
        lifespan=lifespan,
    )
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
    app.include_router(routes_submission.router)  # /documents/review-queue 가 /documents/{id} 보다 먼저 매칭되도록 선등록
    app.include_router(routes_protocols.router)
    app.include_router(routes_deid.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_meetings.router)
    app.include_router(routes_governance.router)
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
        db = SessionLocal()
        try:
            user = current_user_optional(request, db)
            return render(request, "error.html", db, user, code=exc.status_code, detail=exc.detail,
                          status_code=exc.status_code)
        finally:
            db.close()

    return app


app = create_app()
