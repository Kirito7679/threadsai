"""Публичные страницы и вебхуки, которые требует кабинет Meta.

Политика конфиденциальности, страница статуса удаления данных и два колбэка:
отзыв доступа (uninstall) и запрос на удаление данных (delete).
Все эндпоинты доступны без авторизации — Meta ходит сюда без сессии,
поэтому подлинность запроса подтверждается подписью signed_request.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import purge_account
from app.config import settings
from app.db import get_db
from app.models import Account, DeletionRequest
from app.security import parse_signed_request
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["legal"])


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request, "privacy.html", {"account": None, "app_settings": settings}
    )


@router.get("/data-deletion", response_class=HTMLResponse)
def data_deletion(request: Request, code: str = "", db: Session = Depends(get_db)):
    """Страница статуса удаления: по коду видно, что заявка отработала."""
    entry = None
    if code:
        entry = db.scalars(select(DeletionRequest).where(DeletionRequest.code == code)).first()
    return templates.TemplateResponse(
        request,
        "data_deletion.html",
        {"account": None, "app_settings": settings, "code": code, "entry": entry},
    )


def _delete_for_user(db: Session, threads_user_id: str) -> tuple[str, str]:
    """Удаляет все данные пользователя. Возвращает статус и пояснение."""
    accounts = list(
        db.scalars(select(Account).where(Account.threads_user_id == str(threads_user_id)))
    )
    if not accounts:
        return "not_found", "Данных по этому пользователю нет"

    removed = [purge_account(db, account) for account in accounts]
    total = sum(sum(item["removed"].values()) for item in removed)
    return "done", f"Удалено аккаунтов: {len(removed)}, записей: {total}"


@router.post("/auth/threads/uninstall")
async def uninstall_callback(signed_request: str = Form(default=""), db: Session = Depends(get_db)):
    """Meta зовёт сюда, когда пользователь отзывает доступ приложению.

    Доступа больше нет, значит токен мёртв: отключаем аккаунт, чтобы фоновые
    задачи не бились в него каждые две минуты. Данные при этом сохраняем —
    отзыв доступа не то же самое, что требование удалить данные.
    """
    data = parse_signed_request(signed_request)
    if data is None:
        return JSONResponse({"success": False, "error": "invalid signed_request"}, status_code=400)

    threads_user_id = str(data.get("user_id", ""))
    accounts = list(
        db.scalars(select(Account).where(Account.threads_user_id == threads_user_id))
    )
    for account in accounts:
        account.is_active = False
        account.status = "needs_reauth"
        account.last_error = "Доступ отозван пользователем в Threads"
        account.last_error_at = datetime.now(timezone.utc)
    db.commit()

    log.info("Отзыв доступа для %s, отключено аккаунтов: %s", threads_user_id, len(accounts))
    return JSONResponse({"success": True})


@router.get("/auth/threads/uninstall")
def uninstall_callback_get():
    """Meta проверяет доступность адреса обычным GET при сохранении настроек."""
    return JSONResponse({"success": True})


@router.post("/auth/threads/delete")
async def delete_callback(signed_request: str = Form(default=""), db: Session = Depends(get_db)):
    """Запрос на удаление данных.

    Meta ожидает JSON с адресом страницы статуса и кодом подтверждения.
    Раньше код просто возвращался, а данные оставались на месте — теперь
    удаление выполняется, а заявка сохраняется, чтобы пользователь мог
    проверить результат по коду.
    """
    data = parse_signed_request(signed_request)
    if data is None:
        return JSONResponse({"success": False, "error": "invalid signed_request"}, status_code=400)

    threads_user_id = str(data.get("user_id", ""))
    code = secrets.token_hex(8)

    entry = DeletionRequest(code=code, threads_user_id=threads_user_id, status="pending")
    db.add(entry)
    db.flush()

    try:
        status, detail = _delete_for_user(db, threads_user_id)
    except Exception as exc:  # noqa: BLE001 - Meta должна получить ответ в любом случае
        log.exception("Удаление данных для %s не удалось", threads_user_id)
        status, detail = "pending", f"Ошибка удаления, требуется ручная проверка: {exc}"

    entry.status = status
    entry.detail = detail
    entry.completed_at = datetime.now(timezone.utc) if status != "pending" else None
    db.commit()

    log.info("Запрос на удаление данных %s: %s — %s", code, status, detail)
    return JSONResponse(
        {
            "url": f"{settings.app_base_url}/data-deletion?code={code}",
            "confirmation_code": code,
        }
    )


@router.get("/auth/threads/delete")
def delete_callback_get():
    return JSONResponse({"success": True})
