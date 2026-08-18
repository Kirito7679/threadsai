"""Публичные страницы и вебхуки, которые требует кабинет Meta.

Политика конфиденциальности, инструкция по удалению данных и два колбэка:
отзыв доступа (uninstall) и запрос на удаление данных (delete).
Все эндпоинты доступны без авторизации — Meta ходит сюда без сессии.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["legal"])


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request, "privacy.html", {"account": None, "app_settings": settings}
    )


@router.get("/data-deletion", response_class=HTMLResponse)
def data_deletion(request: Request, code: str = ""):
    return templates.TemplateResponse(
        request, "data_deletion.html", {"account": None, "app_settings": settings, "code": code}
    )


@router.post("/auth/threads/uninstall")
async def uninstall_callback(signed_request: str = Form(default="")):
    """Meta зовёт сюда, когда пользователь отзывает доступ приложению."""
    log.info("Получен колбэк отзыва доступа Threads")
    return JSONResponse({"success": True})


@router.get("/auth/threads/uninstall")
def uninstall_callback_get():
    """Meta проверяет доступность адреса обычным GET при сохранении настроек."""
    return JSONResponse({"success": True})


@router.post("/auth/threads/delete")
async def delete_callback(signed_request: str = Form(default="")):
    """Запрос на удаление данных.

    Meta ожидает JSON с адресом страницы статуса и кодом подтверждения,
    по которому пользователь может проверить ход удаления.
    """
    confirmation_code = hashlib.sha256(
        (signed_request or "threads-delete-request").encode("utf-8")
    ).hexdigest()[:16]
    log.info("Получен запрос на удаление данных, код %s", confirmation_code)
    return JSONResponse(
        {
            "url": f"{settings.app_base_url}/data-deletion?code={confirmation_code}",
            "confirmation_code": confirmation_code,
        }
    )


@router.get("/auth/threads/delete")
def delete_callback_get():
    return JSONResponse({"success": True})
