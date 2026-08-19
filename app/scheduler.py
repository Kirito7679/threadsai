"""Фоновые задачи: разведка, генерация, публикация, сбор метрик."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import analytics, generator, publisher, research
from app.accounts import get_all_accounts, get_setting, refresh_token_if_needed
from app.config import settings
from app.db import session_scope
from app.models import Account, JobRun

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=settings.timezone)


def _start_job(name: str, account_id: int | None) -> int:
    with session_scope() as db:
        record = JobRun(
            name=name,
            account_id=account_id,
            started_at=datetime.now(timezone.utc),
            status="running",
        )
        db.add(record)
        db.flush()
        return record.id


def _finish_job(job_id: int, status: str, detail: dict) -> None:
    with session_scope() as db:
        record = db.get(JobRun, job_id)
        if record is not None:
            record.finished_at = datetime.now(timezone.utc)
            record.status = status
            record.detail = json.dumps(detail, ensure_ascii=False, default=str)[:4000]


def run_job(name: str, fn, only_healthy: bool = True) -> dict:
    """Выполняет задачу по каждому аккаунту отдельно.

    Своя сессия на аккаунт: после неудачного flush сессия SQLAlchemy требует
    rollback, и в общей сессии один сбойный аккаунт ронял бы все следующие.
    Своя строка журнала на аккаунт: кабинет не должен видеть в detail
    юзернеймы и ошибки соседей.
    """
    try:
        with session_scope() as db:
            accounts = [
                (account.id, account.username)
                for account in get_all_accounts(db, only_healthy=only_healthy)
            ]
    except Exception as exc:  # noqa: BLE001 - база недоступна
        log.exception("Задача %s не смогла получить список аккаунтов", name)
        _finish_job(_start_job(name, None), "error", {"error": str(exc)})
        return {"error": str(exc)}

    if not accounts:
        job_id = _start_job(name, None)
        detail = {"note": "Нет аккаунтов, готовых к работе"}
        _finish_job(job_id, "ok", detail)
        return detail

    results: dict = {}
    for account_id, username in accounts:
        label = username or str(account_id)
        job_id = _start_job(name, account_id)
        try:
            with session_scope() as db:
                account = db.get(Account, account_id)
                if account is None:
                    # Аккаунт отключили, пока задача шла по списку
                    _finish_job(job_id, "ok", {"note": "Аккаунт удалён во время выполнения"})
                    continue
                result = fn(db, account)
            if not isinstance(result, dict):
                result = {"result": result}
            status = "error" if result.get("error") else "ok"
        except Exception as exc:  # noqa: BLE001 - задача не должна ронять планировщик
            log.exception("Задача %s упала на аккаунте %s", name, account_id)
            result = {"error": str(exc)}
            status = "error"

        _finish_job(job_id, status, result)
        results[label] = result

    log.info("Задача %s завершена по %s аккаунтам", name, len(results))
    return results


def run_job_for_account(name: str, account_id: int) -> dict:
    """Один запуск по одному аккаунту — для кнопок в панели.

    Кнопка в кабинете должна трогать только этот кабинет: иначе нажатие
    «Разведка» тратит суточную квоту поиска всех подключённых аккаунтов
    и пишет им в журнал запуск, которого они не заказывали.
    """
    work = JOB_WORK.get(name)
    if work is None:
        return {"error": f"Неизвестная задача: {name}"}

    job_id = _start_job(name, account_id)
    try:
        with session_scope() as db:
            account = db.get(Account, account_id)
            if account is None:
                _finish_job(job_id, "error", {"error": "Аккаунт не найден"})
                return {"error": "Аккаунт не найден"}
            result = work(db, account)
        if not isinstance(result, dict):
            result = {"result": result}
        status = "error" if result.get("error") else "ok"
    except Exception as exc:  # noqa: BLE001 - ручной запуск не должен ронять сервер
        log.exception("Ручной запуск %s упал на аккаунте %s", name, account_id)
        result = {"error": str(exc)}
        status = "error"

    _finish_job(job_id, status, result)
    return result


# ------------------------------------------------------------------ Задачи


def work_sync_and_metrics(db, account) -> dict:
    synced = analytics.sync_own_posts(db, account)
    metrics = analytics.collect_post_metrics(db, account)
    return {"sync": synced, "metrics": metrics}


def work_generate(db, account) -> dict:
    result = generator.generate_drafts(db, account)

    # Автопилот: одобряем и ставим в расписание без ручной модерации
    if get_setting(db, account.id, "autopilot", "false").lower() == "true":
        from sqlalchemy import select

        from app.models import Draft

        pending = list(
            db.scalars(
                select(Draft)
                .where(Draft.account_id == account.id, Draft.status == "pending")
                .order_by(Draft.created_at)
            )
        )
        slots = generator.propose_slots(db, account, len(pending))
        for draft, slot in zip(pending, slots):
            draft.status = "approved"
            draft.scheduled_at = slot
        result["auto_approved"] = min(len(pending), len(slots))

    return result


# Что именно делает каждая задача с одним аккаунтом. Планировщик гоняет это
# по всем аккаунтам, кнопки в панели — только по своему.
JOB_WORK = {
    "publish_due": publisher.publish_due,
    "sync_and_metrics": work_sync_and_metrics,
    "account_metrics": analytics.collect_account_metrics,
    "research": research.run_full_research,
    "generate_drafts": work_generate,
    "refresh_tokens": refresh_token_if_needed,
}


def job_publish_due() -> None:
    run_job("publish_due", publisher.publish_due)


def job_sync_and_metrics() -> None:
    run_job("sync_and_metrics", work_sync_and_metrics)


def job_account_metrics() -> None:
    run_job("account_metrics", analytics.collect_account_metrics)


def job_research() -> None:
    run_job("research", research.run_full_research)


def job_generate() -> None:
    run_job("generate_drafts", work_generate)


def job_refresh_tokens() -> None:
    # Единственная задача, которая ходит и в отвалившиеся аккаунты: удачное
    # продление снимает пометку needs_reauth. Так аккаунт, помеченный из-за
    # череды сетевых сбоев, чинится сам, а отозванный доступ остаётся помеченным.
    run_job("refresh_tokens", refresh_token_if_needed, only_healthy=False)


# ------------------------------------------------------------------ Регистрация


def start_scheduler() -> None:
    if not settings.enable_scheduler:
        log.info("Планировщик отключён (ENABLE_SCHEDULER=false)")
        return
    if scheduler.running:
        return

    scheduler.add_job(
        job_publish_due,
        IntervalTrigger(minutes=2),
        id="publish_due",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_sync_and_metrics,
        IntervalTrigger(minutes=settings.metrics_interval_minutes),
        id="sync_and_metrics",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_research,
        IntervalTrigger(hours=settings.research_interval_hours),
        id="research",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_generate,
        CronTrigger(hour=settings.generation_hour, minute=0),
        id="generate_drafts",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_account_metrics,
        CronTrigger(hour=23, minute=50),
        id="account_metrics",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_refresh_tokens,
        CronTrigger(hour=3, minute=30),
        id="refresh_tokens",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info("Планировщик запущен, часовой пояс %s", settings.timezone)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


JOBS_BY_NAME = {
    "publish_due": job_publish_due,
    "sync_and_metrics": job_sync_and_metrics,
    "account_metrics": job_account_metrics,
    "research": job_research,
    "generate_drafts": job_generate,
    "refresh_tokens": job_refresh_tokens,
}
