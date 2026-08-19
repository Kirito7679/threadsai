"""Веб-панель: дашборд, очередь модерации, тренды, посты, настройки."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import analytics, generator, publisher, research
from app.accounts import (
    DEFAULT_SETTINGS,
    NEEDS_REAUTH,
    account_tz,
    get_account_by_threads_id,
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
from app.security import read_session_cookie
from app.templating import set_request_tz, templates
from app.threads_api import MAX_POST_CHARS

log = logging.getLogger(__name__)
router = APIRouter()
# Пояс сервиса — запасной, когда кабинета ещё нет
LOCAL_TZ = ZoneInfo(app_settings.timezone)

# Список для выпадающего меню в настройках. Полный набор из zoneinfo — это
# около шестисот пунктов, выбирать в них неудобно; здесь пояса, в которых
# реально живут пользователи, плюс тот, что задан сервису.
COMMON_TIMEZONES = sorted(
    {
        app_settings.timezone,
        "UTC",
        "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Europe/Kyiv",
        "Europe/Minsk", "Europe/Riga", "Europe/Vilnius", "Europe/Tallinn",
        "Europe/Warsaw", "Europe/Berlin", "Europe/Paris", "Europe/Madrid",
        "Europe/Rome", "Europe/Amsterdam", "Europe/Lisbon", "Europe/London",
        "Europe/Istanbul", "Europe/Belgrade", "Europe/Prague",
        "Asia/Tbilisi", "Asia/Yerevan", "Asia/Baku", "Asia/Almaty",
        "Asia/Tashkent", "Asia/Bishkek", "Asia/Dushanbe", "Asia/Ashgabat",
        "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Novosibirsk", "Asia/Krasnoyarsk",
        "Asia/Irkutsk", "Asia/Yakutsk", "Asia/Vladivostok",
        "Asia/Dubai", "Asia/Jerusalem", "Asia/Bangkok", "Asia/Shanghai",
        "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Kolkata",
        "America/New_York", "America/Chicago", "America/Denver",
        "America/Los_Angeles", "America/Sao_Paulo", "America/Mexico_City",
        "Australia/Sydney", "Australia/Perth", "Africa/Cairo", "Africa/Lagos",
    }
)


def current_account(request: Request, db: Session) -> Account | None:
    """Аккаунт текущей сессии — строго тот, чьим Threads-аккаунтом вошли.

    Раньше при неудачном поиске по uid происходил откат на первый активный
    аккаунт в базе. Пока пользователь один, это незаметно; при публичном
    входе любой сбой поиска отправлял человека в чужой кабинет с полными
    правами. Теперь не нашли свой — не отдаём никакой.

    Откат на первый аккаунт остаётся только у сессии владельца, полученной
    запасным входом по паролю: это его собственное развёртывание.
    """
    session = read_session_cookie(request.cookies.get("session")) or {}
    uid = str(session.get("uid", "") or "")
    if uid:
        return get_account_by_threads_id(db, uid)
    if session.get("owner"):
        return get_active_account(db)
    return None


def owned_draft(request: Request, db: Session, draft_id: int) -> Draft | None:
    """Черновик текущего аккаунта. Чужие не отдаём даже по прямому номеру."""
    account = current_account(request, db)
    draft = db.get(Draft, draft_id)
    if draft is None or account is None or draft.account_id != account.id:
        return None
    return draft


def _base_context(request: Request, db: Session, account: Account | None) -> dict:
    pending_count = 0
    tz = LOCAL_TZ
    if account is not None:
        pending_count = (
            db.query(Draft)
            .filter(Draft.account_id == account.id, Draft.status == "pending")
            .count()
        )
        # Все даты на странице показываем в поясе владельца кабинета
        tz = account_tz(db, account.id)
    set_request_tz(tz)
    return {
        "tz_name": str(tz),
        "request": request,
        "account": account,
        "pending_count": pending_count,
        "app_settings": app_settings,
        "llm_ready": get_llm().is_configured,
        # Токен отозван или протух: без баннера человек узнает об этом только
        # по тому, что посты перестали выходить.
        "needs_reauth": account is not None and account.status == NEEDS_REAUTH,
        "flash_ok": request.query_params.get("ok", ""),
        "flash_error": request.query_params.get("error", ""),
    }


def account_jobs(db: Session, account: Account | None, limit: int = 15) -> list[JobRun]:
    """Журнал задач своего кабинета. Чужие запуски не показываем."""
    if account is None:
        return []
    return list(
        db.scalars(
            select(JobRun)
            .where(JobRun.account_id == account.id)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
        )
    )


def _parse_local_datetime(raw: str, tz: ZoneInfo) -> datetime | None:
    """Строка из <input type=datetime-local> -> aware UTC.

    Пояс берётся из настроек аккаунта: поле в браузере показывает местное
    время владельца, и трактовать его в поясе сервиса значит промахнуться
    на разницу поясов.
    """
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


# ------------------------------------------------------------------ Дашборд


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    account = current_account(request, db)
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
            "jobs": account_jobs(db, account, limit=8),
            "report": latest_report(db, account.id),
        }
    )
    context["engagement_of"] = analytics.engagement
    return templates.TemplateResponse(request, "dashboard.html", context)


# ------------------------------------------------------------------ Очередь


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, status: str = "pending", db: Session = Depends(get_db)):
    account = current_account(request, db)
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
def queue_generate(request: Request, count: int = Form(3), db: Session = Depends(get_db)):
    account = current_account(request, db)
    if account is None:
        return RedirectResponse("/", status_code=303)
    result = generator.generate_drafts(db, account, count=max(1, min(count, 10)))
    db.commit()
    if result.get("error"):
        return RedirectResponse(f"/queue?error={result['error']}", status_code=303)
    return RedirectResponse(f"/queue?ok=Готово+черновиков:+{result['created']}", status_code=303)


@router.post("/queue/{draft_id}/approve")
def approve(request: Request, draft_id: int, scheduled_at: str = Form(""), db: Session = Depends(get_db)):
    draft = owned_draft(request, db, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)

    when = _parse_local_datetime(scheduled_at, account_tz(db, draft.account_id))
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
def approve_all(request: Request, db: Session = Depends(get_db)):
    account = current_account(request, db)
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
def reject(request: Request, draft_id: int, reason: str = Form(""), db: Session = Depends(get_db)):
    draft = owned_draft(request, db, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)
    draft.status = "rejected"
    draft.reject_reason = reason
    db.commit()
    return RedirectResponse("/queue?ok=Отклонено", status_code=303)


@router.post("/queue/{draft_id}/edit")
def edit(request: Request, draft_id: int, parts: str = Form(...), topic: str = Form(""), db: Session = Depends(get_db)):
    """Части ветки разделяются пустой строкой."""
    draft = owned_draft(request, db, draft_id)
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
def regenerate(request: Request, draft_id: int, instruction: str = Form(""), db: Session = Depends(get_db)):
    draft = owned_draft(request, db, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)
    result = generator.regenerate_draft(db, draft, instruction)
    db.commit()
    if not result.get("ok"):
        return RedirectResponse(f"/queue?error={result.get('error')}", status_code=303)
    return RedirectResponse("/queue?ok=Переписано", status_code=303)


@router.post("/queue/{draft_id}/publish-now")
def publish_now(request: Request, draft_id: int, db: Session = Depends(get_db)):
    draft = owned_draft(request, db, draft_id)
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
def delete_draft(request: Request, draft_id: int, db: Session = Depends(get_db)):
    draft = owned_draft(request, db, draft_id)
    if draft is None:
        return RedirectResponse("/queue?error=Черновик+не+найден", status_code=303)
    db.delete(draft)
    db.commit()
    return RedirectResponse("/queue?ok=Удалено", status_code=303)


# ------------------------------------------------------------------ Тренды


@router.get("/research", response_class=HTMLResponse)
def research_page(request: Request, db: Session = Depends(get_db)):
    account = current_account(request, db)
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
def research_run(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    account = current_account(request, db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    # Только свой аккаунт: чужую квоту поиска кнопка в этом кабинете не тратит
    account_id = account.id

    def work() -> None:
        from app.scheduler import run_job_for_account

        run_job_for_account("research", account_id)

    background.add_task(work)
    return RedirectResponse("/research?ok=Разведка+запущена+в+фоне", status_code=303)


@router.post("/keywords/add")
def add_keyword(request: Request, term: str = Form(...), search_mode: str = Form("KEYWORD"), db: Session = Depends(get_db)):
    account = current_account(request, db)
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
def toggle_keyword(request: Request, keyword_id: int, db: Session = Depends(get_db)):
    account = current_account(request, db)
    keyword = db.get(Keyword, keyword_id)
    if keyword is None or account is None or keyword.account_id != account.id:
        return RedirectResponse("/research?error=Ключевое+слово+не+найдено", status_code=303)
    keyword.is_active = not keyword.is_active
    db.commit()
    return RedirectResponse("/research", status_code=303)


@router.post("/keywords/{keyword_id}/delete")
def delete_keyword(request: Request, keyword_id: int, db: Session = Depends(get_db)):
    account = current_account(request, db)
    keyword = db.get(Keyword, keyword_id)
    if keyword is None or account is None or keyword.account_id != account.id:
        return RedirectResponse("/research?error=Ключевое+слово+не+найдено", status_code=303)
    db.delete(keyword)
    db.commit()
    return RedirectResponse("/research?ok=Удалено", status_code=303)


# ------------------------------------------------------------------ Посты


@router.get("/posts", response_class=HTMLResponse)
def posts_page(request: Request, db: Session = Depends(get_db)):
    account = current_account(request, db)
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
    account = current_account(request, db)
    context = _base_context(request, db, account)
    post = db.get(Post, post_id)
    if post is None or account is None or post.account_id != account.id:
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
    account = current_account(request, db)
    context = _base_context(request, db, account)
    context.update(
        {
            "values": get_settings_dict(db, account.id) if account else dict(DEFAULT_SETTINGS),
            "app_settings": app_settings,
            "jobs": account_jobs(db, account, limit=15),
            "spend": analytics.llm_spend(db, account.id) if account else None,
            "timezones": COMMON_TIMEZONES,
            "quota": None,
        }
    )
    # У отвалившегося аккаунта спрашивать квоту незачем — ответ всё равно будет
    # ошибкой авторизации, а страница на 6 секунд подвиснет.
    if account is not None and account.status != NEEDS_REAUTH:
        from app.security import decrypt
        from app.threads_api import ThreadsClient

        try:
            # Короткий таймаут и без повторов: страница не должна ждать сеть
            with ThreadsClient(
                decrypt(account.access_token_enc), account.threads_user_id, timeout=6.0
            ) as quick:
                context["quota"] = publisher.remaining_quota(quick)
        except Exception as exc:  # noqa: BLE001 - показываем страницу даже если API недоступен
            log.warning("Квоту получить не удалось: %s", exc)
    return templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    account = current_account(request, db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    for key in DEFAULT_SETTINGS:
        if key not in form:
            continue
        value = str(form[key]).strip()

        # Битый пояс сломал бы и расписание, и показ дат — не принимаем
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                return RedirectResponse(
                    f"/settings?error=Неизвестный+часовой+пояс:+{value}", status_code=303
                )
        if key == "generation_hour":
            if not value.isdigit() or not 0 <= int(value) <= 23:
                return RedirectResponse(
                    "/settings?error=Час+генерации+должен+быть+числом+от+0+до+23", status_code=303
                )

        set_setting(db, account.id, key, value)

    # Чекбокс автопилота приходит только когда включён
    set_setting(db, account.id, "autopilot", "true" if form.get("autopilot") else "false")
    db.commit()
    return RedirectResponse("/settings?ok=Настройки+сохранены", status_code=303)


@router.post("/jobs/{name}/run")
def run_job_now(request: Request, name: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    from app.scheduler import JOB_WORK, run_job_for_account

    if name not in JOB_WORK:
        return RedirectResponse("/settings?error=Неизвестная+задача", status_code=303)

    account = current_account(request, db)
    if account is None:
        return RedirectResponse("/", status_code=303)

    # Кнопка в кабинете запускает задачу только для этого кабинета
    account_id = account.id
    background.add_task(lambda: run_job_for_account(name, account_id))
    return RedirectResponse(f"/settings?ok=Задача+{name}+запущена", status_code=303)


@router.get("/healthz")
def healthz():
    with session_scope() as db:
        db.execute(select(1))
    return {"status": "ok"}
