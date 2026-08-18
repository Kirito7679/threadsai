"""Веб-панель: дашборд, очередь модерации, тренды, посты, настройки."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics, generator, publisher, research
from app.accounts import (
    DEFAULT_SETTINGS,
    get_active_account,
    get_keywords,
    get_settings_dict,
    set_setting,
)
from app.config import settings as app_settings
from app.db import get_db, session_scope
from app.llm import get_llm
from app.models import Account, Draft, JobRun, Keyword, Post, PostMetric, ResearchPost
from app.research import latest_report
from app.templating import templates
from app.threads_api import MAX_POST_CHARS

log = logging.getLogger(__name__)
router = APIRouter()
LOCAL_TZ = ZoneInfo(app_settings.timezone)


def _base_context(request: Request, db: Session, account: Account | None) -> dict:
    pending_count = 0
    if account is not None:
        pending_count = (
            db.query(Draft)
            .filter(Draft.account_id == account.id, Draft.status == "pending")
            .count()
        )
    return {
        "request": request,
        "account": account,
        "pending_count": pending_count,
        "app_settings": app_settings,
        "llm_ready": get_llm().is_configured,
        "flash_ok": request.query_params.get("ok", ""),
        "flash_error": request.query_params.get("error", ""),
    }


def _parse_local_datetime(raw: str) -> datetime | None:
    """Строка из <input type=datetime-local> -> aware UTC."""
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)


# ------------------------------------------------------------------ Дашборд


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)

    if account is None:
        return templates.TemplateResponse(request, "onboarding.html", context)

    context.update(
        {
            "summary": analytics.summary(db, account.id, days=30),
            "timeseries": analytics.account_timeseries(db, account.id, days=30),
            "top": analytics.top_posts(db, account.id, days=30, limit=5),
            "topics": analytics.topic_performance(db, account.id)[:6],
            "hours": analytics.best_hours(db, account.id)[:5],
            "scheduled": list(
                db.scalars(
                    select(Draft)
                    .where(Draft.account_id == account.id, Draft.status == "approved")
                    .order_by(Draft.scheduled_at)
                    .limit(6)
                )
            ),
            "jobs": list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(8))),
            "report": latest_report(db, account.id),
        }
    )
    context["engagement_of"] = analytics.engagement
    return templates.TemplateResponse(request, "dashboard.html", context)


# ------------------------------------------------------------------ Очередь


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, status: str = "pending", db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)
    if account is None:
        return RedirectResponse("/", status_code=303)

    stmt = select(Draft).where(Draft.account_id == account.id)
    if status != "all":
        stmt = stmt.where(Draft.status == status)
    order = Draft.scheduled_at if status == "approved" else Draft.created_at.desc()
    drafts = list(db.scalars(stmt.order_by(order).limit(80)))

    counts = {
        name: db.query(Draft)
        .filter(Draft.account_id == account.id, Draft.status == name)
        .count()
        for name in ("pending", "approved", "published", "rejected", "failed")
    }

    context.update(
        {
            "drafts": drafts,
            "status": status,
            "counts": counts,
            "max_chars": MAX_POST_CHARS,
            "next_slots": generator.propose_slots(db, account, 5),
        }
    )
    return templates.TemplateResponse(request, "queue.html", context)


@router.post("/queue/generate")
def queue_generate(count: int = Form(3), db: Session = Depends(get_db)):
    account = get_active_account(db)
    if account is None:
        return RedirectResponse("/", status_code=303)
    result = generator.generate_drafts(db, account, count=max(1, min(count, 10)))
    db.commit()
    if result.get("error"):
        return RedirectResponse(f"/queue?error={result['error']}", status_code=303)
    return RedirectResponse(f"/queue?ok=Готово+черновиков:+{result['created']}", status_code=303)


@router.post("/queue/{draft_id}/approve")
def approve(draft_id: int, scheduled_at: str = Form(""), db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)

    when = _parse_local_datetime(scheduled_at)
    if when is None:
        account = db.get(Account, draft.account_id)
        slots = generator.propose_slots(db, account, 1) if account else []
        when = slots[0] if slots else datetime.now(timezone.utc)

    draft.status = "approved"
    draft.scheduled_at = when
    draft.error = ""
    db.commit()
    return RedirectResponse("/queue?ok=Запланировано", status_code=303)


@router.post("/queue/approve-all")
def approve_all(db: Session = Depends(get_db)):
    account = get_active_account(db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    pending = list(
        db.scalars(
            select(Draft)
            .where(Draft.account_id == account.id, Draft.status == "pending")
            .order_by(Draft.created_at)
        )
    )
    slots = generator.propose_slots(db, account, len(pending))
    approved = 0
    for draft, slot in zip(pending, slots):
        draft.status = "approved"
        draft.scheduled_at = slot
        approved += 1
    db.commit()
    return RedirectResponse(f"/queue?ok=Одобрено:+{approved}", status_code=303)


@router.post("/queue/{draft_id}/reject")
def reject(draft_id: int, reason: str = Form(""), db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is not None:
        draft.status = "rejected"
        draft.reject_reason = reason
        db.commit()
    return RedirectResponse("/queue?ok=Отклонено", status_code=303)


@router.post("/queue/{draft_id}/edit")
def edit(draft_id: int, parts: str = Form(...), topic: str = Form(""), db: Session = Depends(get_db)):
    """Части ветки разделяются пустой строкой."""
    draft = db.get(Draft, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)

    chunks = [chunk.strip() for chunk in parts.split("\n\n") if chunk.strip()]
    too_long = [i + 1 for i, chunk in enumerate(chunks) if len(chunk) > MAX_POST_CHARS]
    if too_long:
        return RedirectResponse(
            f"/queue?error=Часть+{','.join(map(str, too_long))}+длиннее+{MAX_POST_CHARS}+символов",
            status_code=303,
        )
    if not chunks:
        return RedirectResponse("/queue?error=Пустой+текст", status_code=303)

    draft.parts_json = json.dumps(chunks, ensure_ascii=False)
    if topic:
        draft.topic = topic[:120]
    draft.edited_by_human = True
    db.commit()
    return RedirectResponse("/queue?ok=Сохранено", status_code=303)


@router.post("/queue/{draft_id}/regenerate")
def regenerate(draft_id: int, instruction: str = Form(""), db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)
    result = generator.regenerate_draft(db, draft, instruction)
    db.commit()
    if not result.get("ok"):
        return RedirectResponse(f"/queue?error={result.get('error')}", status_code=303)
    return RedirectResponse("/queue?ok=Переписано", status_code=303)


@router.post("/queue/{draft_id}/publish-now")
def publish_now(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)
    account = db.get(Account, draft.account_id)
    if account is None:
        return RedirectResponse("/queue?error=Аккаунт+не+найден", status_code=303)

    result = publisher.publish_draft(db, account, draft)
    db.commit()
    if not result.get("ok"):
        return RedirectResponse(f"/queue?error={result.get('error')}", status_code=303)
    return RedirectResponse("/queue?ok=Опубликовано", status_code=303)


@router.post("/queue/{draft_id}/delete")
def delete_draft(draft_id: int, db: Session = Depends(get_db)):
    draft = db.get(Draft, draft_id)
    if draft is not None:
        db.delete(draft)
        db.commit()
    return RedirectResponse("/queue?ok=Удалено", status_code=303)


# ------------------------------------------------------------------ Тренды


@router.get("/research", response_class=HTMLResponse)
def research_page(request: Request, db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)
    if account is None:
        return RedirectResponse("/", status_code=303)

    report = latest_report(db, account.id)
    payload = {}
    if report is not None:
        try:
            payload = json.loads(report.payload_json)
        except json.JSONDecodeError:
            payload = {}

    popular = list(
        db.scalars(
            select(ResearchPost)
            .where(ResearchPost.account_id == account.id, ResearchPost.search_type == "TOP")
            .order_by(ResearchPost.collected_at.desc())
            .limit(40)
        )
    )

    context.update(
        {
            "keywords": get_keywords(db, account.id, only_active=False),
            "search_denied": get_settings_dict(db, account.id).get("keyword_search_denied") == "true",
            "report": report,
            "payload": payload,
            "popular": popular,
            "total_research": db.query(ResearchPost)
            .filter(ResearchPost.account_id == account.id)
            .count(),
        }
    )
    return templates.TemplateResponse(request, "research.html", context)


@router.post("/research/run")
def research_run(background: BackgroundTasks, db: Session = Depends(get_db)):
    account = get_active_account(db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    def work() -> None:
        from app.scheduler import run_job

        run_job("research", research.run_full_research)

    background.add_task(work)
    return RedirectResponse("/research?ok=Разведка+запущена+в+фоне", status_code=303)


@router.post("/keywords/add")
def add_keyword(term: str = Form(...), search_mode: str = Form("KEYWORD"), db: Session = Depends(get_db)):
    account = get_active_account(db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    term = term.strip()
    if term:
        exists = db.scalars(
            select(Keyword).where(Keyword.account_id == account.id, Keyword.term == term)
        ).first()
        if exists is None:
            db.add(
                Keyword(
                    account_id=account.id,
                    term=term,
                    search_mode="TAG" if search_mode == "TAG" else "KEYWORD",
                )
            )
            db.commit()
    return RedirectResponse("/research?ok=Ключевое+слово+добавлено", status_code=303)


@router.post("/keywords/{keyword_id}/toggle")
def toggle_keyword(keyword_id: int, db: Session = Depends(get_db)):
    keyword = db.get(Keyword, keyword_id)
    if keyword is not None:
        keyword.is_active = not keyword.is_active
        db.commit()
    return RedirectResponse("/research", status_code=303)


@router.post("/keywords/{keyword_id}/delete")
def delete_keyword(keyword_id: int, db: Session = Depends(get_db)):
    keyword = db.get(Keyword, keyword_id)
    if keyword is not None:
        db.delete(keyword)
        db.commit()
    return RedirectResponse("/research?ok=Удалено", status_code=303)


# ------------------------------------------------------------------ Посты


@router.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)
    if account is None:
        return RedirectResponse("/", status_code=303)

    posts = list(
        db.scalars(
            select(Post)
            .where(Post.account_id == account.id, Post.chain_index == 0)
            .order_by(Post.published_at.desc())
            .limit(100)
        )
    )
    context.update({"posts": posts, "er_of": analytics.engagement_rate, "engagement_of": analytics.engagement})
    return templates.TemplateResponse(request, "posts.html", context)


@router.get("/posts/{post_id}", response_class=HTMLResponse)
def post_detail(request: Request, post_id: int, db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)
    post = db.get(Post, post_id)
    if post is None or account is None:
        return RedirectResponse("/posts?error=Пост+не+найден", status_code=303)

    metrics = list(
        db.scalars(
            select(PostMetric).where(PostMetric.post_id == post.id).order_by(PostMetric.collected_at)
        )
    )
    chain = list(
        db.scalars(
            select(Post)
            .where(Post.account_id == account.id, Post.root_media_id == (post.root_media_id or post.media_id))
            .order_by(Post.chain_index)
        )
    )
    context.update(
        {
            "post": post,
            "metrics": metrics,
            "chain": chain,
            "er": analytics.engagement_rate(post),
            "engagement": analytics.engagement(post),
        }
    )
    return templates.TemplateResponse(request, "post_detail.html", context)


# ------------------------------------------------------------------ Настройки


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    account = get_active_account(db)
    context = _base_context(request, db, account)
    context.update(
        {
            "values": get_settings_dict(db, account.id) if account else dict(DEFAULT_SETTINGS),
            "app_settings": app_settings,
            "jobs": list(db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(15))),
            "quota": None,
        }
    )
    if account is not None:
        from app.security import decrypt
        from app.threads_api import ThreadsClient

        try:
            # Короткий таймаут и без повторов: страница не должна ждать сеть
            quick = ThreadsClient(decrypt(account.access_token_enc), account.threads_user_id, timeout=6.0)
            context["quota"] = publisher.remaining_quota(quick)
        except Exception as exc:  # noqa: BLE001 - показываем страницу даже если API недоступен
            log.warning("Квоту получить не удалось: %s", exc)
    return templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    account = get_active_account(db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    for key in DEFAULT_SETTINGS:
        if key in form:
            set_setting(db, account.id, key, str(form[key]).strip())
    # Чекбокс автопилота приходит только когда включён
    set_setting(db, account.id, "autopilot", "true" if form.get("autopilot") else "false")
    db.commit()
    return RedirectResponse("/settings?ok=Настройки+сохранены", status_code=303)


@router.post("/jobs/{name}/run")
def run_job_now(name: str, background: BackgroundTasks):
    from app.scheduler import JOBS_BY_NAME

    job = JOBS_BY_NAME.get(name)
    if job is None:
        return RedirectResponse("/settings?error=Неизвестная+задача", status_code=303)
    background.add_task(job)
    return RedirectResponse(f"/settings?ok=Задача+{name}+запущена", status_code=303)


@router.get("/healthz")
def healthz():
    with session_scope() as db:
        db.execute(select(1))
    return {"status": "ok"}
