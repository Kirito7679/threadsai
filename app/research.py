"""Разведка: сбор популярных чужих веток и построение отчёта по трендам.

Важное ограничение платформы: официальный API не отдаёт лайки/просмотры чужих
постов. Прокси популярности — параметр search_type=TOP в keyword_search:
Threads сам ранжирует выдачу по популярности. Мы это фиксируем и используем
как сигнал, а не как точную метрику.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import client_for, get_keywords, get_settings_dict, set_setting
from app.llm import LLMError, get_llm
from app.models import Account, ResearchPost, TrendReport
from app.threads_api import ThreadsAPIError, parse_timestamp

log = logging.getLogger(__name__)

ANALYZE_BATCH = 40


def collect_research(db: Session, account: Account, per_keyword: int = 25) -> dict:
    """Обходит ключевые слова и складывает найденные посты в базу."""
    client = client_for(account)
    keywords = get_keywords(db, account.id)
    if not keywords:
        return {"collected": 0, "new": 0, "note": "Ключевые слова не заданы"}

    existing = {
        row
        for row in db.scalars(
            select(ResearchPost.threads_post_id).where(ResearchPost.account_id == account.id)
        )
    }

    collected = 0
    new = 0
    errors: list[str] = []
    permission_denied = False

    for keyword in keywords:
        for search_type in ("TOP", "RECENT"):
            try:
                results = client.keyword_search(
                    keyword.term,
                    search_type=search_type,
                    search_mode=keyword.search_mode,
                    limit=per_keyword,
                )
            except ThreadsAPIError as exc:
                log.warning("Поиск '%s' (%s) не удался: %s", keyword.term, search_type, exc)
                errors.append(f"{keyword.term}/{search_type}: {exc}")

                # Meta отдаёт публичный поиск только приложениям, прошедшим App Review
                if "permission for this action" in str(exc).lower():
                    permission_denied = True
                    set_setting(db, account.id, "keyword_search_denied", "true")
                    return {
                        "collected": collected,
                        "new": new,
                        "errors": errors[:2],
                        "permission_denied": True,
                        "note": "Нет разрешения threads_keyword_search — нужен App Review",
                    }

                if exc.is_rate_limit or exc.is_auth_error:
                    return {
                        "collected": collected,
                        "new": new,
                        "errors": errors,
                        "note": "Остановились: лимит запросов или проблема с токеном",
                    }
                continue

            for item in results:
                collected += 1
                post_id = item.get("id")
                if not post_id or post_id in existing:
                    continue
                existing.add(post_id)
                new += 1
                db.add(
                    ResearchPost(
                        account_id=account.id,
                        threads_post_id=post_id,
                        keyword=keyword.term,
                        search_type=search_type,
                        username=item.get("username", ""),
                        text=item.get("text", "") or "",
                        media_type=item.get("media_type", "TEXT_POST"),
                        permalink=item.get("permalink", ""),
                        posted_at=parse_timestamp(item.get("timestamp")),
                        has_replies=bool(item.get("has_replies")),
                        is_reply=bool(item.get("is_reply")),
                    )
                )
            db.flush()

    if collected and not permission_denied:
        set_setting(db, account.id, "keyword_search_denied", "false")
    return {"collected": collected, "new": new, "errors": errors}


ANALYST_SYSTEM = """Ты аналитик соцсети Threads. Твоя работа — разбирать чужие посты
и объяснять, за счёт чего они собирают внимание.

Отвечай ТОЛЬКО валидным JSON без пояснений вокруг.
Формат ответа:
{"items": [{"id": "<id поста из входных данных>", "topic": "<тема, 1-3 слова>",
"hook_type": "<тип зацепки: вопрос|провокация|личная история|цифра|список|боль|инсайт|новость|мем>",
"format_type": "<формат: короткая мысль|тред|список|история|вопрос аудитории|разбор>",
"tone": "<тон: экспертный|дружеский|ироничный|эмоциональный|нейтральный>",
"insight": "<одно предложение: почему пост цепляет>",
"relevance": <число 0..1, насколько пост релевантен нише аккаунта>}]}"""


def analyze_research(db: Session, account: Account, limit: int = ANALYZE_BATCH) -> dict:
    """Прогоняет неразобранные посты через LLM-классификатор."""
    llm = get_llm()
    if not llm.is_configured:
        return {"analyzed": 0, "note": "DEEPSEEK_API_KEY не задан"}

    pending = list(
        db.scalars(
            select(ResearchPost)
            .where(
                ResearchPost.account_id == account.id,
                ResearchPost.analyzed.is_(False),
                ResearchPost.text != "",
            )
            .order_by(ResearchPost.search_type.desc(), ResearchPost.collected_at.desc())
            .limit(limit)
        )
    )
    if not pending:
        return {"analyzed": 0, "note": "Нечего разбирать"}

    settings_map = get_settings_dict(db, account.id)
    payload = [
        {
            "id": str(post.id),
            "text": post.text[:600],
            "keyword": post.keyword,
            "popular": post.search_type == "TOP",
        }
        for post in pending
    ]

    user_prompt = (
        f"Ниша аккаунта: {settings_map.get('niche') or 'не указана'}\n"
        f"Аудитория: {settings_map.get('audience') or 'не указана'}\n\n"
        f"Разбери {len(payload)} постов. Поле popular=true означает, что пост "
        f"попал в топ выдачи Threads, то есть собрал заметное внимание.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        result = llm.chat_json(ANALYST_SYSTEM, user_prompt, temperature=0.3, max_tokens=6000)
    except LLMError as exc:
        log.error("Классификация не удалась: %s", exc)
        return {"analyzed": 0, "error": str(exc)}

    items = result.get("items", []) if isinstance(result, dict) else []
    by_id = {str(post.id): post for post in pending}
    analyzed = 0

    for item in items:
        post = by_id.get(str(item.get("id")))
        if post is None:
            continue
        post.topic = str(item.get("topic", ""))[:120]
        post.hook_type = str(item.get("hook_type", ""))[:64]
        post.format_type = str(item.get("format_type", ""))[:64]
        post.tone = str(item.get("tone", ""))[:64]
        post.insight = str(item.get("insight", ""))
        try:
            post.relevance = max(0.0, min(1.0, float(item.get("relevance", 0))))
        except (TypeError, ValueError):
            post.relevance = 0.0
        post.analyzed = True
        analyzed += 1

    # Посты, которые модель проигнорировала, помечаем разобранными,
    # чтобы они не блокировали очередь на каждом запуске.
    for post in pending:
        if not post.analyzed:
            post.analyzed = True

    return {"analyzed": analyzed, "sent": len(pending)}


STRATEGIST_SYSTEM = """Ты контент-стратег в Threads. На входе — сводка по постам,
которые сейчас популярны в нише. Твоя задача — выделить рабочие тренды и предложить,
о чём писать аккаунту.

Отвечай ТОЛЬКО валидным JSON:
{"summary": "<3-5 предложений: что происходит в нише прямо сейчас>",
 "trends": [{"name": "<название тренда>", "why": "<почему заходит>",
             "example_hooks": ["<пример зацепки>", "..."], "strength": <0..1>}],
 "content_ideas": [{"topic": "<тема>", "angle": "<под каким углом подать>",
                    "hook_type": "<тип зацепки>", "why_now": "<почему сейчас>"}],
 "avoid": ["<что в этой нише выглядит заезженным>"]}"""


def build_trend_report(db: Session, account: Account, window_hours: int = 72) -> TrendReport | None:
    """Собирает агрегированный отчёт по трендам за окно наблюдения."""
    llm = get_llm()
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    posts = list(
        db.scalars(
            select(ResearchPost)
            .where(
                ResearchPost.account_id == account.id,
                ResearchPost.collected_at >= since,
                ResearchPost.analyzed.is_(True),
            )
            .order_by(ResearchPost.relevance.desc())
            .limit(120)
        )
    )
    if not posts:
        log.info("Нет разобранных постов для отчёта")
        return None

    top_posts = [p for p in posts if p.search_type == "TOP"]
    topics = Counter(p.topic for p in top_posts if p.topic)
    hooks = Counter(p.hook_type for p in top_posts if p.hook_type)
    formats = Counter(p.format_type for p in top_posts if p.format_type)

    stats = {
        "posts_in_window": len(posts),
        "popular_posts": len(top_posts),
        "top_topics": topics.most_common(12),
        "top_hooks": hooks.most_common(8),
        "top_formats": formats.most_common(8),
    }

    settings_map = get_settings_dict(db, account.id)
    samples = [
        {
            "text": p.text[:400],
            "topic": p.topic,
            "hook": p.hook_type,
            "insight": p.insight,
            "popular": p.search_type == "TOP",
        }
        for p in sorted(posts, key=lambda x: (x.search_type == "TOP", x.relevance), reverse=True)[:35]
    ]

    payload: dict = {"stats": stats, "generated_by": "fallback"}

    if llm.is_configured:
        user_prompt = (
            f"Ниша: {settings_map.get('niche') or 'не указана'}\n"
            f"Аудитория: {settings_map.get('audience') or 'не указана'}\n"
            f"Цели: {settings_map.get('goals')}\n"
            f"Язык контента: {settings_map.get('language')}\n\n"
            f"Частотность по популярным постам:\n{json.dumps(stats, ensure_ascii=False)}\n\n"
            f"Примеры постов:\n{json.dumps(samples, ensure_ascii=False)}"
        )
        try:
            result = llm.chat_json(STRATEGIST_SYSTEM, user_prompt, temperature=0.6, max_tokens=4000)
            if isinstance(result, dict):
                payload = {**result, "stats": stats, "generated_by": llm.model}
        except LLMError as exc:
            log.error("Отчёт по трендам не построен: %s", exc)
            payload["error"] = str(exc)

    report = TrendReport(
        account_id=account.id,
        window_hours=window_hours,
        posts_analyzed=len(posts),
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(report)
    db.flush()
    return report


def latest_report(db: Session, account_id: int) -> TrendReport | None:
    return db.scalars(
        select(TrendReport)
        .where(TrendReport.account_id == account_id)
        .order_by(TrendReport.created_at.desc())
        .limit(1)
    ).first()


def run_full_research(db: Session, account: Account) -> dict:
    """Полный цикл: сбор -> классификация -> отчёт."""
    collected = collect_research(db, account)
    db.flush()
    analyzed = analyze_research(db, account)
    db.flush()
    report = build_trend_report(db, account)
    return {
        "collected": collected,
        "analyzed": analyzed,
        "report_id": report.id if report else None,
    }
