"""Генерация черновиков веток на основе трендов и собственной статистики."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import account_tz, get_settings_dict
from app.analytics import engagement, top_posts, topic_performance
from app.config import settings as app_settings
from app.llm import LLMError, get_llm, record_usage
from app.models import Account, Draft
from app.research import latest_report
from app.threads_api import MAX_POST_CHARS

log = logging.getLogger(__name__)

WRITER_SYSTEM = """Ты пишешь посты для Threads от лица владельца аккаунта.

Жёсткие правила:
1. Каждая часть ветки — максимум 480 символов. Это лимит платформы, превышение ломает публикацию.
2. Первая часть — самостоятельный пост, который цепляет сам по себе. Читатель видит только её.
3. Пиши живым языком, как человек, а не как маркетинговый отдел. Без канцелярита.
4. Никаких хэштегов пачками: максимум один и только по делу.
5. Не выдумывай факты, цифры и кейсы, которых нет во входных данных. Лучше личное наблюдение, чем ложная статистика.
6. Не копируй чужие посты из примеров. Примеры нужны только для понимания формата и темы.
7. Ветка из 1-4 частей. Если мысль укладывается в один пост — делай один.

Отвечай ТОЛЬКО валидным JSON:
{"drafts": [{"topic": "<рубрика или тема, 1-3 слова>",
             "hook_type": "<тип зацепки>",
             "parts": ["<часть 1>", "<часть 2>"],
             "rationale": "<1-2 предложения: на каком сигнале основан пост>"}]}"""


def _format_own_posts(posts: list) -> str:
    if not posts:
        return "Статистики по своим постам пока нет."
    lines = []
    for post in posts:
        lines.append(
            f"- [{post.views} просмотров, {engagement(post)} реакций] {post.text[:220]}"
        )
    return "\n".join(lines)


def build_brief(db: Session, account: Account) -> dict:
    """Собирает контекст для генерации: тренды, свою статистику, настройки."""
    conf = get_settings_dict(db, account.id)
    report = latest_report(db, account.id)
    trends: dict = {}
    if report is not None:
        try:
            trends = json.loads(report.payload_json)
        except json.JSONDecodeError:
            trends = {}

    best = top_posts(db, account.id, days=45, limit=8)
    worst = sorted(
        [p for p in top_posts(db, account.id, days=45, limit=200) if p.views > 0],
        key=engagement,
    )[:5]

    recent_drafts = list(
        db.scalars(
            select(Draft)
            .where(Draft.account_id == account.id)
            .order_by(Draft.created_at.desc())
            .limit(15)
        )
    )
    recent_topics = [d.topic for d in recent_drafts if d.topic]

    return {
        "config": conf,
        "trends": trends,
        "report_id": report.id if report else None,
        "best_posts": best,
        "worst_posts": worst,
        "topic_performance": topic_performance(db, account.id),
        "recent_topics": recent_topics,
    }


def _user_prompt(brief: dict, count: int) -> str:
    conf = brief["config"]
    trends = brief["trends"]

    trend_block = "Свежих данных по трендам пока нет."
    if trends:
        parts = []
        if trends.get("summary"):
            parts.append(f"Что происходит в нише: {trends['summary']}")
        if trends.get("trends"):
            parts.append("Тренды:\n" + json.dumps(trends["trends"], ensure_ascii=False, indent=1))
        if trends.get("content_ideas"):
            parts.append(
                "Идеи от аналитика:\n" + json.dumps(trends["content_ideas"], ensure_ascii=False, indent=1)
            )
        if trends.get("avoid"):
            parts.append("Заезженное, избегать: " + ", ".join(trends["avoid"]))
        trend_block = "\n\n".join(parts)

    perf = brief["topic_performance"]
    perf_block = (
        json.dumps(perf[:8], ensure_ascii=False)
        if perf
        else "Данных по рубрикам пока недостаточно."
    )

    return f"""ПРОФИЛЬ АККАУНТА
Ниша: {conf.get('niche') or 'не указана'}
Аудитория: {conf.get('audience') or 'не указана'}
Цели: {conf.get('goals')}
Язык: {conf.get('language')}
Рубрики: {conf.get('rubrics')}

ТОН ГОЛОСА (соблюдай дословно)
{conf.get('brand_voice')}

ЗАПРЕЩЕНО
{conf.get('forbidden')}

ЧТО СЕЙЧАС РАБОТАЕТ В НИШЕ
{trend_block}

МОИ ЛУЧШИЕ ПОСТЫ (ориентир по формату и тону)
{_format_own_posts(brief['best_posts'])}

МОИ СЛАБЫЕ ПОСТЫ (так больше не надо)
{_format_own_posts(brief['worst_posts'])}

ЭФФЕКТИВНОСТЬ РУБРИК (avg_views — средние просмотры, er — вовлечённость в %)
{perf_block}

НЕДАВНО УЖЕ ПИСАЛИ (не повторяйся)
{', '.join(brief['recent_topics']) or 'ничего'}

ЗАДАЧА
Придумай {count} разных веток. Разные рубрики, разные типы зацепок, разная длина.
Как минимум одна ветка должна опираться на конкретный тренд из блока выше."""


def _validate_parts(parts: list) -> list[str]:
    """Обрезает и чистит части, отбрасывает пустые."""
    cleaned: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        if len(text) > MAX_POST_CHARS:
            # Режем по границе предложения, чтобы не обрывать на полуслове
            cut = text[: MAX_POST_CHARS - 1]
            for sep in (". ", "! ", "? ", "\n"):
                index = cut.rfind(sep)
                if index > MAX_POST_CHARS * 0.6:
                    cut = cut[: index + 1]
                    break
            text = cut.strip()
        cleaned.append(text)
    return cleaned[:4]


def generate_drafts(db: Session, account: Account, count: int | None = None) -> dict:
    """Генерирует черновики и кладёт их в очередь на модерацию."""
    count = count or app_settings.drafts_per_run
    llm = get_llm()
    if not llm.is_configured:
        return {"created": 0, "error": "DEEPSEEK_API_KEY не задан"}

    brief = build_brief(db, account)
    try:
        result = llm.chat_json(
            WRITER_SYSTEM,
            _user_prompt(brief, count),
            temperature=0.9,
            max_tokens=4000,
        )
    except LLMError as exc:
        log.error("Генерация не удалась: %s", exc)
        return {"created": 0, "error": str(exc)}
    record_usage(db, account.id, "generate", getattr(llm, "last_usage", None))

    items = result.get("drafts", []) if isinstance(result, dict) else []
    if not items:
        return {"created": 0, "error": "Модель вернула пустой список"}

    created = 0
    for item in items[:count]:
        parts = _validate_parts(item.get("parts", []))
        if not parts:
            continue
        db.add(
            Draft(
                account_id=account.id,
                status="pending",
                parts_json=json.dumps(parts, ensure_ascii=False),
                topic=str(item.get("topic", ""))[:120],
                hook_type=str(item.get("hook_type", ""))[:64],
                rationale=str(item.get("rationale", "")),
                source_report_id=brief["report_id"],
                model=llm.model,
            )
        )
        created += 1

    db.flush()
    return {"created": created, "model": llm.model, "report_id": brief["report_id"]}


def regenerate_draft(db: Session, draft: Draft, instruction: str = "") -> dict:
    """Переписывает конкретный черновик с учётом замечания пользователя."""
    llm = get_llm()
    if not llm.is_configured:
        return {"ok": False, "error": "DEEPSEEK_API_KEY не задан"}

    account = db.get(Account, draft.account_id)
    if account is None:
        return {"ok": False, "error": "Аккаунт не найден"}

    brief = build_brief(db, account)
    current = json.loads(draft.parts_json or "[]")

    prompt = (
        f"{_user_prompt(brief, 1)}\n\n"
        f"ТЕКУЩИЙ ВАРИАНТ (нужно переписать)\n"
        f"{json.dumps(current, ensure_ascii=False, indent=1)}\n\n"
        f"ЗАМЕЧАНИЕ ОТ АВТОРА\n{instruction or 'Сделай сильнее: острее зацепка, конкретнее содержание.'}\n\n"
        f"Верни ровно один переписанный черновик."
    )

    try:
        result = llm.chat_json(WRITER_SYSTEM, prompt, temperature=0.9, max_tokens=2500)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    record_usage(db, account.id, "rewrite", getattr(llm, "last_usage", None))

    items = result.get("drafts", []) if isinstance(result, dict) else []
    if not items:
        return {"ok": False, "error": "Модель вернула пустой ответ"}

    item = items[0]
    parts = _validate_parts(item.get("parts", []))
    if not parts:
        return {"ok": False, "error": "В ответе нет текста"}

    draft.parts_json = json.dumps(parts, ensure_ascii=False)
    draft.topic = str(item.get("topic", draft.topic))[:120]
    draft.hook_type = str(item.get("hook_type", draft.hook_type))[:64]
    draft.rationale = str(item.get("rationale", draft.rationale))
    draft.edited_by_human = False
    draft.status = "pending"
    return {"ok": True}


def propose_slots(db: Session, account: Account, count: int) -> list[datetime]:
    """Ближайшие свободные слоты по расписанию из настроек (в UTC).

    Часы публикации задаются в поясе владельца кабинета, а не сервиса:
    «9, 13, 19» у пользователя из другого пояса — это его 9, 13 и 19.
    """
    conf = get_settings_dict(db, account.id)
    tz = account_tz(db, account.id)

    try:
        hours = sorted({int(h.strip()) for h in conf.get("posting_hours", "9,13,19").split(",") if h.strip()})
    except ValueError:
        hours = [9, 13, 19]
    if not hours:
        hours = [9, 13, 19]

    try:
        per_day = max(1, int(conf.get("posts_per_day", "3")))
    except ValueError:
        per_day = 3

    # SQLite отдаёт naive datetime — приводим всё к UTC-aware, иначе сравнение упадёт
    taken = {
        (slot if slot.tzinfo else slot.replace(tzinfo=timezone.utc)).replace(second=0, microsecond=0)
        for slot in db.scalars(
            select(Draft.scheduled_at).where(
                Draft.account_id == account.id,
                Draft.status.in_(("approved", "publishing")),
                Draft.scheduled_at.is_not(None),
            )
        )
        if slot is not None
    }

    now_local = datetime.now(tz)
    slots: list[datetime] = []
    day_offset = 0

    while len(slots) < count and day_offset < 30:
        day = (now_local + timedelta(days=day_offset)).date()
        used_today = 0
        for hour in hours:
            if used_today >= per_day:
                break
            candidate_local = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            if candidate_local <= now_local + timedelta(minutes=5):
                continue
            candidate_utc = candidate_local.astimezone(timezone.utc).replace(second=0, microsecond=0)
            if any(abs((candidate_utc - t).total_seconds()) < 60 for t in taken):
                continue
            slots.append(candidate_utc)
            taken.add(candidate_utc)
            used_today += 1
            if len(slots) >= count:
                break
        day_offset += 1

    return slots
