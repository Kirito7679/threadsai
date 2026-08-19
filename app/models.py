from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    """Подключённый аккаунт Threads."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    threads_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255), default="")
    profile_picture_url: Mapped[str] = mapped_column(Text, default="")
    access_token_enc: Mapped[str] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Здоровье подключения. Пока status="ok", фоновые задачи работают с аккаунтом.
    # needs_reauth выставляется, когда Threads отвечает ошибкой авторизации:
    # иначе publish_due молча падает каждые 2 минуты и никто об этом не знает.
    status: Mapped[str] = mapped_column(String(24), default="ok")  # ok | needs_reauth
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    settings: Mapped[list["AccountSetting"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class AccountSetting(Base):
    """Настройки в формате key/value: тон, рубрики, расписание, автопилот."""

    __tablename__ = "account_settings"
    __table_args__ = (UniqueConstraint("account_id", "key", name="uq_setting"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text, default="")

    account: Mapped[Account] = relationship(back_populates="settings")


class Keyword(Base):
    """Ключевое слово или тег, по которому мониторим популярные ветки."""

    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("account_id", "term", name="uq_keyword"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    term: Mapped[str] = mapped_column(String(255))
    search_mode: Mapped[str] = mapped_column(String(16), default="KEYWORD")  # KEYWORD | TAG
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchPost(Base):
    """Чужой публичный пост, найденный через keyword_search."""

    __tablename__ = "research_posts"
    __table_args__ = (
        UniqueConstraint("account_id", "threads_post_id", name="uq_research_post"),
        Index("ix_research_collected", "account_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    threads_post_id: Mapped[str] = mapped_column(String(64))
    keyword: Mapped[str] = mapped_column(String(255), default="")
    search_type: Mapped[str] = mapped_column(String(16), default="TOP")  # TOP | RECENT
    username: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str] = mapped_column(String(32), default="TEXT_POST")
    permalink: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_replies: Mapped[bool] = mapped_column(Boolean, default=False)
    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Заполняется LLM-классификатором
    analyzed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    topic: Mapped[str] = mapped_column(String(120), default="")
    hook_type: Mapped[str] = mapped_column(String(64), default="")
    format_type: Mapped[str] = mapped_column(String(64), default="")
    tone: Mapped[str] = mapped_column(String(64), default="")
    insight: Mapped[str] = mapped_column(Text, default="")
    relevance: Mapped[float] = mapped_column(Float, default=0.0)


class TrendReport(Base):
    """Сводка по трендам за окно наблюдения — результат работы LLM-аналитика."""

    __tablename__ = "trend_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    window_hours: Mapped[int] = mapped_column(Integer, default=72)
    posts_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class Draft(Base):
    """Черновик ветки: список частей плюс статус модерации."""

    __tablename__ = "drafts"
    __table_args__ = (Index("ix_draft_status", "account_id", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    # pending | approved | rejected | publishing | published | failed
    status: Mapped[str] = mapped_column(String(24), default="pending")
    parts_json: Mapped[str] = mapped_column(Text, default="[]")
    topic: Mapped[str] = mapped_column(String(120), default="")
    hook_type: Mapped[str] = mapped_column(String(64), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    source_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("trend_reports.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(64), default="")
    edited_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Post(Base):
    """Опубликованный пост нашего аккаунта."""

    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("account_id", "media_id", name="uq_post_media"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id", ondelete="SET NULL"))
    root_media_id: Mapped[str] = mapped_column(String(64), default="")
    chain_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    media_type: Mapped[str] = mapped_column(String(32), default="TEXT_POST")
    permalink: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(String(120), default="")
    is_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Последний снимок метрик — денормализация ради быстрых списков
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    metrics_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PostMetric(Base):
    """Временной ряд метрик поста."""

    __tablename__ = "post_metrics"
    __table_args__ = (Index("ix_metric_post_time", "post_id", "collected_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)


class AccountMetric(Base):
    """Дневной срез метрик аккаунта."""

    __tablename__ = "account_metrics"
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_account_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    followers_count: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRun(Base):
    """Журнал фоновых задач — в панели видно, что и когда отработало.

    Одна строка на аккаунт: кабинет не должен показывать юзернеймы и тексты
    ошибок соседей. account_id=None — сбой самой задачи, ещё до того как
    стало известно, по каким аккаунтам она пойдёт.
    """

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_account_time", "account_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    detail: Mapped[str] = mapped_column(Text, default="")


class LlmUsage(Base):
    """Расход токенов DeepSeek: во что обходится обслуживание аккаунта."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_usage_account_time", "account_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    # analyze | trends | generate | rewrite
    job: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class DeletionRequest(Base):
    """Заявка на удаление данных, пришедшая колбэком от Meta.

    Meta требует вернуть код подтверждения и адрес, где пользователь проверит
    ход удаления, — значит заявку нужно где-то хранить.
    """

    __tablename__ = "deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    threads_user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # pending | done | not_found
    status: Mapped[str] = mapped_column(String(16), default="pending")
    detail: Mapped[str] = mapped_column(Text, default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
