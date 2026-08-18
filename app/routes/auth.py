"""Вход в панель и OAuth-подключение аккаунта Threads."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.accounts import get_account_by_threads_id, get_active_account, upsert_account
from app.config import settings
from app.db import get_db
from app.security import check_password, make_session_cookie, new_state_token, read_session_cookie
from app.templating import templates
from app.threads_api import ThreadsAPIError, ThreadsClient

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
COOKIE_SECURE = settings.app_base_url.startswith("https")


def _set_session(response, threads_user_id: str = ""):
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie({"auth": True, "uid": threads_user_id}),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=60 * 60 * 24 * 14,
    )
    return response


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = "", db: Session = Depends(get_db)):
    """Единственный вход — через Threads. Пароль остаётся запасным вариантом."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error or request.query_params.get("error", ""),
            "account": None,
            "has_accounts": get_active_account(db) is not None,
            "app_settings": settings,
            "app_configured": bool(settings.threads_app_id and settings.threads_app_secret),
        },
    )


@router.post("/login")
def login(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    """Запасной вход по паролю — на случай, если OAuth недоступен."""
    if not check_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Неверный пароль",
                "account": None,
                "has_accounts": get_active_account(db) is not None,
                "app_settings": settings,
                "app_configured": bool(settings.threads_app_id and settings.threads_app_secret),
            },
            status_code=401,
        )
    return _set_session(RedirectResponse("/", status_code=303))


@router.get("/logout")
def logout():
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/threads")
def connect_threads(request: Request):
    """Старт OAuth: уводим пользователя на страницу разрешений Threads."""
    if not settings.threads_app_id or not settings.threads_app_secret:
        return RedirectResponse("/auth/login?error=Не+заданы+THREADS_APP_ID+и+THREADS_APP_SECRET", 303)

    state = new_state_token()
    response = RedirectResponse(ThreadsClient.authorization_url(state), status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        make_session_cookie({"state": state}),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=900,
    )
    return response


@router.get("/callback")
def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(f"/auth/login?error={error_description or error}", status_code=303)
    if not code:
        return RedirectResponse("/auth/login?error=Threads+не+вернул+код+авторизации", status_code=303)

    saved = read_session_cookie(request.cookies.get(STATE_COOKIE))
    if not saved or saved.get("state") != state:
        return RedirectResponse("/auth/login?error=Проверка+state+не+пройдена", status_code=303)

    try:
        short = ThreadsClient.exchange_code(code)
        threads_user_id = str(short.get("user_id", ""))

        # Каждый аккаунт получает собственный кабинет: свои ключевые слова,
        # черновики, посты и настройки. Авторизоваться могут только те,
        # кого вы добавили в Threads Testers приложения.
        long_lived = ThreadsClient.exchange_long_lived(short["access_token"])
        account = upsert_account(
            db,
            threads_user_id=threads_user_id,
            token=long_lived["access_token"],
            expires_in=long_lived.get("expires_in"),
        )
        db.commit()
        log.info("Вход выполнен: @%s", account.username)
    except (ThreadsAPIError, KeyError) as exc:
        db.rollback()
        log.error("OAuth не завершён: %s", exc)
        return RedirectResponse(f"/auth/login?error={exc}", status_code=303)

    # Успешная авторизация в Threads — это и есть вход в панель
    response = _set_session(
        RedirectResponse("/?ok=Аккаунт+подключён", status_code=303), account.threads_user_id
    )
    response.delete_cookie(STATE_COOKIE)
    return response


@router.post("/disconnect")
def disconnect(request: Request, db: Session = Depends(get_db)):
    session = read_session_cookie(request.cookies.get(SESSION_COOKIE)) or {}
    account = get_account_by_threads_id(db, session.get("uid", "")) or get_active_account(db)
    if account is not None:
        account.is_active = False
        db.commit()
    response = RedirectResponse("/auth/login?ok=Аккаунт+отключён", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
