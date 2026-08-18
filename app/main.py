"""Точка входа: FastAPI-приложение Threads Autopilot."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.routes import auth as auth_routes
from app.routes import ui as ui_routes
from app.scheduler import start_scheduler, stop_scheduler
from app.security import read_session_cookie

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("threads-autopilot")

PUBLIC_PREFIXES = ("/auth/", "/static/", "/healthz", "/favicon.ico")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("База готова: %s", settings.database_url.split("@")[-1])
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Threads Autopilot", lifespan=lifespan, docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC_PREFIXES):
        session = read_session_cookie(request.cookies.get("session"))
        if not session or not session.get("auth"):
            return RedirectResponse("/auth/login", status_code=303)
    return await call_next(request)


app.include_router(auth_routes.router)
app.include_router(ui_routes.router)
