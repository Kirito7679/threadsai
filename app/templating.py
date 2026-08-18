"""Общий объект шаблонов и вспомогательные фильтры Jinja."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

LOCAL_TZ = ZoneInfo(settings.timezone)


def to_local(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).strftime(fmt)


def humanize_delta(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = abs(seconds)
        prefix, suffix = "через ", ""
    else:
        prefix, suffix = "", " назад"
    if seconds < 60:
        core = "меньше минуты"
    elif seconds < 3600:
        core = f"{seconds // 60} мин"
    elif seconds < 86400:
        core = f"{seconds // 3600} ч"
    else:
        core = f"{seconds // 86400} дн"
    return f"{prefix}{core}{suffix}"


def from_json(value: str):
    try:
        return json.loads(value or "[]")
    except json.JSONDecodeError:
        return []


def compact(value: int | float | None) -> str:
    if value is None:
        return "0"
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


templates.env.filters["local"] = to_local
templates.env.filters["ago"] = humanize_delta
templates.env.filters["fromjson"] = from_json
templates.env.filters["compact"] = compact
templates.env.globals["tz_name"] = settings.timezone
templates.env.globals["app_name"] = "Leilath Connector"
