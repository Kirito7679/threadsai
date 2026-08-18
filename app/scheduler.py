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
from app.models import JobRun

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone=settings.timezone)


def run_job(name: str, fn) -> dict:
    """Выполняет задачу по всем активным аккаунтам и пишет результат в журнал."""
    with session_scope() as db:
        record = JobRun(name=name, started_at=datetime.now(timezone.utc), status="running")
        db.add(record)
        db.flush()
        job_id = record.id

    results: dict = {}
    status = "ok"
    try:
        with session_scope() as db:
            accounts = get_all_accounts(db)
            if not accounts:
                results = {"note": "Нет подключённых аккаунтов"}
            for account in accounts:
                try:
                    results[account.username or str(account.id)] = fn(db, account)
                except Exception as exc:  # noqa: BLE001 - задача не должна ронять планировщик
                    log.exception("Задача %s упала на аккаунте %s", name, account.id)
                    results[account.username or str(account.id)] = {"error": str(exc)}
                    status = "error"
    except Exception as exc:  # noqa: BLE001
        log.exception("Задача %s упала", name)
        results = {"error": str(exc)}
        status = "error"

    with session_scope() as db:
        record = db.get(JobRun, job_id)
        if record is not None:
            record.finished_at = datetime.now(timezone.utc)
            record.status = status
            record.detail = json.dumps(results, ensure_ascii=False, default=str)[:4000]

    log.info("Задача %s завершена: %s", name, status)
    return results


# ------------------------------------------------------------------ Задачи


def job_publish_due() -> None:
    run_job("publish_due", publisher.publish_due)


def job_sync_and_metrics() -> None:
    def work(db, account):
        synced = analytics.sync_own_posts(db, account)
        metrics = analytics.collect_post_metrics(db, account)
        return {"sync": synced, "metrics": metrics}

    run_job("sync_and_metrics", work)


def job_account_metrics() -> None:
    run_job("account_metrics", analytics.collect_account_metrics)


def job_research() -> None:
    run_job("research", research.run_full_research)


def job_generate() -> None:
    def work(db, account):
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

    run_job("generate_drafts", work)


def job_refresh_tokens() -> None:
    run_job("refresh_tokens", refresh_token_if_needed)


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
