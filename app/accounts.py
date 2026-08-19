"""Работа с аккаунтом: токены, настройки, фабрика API-клиента."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AccountSetting, Keyword
from app.security import decrypt, encrypt
from app.threads_api import ThreadsAPIError, ThreadsClient

log = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, str] = {
    "brand_voice": (
        "Пишу от первого лица, простым разговорным языком. Без канцелярита, "
        "без пафоса и без эмодзи через слово. Короткие предложения. "
        "Конкретика и личный опыт вместо общих советов."
    ),
    "niche": "",
    "audience": "",
    "goals": "Рост охватов и числа подписчиков, вовлечённая аудитория в комментариях",
    "forbidden": "Кликбейт без содержания, обещания лёгких денег, оскорбления, политика",
    "language": "русский",
    "rubrics": "личный опыт, разбор ошибки, полезный список, наблюдение, вопрос аудитории",
    "posts_per_day": "3",
    "posting_hours": "9,13,19",
    "reply_control": "everyone",
    "min_hours_between_posts": "2",
    "autopilot": "false",
    # Часовой пояс и час генерации — свои у каждого аккаунта: расписание
    # «в 8:00» бессмысленно, если владельцы живут в разных поясах.
    "timezone": settings.timezone,
    "generation_hour": str(settings.generation_hour),
}


def account_tz(db: Session, account_id: int) -> ZoneInfo:
    """Часовой пояс аккаунта. При неизвестном имени — пояс сервиса."""
    name = get_setting(db, account_id, "timezone", settings.timezone)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Неизвестный часовой пояс %r у аккаунта %s", name, account_id)
        return ZoneInfo(settings.timezone)


def get_active_account(db: Session) -> Account | None:
    return db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.id)).first()


def get_account_by_threads_id(db: Session, threads_user_id: str) -> Account | None:
    if not threads_user_id:
        return None
    return db.scalars(
        select(Account).where(
            Account.threads_user_id == str(threads_user_id), Account.is_active.is_(True)
        )
    ).first()


def get_all_accounts(db: Session, only_healthy: bool = False) -> list[Account]:
    """Активные аккаунты. only_healthy отсекает те, что ждут переподключения."""
    stmt = select(Account).where(Account.is_active.is_(True))
    if only_healthy:
        stmt = stmt.where(Account.status != NEEDS_REAUTH)
    return list(db.scalars(stmt.order_by(Account.id)))


# ------------------------------------------------------------------ Здоровье

STATUS_OK = "ok"
NEEDS_REAUTH = "needs_reauth"
# Столько подряд идущих сбоев подряд считаем поводом остановить фоновые задачи
MAX_CONSECUTIVE_ERRORS = 5


def mark_api_error(account: Account, exc: Exception) -> None:
    """Фиксирует сбой обращения к Threads.

    Ошибка авторизации означает отозванный или протухший токен — тут повторять
    бессмысленно, аккаунт сразу уходит в needs_reauth. Прочие сбои (сеть,
    троттлинг) копятся: пять подряд — тоже повод остановиться и сказать
    человеку, иначе publish_due будет падать каждые 2 минуты вечно.
    """
    account.consecutive_errors = (account.consecutive_errors or 0) + 1
    account.last_error = str(exc)[:1000]
    account.last_error_at = datetime.now(timezone.utc)

    is_auth = isinstance(exc, ThreadsAPIError) and exc.is_auth_error
    if is_auth or account.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        if account.status != NEEDS_REAUTH:
            log.warning(
                "Аккаунт @%s требует переподключения: %s", account.username, account.last_error
            )
        account.status = NEEDS_REAUTH


def mark_healthy(account: Account) -> None:
    """Успешный вызов обнуляет счётчик сбоев."""
    if account.consecutive_errors or account.status != STATUS_OK or account.last_error:
        account.consecutive_errors = 0
        account.status = STATUS_OK
        account.last_error = ""
        account.last_error_at = None


def get_setting(db: Session, account_id: int, key: str, default: str | None = None) -> str:
    row = db.scalars(
        select(AccountSetting).where(
            AccountSetting.account_id == account_id, AccountSetting.key == key
        )
    ).first()
    if row is not None:
        return row.value
    return default if default is not None else DEFAULT_SETTINGS.get(key, "")


def get_settings_dict(db: Session, account_id: int) -> dict[str, str]:
    values = dict(DEFAULT_SETTINGS)
    rows = db.scalars(select(AccountSetting).where(AccountSetting.account_id == account_id))
    for row in rows:
        values[row.key] = row.value
    return values


def set_setting(db: Session, account_id: int, key: str, value: str) -> None:
    row = db.scalars(
        select(AccountSetting).where(
            AccountSetting.account_id == account_id, AccountSetting.key == key
        )
    ).first()
    if row is None:
        db.add(AccountSetting(account_id=account_id, key=key, value=value))
    else:
        row.value = value


def get_keywords(db: Session, account_id: int, only_active: bool = True) -> list[Keyword]:
    stmt = select(Keyword).where(Keyword.account_id == account_id)
    if only_active:
        stmt = stmt.where(Keyword.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Keyword.term)))


def client_for(account: Account) -> ThreadsClient:
    return ThreadsClient(decrypt(account.access_token_enc), account.threads_user_id)


def save_token(
    account: Account,
    token: str,
    expires_in: int | None = None,
) -> None:
    account.access_token_enc = encrypt(token)
    account.token_refreshed_at = datetime.now(timezone.utc)
    if expires_in:
        account.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))


def upsert_account(db: Session, threads_user_id: str, token: str, expires_in: int | None) -> Account:
    account = db.scalars(
        select(Account).where(Account.threads_user_id == str(threads_user_id))
    ).first()
    if account is None:
        account = Account(threads_user_id=str(threads_user_id), access_token_enc="")
        db.add(account)
    save_token(account, token, expires_in)
    account.is_active = True
    # Свежий токен снимает пометку needs_reauth — это и есть переподключение
    mark_healthy(account)
    db.flush()

    # Подтягиваем профиль
    try:
        profile = client_for(account).get_profile()
        account.username = profile.get("username", "") or account.username
        account.profile_picture_url = (
            profile.get("threads_profile_picture_url", "") or account.profile_picture_url
        )
    except ThreadsAPIError as exc:
        log.warning("Не удалось получить профиль: %s", exc)

    # Первичные настройки
    for key, value in DEFAULT_SETTINGS.items():
        if not db.scalars(
            select(AccountSetting).where(
                AccountSetting.account_id == account.id, AccountSetting.key == key
            )
        ).first():
            db.add(AccountSetting(account_id=account.id, key=key, value=value))
    return account


def refresh_token_if_needed(db: Session, account: Account, threshold_days: int = 10) -> str:
    """Продлевает долгоживущий токен, если до истечения меньше threshold_days."""
    if account.token_expires_at is None:
        return "Срок действия токена неизвестен — пропускаем"

    expires_at = account.token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    days_left = (expires_at - datetime.now(timezone.utc)).days
    if days_left > threshold_days:
        return f"@{account.username}: осталось {days_left} дн., продление не нужно"

    refreshed_at = account.token_refreshed_at
    if refreshed_at is not None:
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - refreshed_at < timedelta(hours=24):
            return f"@{account.username}: токен моложе 24 часов, продлить нельзя"

    try:
        payload = ThreadsClient.refresh_long_lived(decrypt(account.access_token_enc))
    except ThreadsAPIError as exc:
        # Не продлили — значит доступ отозван или токен уже мёртв. Молчать нельзя:
        # через несколько дней аккаунт просто перестанет публиковать.
        mark_api_error(account, exc)
        raise

    save_token(account, payload["access_token"], payload.get("expires_in"))
    mark_healthy(account)
    return f"@{account.username}: токен продлён на {int(payload.get('expires_in', 0)) // 86400} дн."


def purge_account(db: Session, account: Account) -> dict:
    """Полностью удаляет аккаунт и все его данные.

    Каскады объявлены во внешних ключах, но SQLite по умолчанию их не
    применяет, поэтому дочерние строки удаляем явно — иначе после «удаления»
    в базе останутся посты, метрики и черновики.
    """
    from app.models import (
        AccountMetric,
        AccountSetting,
        Draft,
        JobRun,
        Keyword,
        LlmUsage,
        Post,
        PostMetric,
        ResearchPost,
        TrendReport,
    )

    account_id = account.id
    username = account.username
    removed: dict[str, int] = {}

    post_ids = list(db.scalars(select(Post.id).where(Post.account_id == account_id)))
    if post_ids:
        removed["post_metrics"] = (
            db.query(PostMetric).filter(PostMetric.post_id.in_(post_ids)).delete(synchronize_session=False)
        )

    for model in (Post, Draft, ResearchPost, TrendReport, Keyword, AccountMetric, AccountSetting, JobRun, LlmUsage):
        removed[model.__tablename__] = (
            db.query(model).filter(model.account_id == account_id).delete(synchronize_session=False)
        )

    db.delete(account)
    db.flush()
    log.info("Аккаунт @%s (%s) удалён полностью: %s", username, account_id, removed)
    return {"account_id": account_id, "username": username, "removed": removed}
