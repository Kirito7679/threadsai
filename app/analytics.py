"""Сбор и агрегация аналитики по своим постам и аккаунту."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts import client_for, mark_api_error, mark_healthy
from app.models import Account, AccountMetric, Draft, LlmUsage, Post, PostMetric
from app.threads_api import ThreadsAPIError, parse_timestamp

log = logging.getLogger(__name__)

# Посты старше этого возраста почти не набирают охват — метрики не опрашиваем
METRICS_MAX_AGE_DAYS = 30

# Как часто опрашивать пост в зависимости от его возраста.
# Раньше каждый пост моложе 30 дней опрашивался ежечасно: 720 запросов на пост,
# который не меняется уже месяц. При росте числа аккаунтов цикл не замыкается.
METRICS_TIERS: tuple[tuple[timedelta, timedelta], ...] = (
    (timedelta(hours=24), timedelta(hours=1)),  # первые сутки — каждый час
    (timedelta(days=7), timedelta(hours=6)),  # до недели — раз в 6 часов
    (timedelta(days=30), timedelta(days=1)),  # до месяца — раз в сутки
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _adopt_by_draft(db: Session, account: Account, post: Post) -> bool:
    """Привязывает подхваченный синхронизацией пост к его черновику.

    Синхронизация идёт раз в час и может увидеть пост раньше, чем publisher
    успеет зафиксировать свою строку. Тогда пост навсегда останется с пустым
    topic и выпадет из topic_performance — то есть из обратной связи, ради
    которой всё и затевалось. Ищем черновик по точному совпадению текста части.
    """
    text = (post.text or "").strip()
    if not text:
        return False

    since = datetime.now(timezone.utc) - timedelta(days=2)
    candidates = db.scalars(
        select(Draft).where(
            Draft.account_id == account.id,
            Draft.status.in_(("published", "publishing", "failed")),
            Draft.created_at >= since,
        )
    )
    for draft in candidates:
        try:
            parts = json.loads(draft.parts_json or "[]")
        except json.JSONDecodeError:
            continue
        for index, part in enumerate(parts):
            if str(part).strip() != text:
                continue
            post.draft_id = draft.id
            post.topic = draft.topic
            post.chain_index = index
            post.is_reply = index > 0
            log.info("Пост %s привязан к черновику %s", post.media_id, draft.id)
            return True
    return False


def sync_own_posts(db: Session, account: Account, limit: int = 100) -> dict:
    """Подтягивает опубликованные посты аккаунта (включая опубликованные вручную)."""
    client = client_for(account)
    try:
        items = client.get_own_posts(limit=limit)
    except ThreadsAPIError as exc:
        log.error("Не удалось получить свои посты: %s", exc)
        mark_api_error(account, exc)
        return {"synced": 0, "error": str(exc)}

    known = {
        post.media_id: post
        for post in db.scalars(select(Post).where(Post.account_id == account.id))
    }

    created = 0
    updated = 0
    adopted = 0
    for item in items:
        media_id = item.get("id")
        if not media_id:
            continue
        post = known.get(media_id)
        is_new = post is None
        if is_new:
            post = Post(account_id=account.id, media_id=media_id)
            db.add(post)
            known[media_id] = post
            created += 1
        else:
            updated += 1

        post.text = item.get("text", "") or post.text
        post.media_type = item.get("media_type", post.media_type)
        post.permalink = item.get("permalink", "") or post.permalink
        post.published_at = parse_timestamp(item.get("timestamp")) or post.published_at
        # is_reply и chain_index у постов из очереди уже проставлены publisher'ом:
        # выставляем их только для тех, о ком мы ничего не знаем.
        if is_new or not post.draft_id:
            post.is_reply = bool(item.get("is_reply"))

        # Пост из очереди, который синхронизация увидела первой
        if not post.draft_id and not post.topic and _adopt_by_draft(db, account, post):
            adopted += 1

    db.flush()
    return {"synced": len(items), "created": created, "updated": updated, "adopted": adopted}


def needs_metrics(post: Post, now: datetime | None = None) -> bool:
    """Пора ли опрашивать метрики этого поста.

    Свежий пост меняется каждый час, месячный — почти никогда. Опрашивать их
    одинаково значит тратить сотни запросов впустую и не успевать обойти
    аккаунты, когда их станет много.
    """
    now = now or datetime.now(timezone.utc)
    published = _aware(post.published_at)
    if published is None:
        return False

    age = now - published
    if age > timedelta(days=METRICS_MAX_AGE_DAYS):
        return False

    last = _aware(post.metrics_updated_at)
    if last is None:
        return True  # ни разу не опрашивали

    for max_age, interval in METRICS_TIERS:
        if age <= max_age:
            return now - last >= interval
    return False


def collect_post_metrics(db: Session, account: Account, max_posts: int = 60) -> dict:
    """Снимает метрики по постам, которым это нужно, и пишет точку во временной ряд."""
    client = client_for(account)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=METRICS_MAX_AGE_DAYS)

    candidates = list(
        db.scalars(
            select(Post)
            .where(Post.account_id == account.id, Post.published_at >= cutoff)
            .order_by(Post.published_at.desc())
        )
    )
    posts = [post for post in candidates if needs_metrics(post, now)][:max_posts]
    skipped = len(candidates) - len(posts)

    updated = 0
    errors = 0
    for post in posts:
        try:
            metrics = client.get_post_insights(post.media_id)
        except ThreadsAPIError as exc:
            errors += 1
            if exc.is_auth_error:
                mark_api_error(account, exc)
                log.warning("Токен аккаунта @%s не работает, сбор метрик прерван", account.username)
                break
            if exc.is_rate_limit:
                log.warning("Лимит запросов при сборе метрик, останавливаемся")
                break
            log.warning("Метрики поста %s недоступны: %s", post.media_id, exc)
            continue

        post.views = metrics.get("views", post.views)
        post.likes = metrics.get("likes", post.likes)
        post.replies = metrics.get("replies", post.replies)
        post.reposts = metrics.get("reposts", post.reposts)
        post.quotes = metrics.get("quotes", post.quotes)
        post.shares = metrics.get("shares", post.shares)
        post.metrics_updated_at = datetime.now(timezone.utc)

        db.add(
            PostMetric(
                post_id=post.id,
                views=post.views,
                likes=post.likes,
                replies=post.replies,
                reposts=post.reposts,
                quotes=post.quotes,
                shares=post.shares,
            )
        )
        updated += 1

    if updated and not errors:
        mark_healthy(account)
    db.flush()
    return {"due": len(posts), "updated": updated, "errors": errors, "skipped": skipped}


def collect_account_metrics(db: Session, account: Account) -> dict:
    """Дневной срез метрик аккаунта."""
    client = client_for(account)
    now = datetime.now(timezone.utc)
    since = int((now - timedelta(days=1)).timestamp())
    until = int(now.timestamp())

    try:
        metrics = client.get_account_insights(since=since, until=until)
    except ThreadsAPIError as exc:
        log.error("Метрики аккаунта недоступны: %s", exc)
        mark_api_error(account, exc)
        return {"error": str(exc)}
    mark_healthy(account)

    day = now.strftime("%Y-%m-%d")
    row = db.scalars(
        select(AccountMetric).where(
            AccountMetric.account_id == account.id, AccountMetric.day == day
        )
    ).first()
    if row is None:
        row = AccountMetric(account_id=account.id, day=day)
        db.add(row)

    row.views = metrics.get("views", 0)
    row.likes = metrics.get("likes", 0)
    row.replies = metrics.get("replies", 0)
    row.reposts = metrics.get("reposts", 0)
    row.quotes = metrics.get("quotes", 0)
    row.clicks = metrics.get("clicks", 0)
    row.followers_count = metrics.get("followers_count", row.followers_count)
    row.collected_at = now

    db.flush()
    return {"day": day, **metrics}


# ---------------------------------------------------------------- Агрегации


def engagement(post: Post) -> int:
    return post.likes + post.replies + post.reposts + post.quotes + post.shares


def engagement_rate(post: Post) -> float:
    return round(engagement(post) / post.views * 100, 2) if post.views else 0.0


def top_posts(db: Session, account_id: int, days: int = 30, limit: int = 10) -> list[Post]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts = list(
        db.scalars(
            select(Post)
            .where(
                Post.account_id == account_id,
                Post.published_at >= cutoff,
                Post.is_reply.is_(False),
            )
            .order_by(Post.views.desc())
            .limit(200)
        )
    )
    return sorted(posts, key=engagement, reverse=True)[:limit]


def account_timeseries(db: Session, account_id: int, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.scalars(
        select(AccountMetric)
        .where(AccountMetric.account_id == account_id, AccountMetric.day >= cutoff)
        .order_by(AccountMetric.day)
    )
    return [
        {
            "day": row.day,
            "views": row.views,
            "likes": row.likes,
            "replies": row.replies,
            "reposts": row.reposts,
            "quotes": row.quotes,
            "followers": row.followers_count,
        }
        for row in rows
    ]


def summary(db: Session, account_id: int, days: int = 30) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts = list(
        db.scalars(
            select(Post).where(Post.account_id == account_id, Post.published_at >= cutoff)
        )
    )
    prev_cutoff = cutoff - timedelta(days=days)
    prev_posts = list(
        db.scalars(
            select(Post).where(
                Post.account_id == account_id,
                Post.published_at >= prev_cutoff,
                Post.published_at < cutoff,
            )
        )
    )

    def totals(items: list[Post]) -> dict:
        views = sum(p.views for p in items)
        eng = sum(engagement(p) for p in items)
        return {
            "posts": len(items),
            "views": views,
            "engagement": eng,
            "er": round(eng / views * 100, 2) if views else 0.0,
        }

    current = totals(posts)
    previous = totals(prev_posts)

    def delta(key: str) -> float:
        before = previous[key]
        if not before:
            return 0.0
        return round((current[key] - before) / before * 100, 1)

    followers = db.scalars(
        select(AccountMetric.followers_count)
        .where(AccountMetric.account_id == account_id)
        .order_by(AccountMetric.day.desc())
        .limit(1)
    ).first()

    return {
        "current": current,
        "previous": previous,
        "delta": {key: delta(key) for key in ("posts", "views", "engagement")},
        "followers": followers or 0,
    }


def topic_performance(db: Session, account_id: int, days: int = 60) -> list[dict]:
    """Какие рубрики реально работают — по опубликованным из очереди постам."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            Post.topic,
            func.count(Post.id),
            func.sum(Post.views),
            func.sum(Post.likes + Post.replies + Post.reposts + Post.quotes + Post.shares),
        )
        .where(
            Post.account_id == account_id,
            Post.published_at >= cutoff,
            Post.topic != "",
            Post.chain_index == 0,
        )
        .group_by(Post.topic)
    ).all()

    result = []
    for topic, count, views, eng in rows:
        views = views or 0
        eng = eng or 0
        result.append(
            {
                "topic": topic,
                "posts": count,
                "views": views,
                "engagement": eng,
                "avg_views": round(views / count) if count else 0,
                "er": round(eng / views * 100, 2) if views else 0.0,
            }
        )
    return sorted(result, key=lambda item: item["avg_views"], reverse=True)


def best_hours(db: Session, account_id: int, days: int = 60) -> list[dict]:
    """В какие часы публикации набирают больше всего просмотров."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts = db.scalars(
        select(Post).where(
            Post.account_id == account_id,
            Post.published_at >= cutoff,
            Post.is_reply.is_(False),
        )
    )
    buckets: dict[int, list[int]] = defaultdict(list)
    for post in posts:
        published = _aware(post.published_at)
        if published is None:
            continue
        buckets[published.hour].append(post.views)

    return sorted(
        (
            {
                "hour": hour,
                "posts": len(views),
                "avg_views": round(sum(views) / len(views)) if views else 0,
            }
            for hour, views in buckets.items()
        ),
        key=lambda item: item["avg_views"],
        reverse=True,
    )


def llm_spend(db: Session, account_id: int, days: int = 30) -> dict:
    """Во что обошлась модель для этого аккаунта за период.

    Без этого нельзя ни построить тариф, ни заметить аккаунт, который
    внезапно стал стоить втрое дороже остальных.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            LlmUsage.job,
            func.count(LlmUsage.id),
            func.sum(LlmUsage.prompt_tokens),
            func.sum(LlmUsage.completion_tokens),
            func.sum(LlmUsage.cost_usd),
        )
        .where(LlmUsage.account_id == account_id, LlmUsage.created_at >= cutoff)
        .group_by(LlmUsage.job)
    ).all()

    # Округляем до шестого знака: один вызов стоит доли цента, при округлении
    # до четвёртого мелкие задачи схлопывались бы в ноль.
    by_job = [
        {
            "job": job,
            "calls": calls,
            "tokens": int((prompt or 0) + (completion or 0)),
            "cost_usd": round(cost or 0.0, 6),
        }
        for job, calls, prompt, completion, cost in rows
    ]
    by_job.sort(key=lambda item: item["cost_usd"], reverse=True)

    total_cost = round(sum(item["cost_usd"] for item in by_job), 6)
    return {
        "days": days,
        "by_job": by_job,
        "calls": sum(item["calls"] for item in by_job),
        "tokens": sum(item["tokens"] for item in by_job),
        "cost_usd": total_cost,
        # Прикидка на месяц вперёд по текущему темпу
        "monthly_usd": round(total_cost / days * 30, 2) if days else 0.0,
    }
