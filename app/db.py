import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    # Railway отдаёт postgres://; SQLAlchemy 2 требует явный драйвер
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite:///./"):
        os.makedirs(os.path.dirname(url.replace("sqlite:///", "")) or ".", exist_ok=True)
    return url


DATABASE_URL = _normalize_url(settings.database_url)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """Зависимость FastAPI."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _add_missing_columns() -> None:
    """Добавляет колонки, появившиеся в моделях после первого деплоя.

    create_all() создаёт только новые таблицы и молча игнорирует новые поля
    в существующих. Полноценных миграций тут нет, поэтому добавляем колонки
    сами: ADD COLUMN одинаково понимают и SQLite, и Postgres. Удаление и смена
    типа не поддерживаются — для этого нужен Alembic.
    """
    from app import models

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in models.Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # только что создана create_all(), она уже полная
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
            # NOT NULL без значения по умолчанию на непустой таблице не пройдёт,
            # поэтому новые колонки добавляются как nullable и заполняются кодом.
            try:
                with engine.begin() as connection:
                    connection.execute(text(ddl))
                log.info("Миграция: %s.%s добавлена", table.name, column.name)
            except SQLAlchemyError as exc:
                log.error("Миграция %s.%s не удалась: %s", table.name, column.name, exc)


def _backfill_defaults() -> None:
    """Проставляет значения новым колонкам у строк, созданных до миграции."""
    statements = (
        "UPDATE accounts SET status = 'ok' WHERE status IS NULL",
        "UPDATE accounts SET consecutive_errors = 0 WHERE consecutive_errors IS NULL",
        "UPDATE accounts SET last_error = '' WHERE last_error IS NULL",
    )
    for statement in statements:
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except SQLAlchemyError as exc:
            log.warning("Заполнение по умолчанию пропущено: %s", exc)


def init_db() -> None:
    from app import models  # noqa: F401 - регистрация мапперов

    models.Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _backfill_defaults()
