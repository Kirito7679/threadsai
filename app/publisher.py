"""Публикация одобренных черновиков в Threads."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import client_for, get_setting, mark_api_error, mark_healthy
from app.models import Account, Draft, Post
from app.threads_api import DAILY_PUBLISH_LIMIT, ThreadsAPIError, ThreadsClient

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
CHAIN_DELAY_SEC = 5


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def record_post(
    db: Session,
    account: Account,
    media_id: str,
    draft: Draft,
    index: int,
    root_media_id: str,
    text: str,
) -> Post:
    """Заводит строку опубликованного поста или дополняет уже существующую.

    Часовая синхронизация могла увидеть этот пост в ленте раньше, чем мы дошли
    до записи: тогда строка уже есть, и вставка второй нарушила бы уникальность
    (account_id, media_id) — публикация упала бы уже после того, как пост
    оказался в ленте. Поэтому дописываем то, чего синхронизация знать не может:
    привязку к черновику, рубрику и место в цепочке.
    """
    post = db.scalars(
        select(Post).where(Post.account_id == account.id, Post.media_id == media_id)
    ).first()
    if post is None:
        post = Post(account_id=account.id, media_id=media_id)
        db.add(post)

    post.draft_id = draft.id
    post.root_media_id = root_media_id
    post.chain_index = index
    post.text = text
    post.topic = draft.topic
    post.is_reply = index > 0
    if post.published_at is None:
        post.published_at = datetime.now(timezone.utc)
    return post


def remaining_quota(client: ThreadsClient) -> int | None:
    """Сколько постов ещё можно опубликовать за сутки. None — узнать не удалось."""
    try:
        payload = client.get_publishing_limit()
    except ThreadsAPIError as exc:
        log.warning("Квоту публикаций узнать не удалось: %s", exc)
        return None
    data = payload.get("data") or []
    if not data:
        return None
    entry = data[0]
    used = int(entry.get("quota_usage", 0) or 0)
    total = int((entry.get("config") or {}).get("quota_total", DAILY_PUBLISH_LIMIT) or DAILY_PUBLISH_LIMIT)
    return max(0, total - used)


def publish_draft(db: Session, account: Account, draft: Draft) -> dict:
    """Публикует ветку целиком: первый пост, затем ответы в цепочку."""
    parts: list[str] = json.loads(draft.parts_json or "[]")
    if not parts:
        draft.status = "failed"
        draft.error = "Пустой черновик"
        return {"ok": False, "error": draft.error}

    client = client_for(account)

    quota = remaining_quota(client)
    if quota is not None and quota < len(parts):
        draft.error = f"Суточная квота исчерпана: осталось {quota}, нужно {len(parts)}"
        log.warning("%s — публикация отложена", draft.error)
        return {"ok": False, "error": draft.error, "retry": True}

    reply_control = get_setting(db, account.id, "reply_control", "everyone")
    draft.status = "publishing"
    draft.attempts += 1
    db.flush()

    published_ids: list[str] = []
    previous_id: str | None = None

    try:
        for index, text in enumerate(parts):
            media_id = client.publish_text_post(
                text,
                reply_to_id=previous_id,
                reply_control=reply_control if index == 0 else None,
            )
            published_ids.append(media_id)
            previous_id = media_id

            record_post(db, account, media_id, draft, index, published_ids[0], text)
            db.flush()

            if index < len(parts) - 1:
                time.sleep(CHAIN_DELAY_SEC)

    except (ThreadsAPIError, ValueError) as exc:
        message = str(exc)
        log.error("Публикация черновика %s не удалась: %s", draft.id, message)
        if isinstance(exc, ThreadsAPIError):
            mark_api_error(account, exc)
        if published_ids:
            # Часть ветки уже в ленте — дальше вручную, автоповтор всё продублирует
            draft.status = "failed"
            draft.error = (
                f"Опубликовано частично ({len(published_ids)} из {len(parts)}). "
                f"Ошибка: {message}"
            )
            draft.published_at = datetime.now(timezone.utc)
            return {"ok": False, "partial": True, "error": draft.error}

        if draft.attempts >= MAX_ATTEMPTS:
            draft.status = "failed"
            draft.error = message
            return {"ok": False, "error": message}

        draft.status = "approved"  # вернём в очередь на повтор
        draft.error = message
        return {"ok": False, "retry": True, "error": message}

    # Подтягиваем permalink опубликованных постов
    for media_id in published_ids:
        try:
            info = client.get_post(media_id)
        except ThreadsAPIError:
            continue
        post = db.scalars(
            select(Post).where(Post.account_id == account.id, Post.media_id == media_id)
        ).first()
        if post is not None:
            post.permalink = info.get("permalink", "")

    draft.status = "published"
    draft.published_at = datetime.now(timezone.utc)
    draft.error = ""
    mark_healthy(account)
    db.flush()

    log.info("Черновик %s опубликован: %s", draft.id, published_ids[0])
    return {"ok": True, "media_ids": published_ids}


def publish_due(db: Session, account: Account) -> dict:
    """Публикует все одобренные черновики, у которых подошло время."""
    now = datetime.now(timezone.utc)
    candidates = list(
        db.scalars(
            select(Draft)
            .where(
                Draft.account_id == account.id,
                Draft.status == "approved",
                Draft.scheduled_at.is_not(None),
            )
            .order_by(Draft.scheduled_at)
            .limit(10)
        )
    )

    due = [d for d in candidates if (_aware(d.scheduled_at) or now) <= now]
    if not due:
        return {"published": 0, "pending": len(candidates)}

    published = 0
    failed = 0
    for draft in due:
        result = publish_draft(db, account, draft)
        db.commit()
        if result.get("ok"):
            published += 1
        else:
            failed += 1

    return {"published": published, "failed": failed}
