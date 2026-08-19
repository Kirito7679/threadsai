"""Клиент официального Threads API (graph.threads.net).

Документация: https://developers.facebook.com/docs/threads
Все эндпоинты и параметры сверены с официальной докой.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.threads.net"
API_BASE = f"{GRAPH_HOST}/v1.0"
AUTH_URL = "https://threads.net/oauth/authorize"

# Лимиты платформы
MAX_POST_CHARS = 500
DAILY_PUBLISH_LIMIT = 250
# Дока рекомендует ждать ~30 сек перед публикацией контейнера с медиа.
# Для TEXT достаточно короткой паузы.
MEDIA_PROCESSING_WAIT_SEC = 30
TEXT_PROCESSING_WAIT_SEC = 2


class ThreadsAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: Any, endpoint: str = ""):
        self.status_code = status_code
        self.payload = payload
        self.endpoint = endpoint
        message = payload
        if isinstance(payload, dict):
            err = payload.get("error", {})
            message = err.get("message") or payload
        super().__init__(f"Threads API {status_code} на {endpoint}: {message}")

    @property
    def is_rate_limit(self) -> bool:
        if self.status_code == 429:
            return True
        if isinstance(self.payload, dict):
            code = self.payload.get("error", {}).get("code")
            return code in (4, 17, 32, 613)
        return False

    @property
    def is_auth_error(self) -> bool:
        if self.status_code in (401, 403):
            return True
        if isinstance(self.payload, dict):
            return self.payload.get("error", {}).get("code") == 190
        return False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ThreadsClient:
    """Тонкая обёртка над Graph API. Один экземпляр — один access token."""

    def __init__(self, access_token: str, user_id: str = "me", timeout: float = 30.0):
        self.access_token = access_token
        self.user_id = user_id or "me"
        self._timeout = timeout
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ HTTP

    @property
    def http(self) -> httpx.Client:
        """Одно соединение на весь жизненный цикл клиента.

        Сбор метрик — это десятки последовательных запросов подряд; создавать
        под каждый новый httpx.Client значит платить за TLS-хендшейк каждый раз.
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self._timeout,
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def __enter__(self) -> ThreadsClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        data: dict | None = None,
        retries: int = 2,
    ) -> dict:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        data = {k: v for k, v in (data or {}).items() if v is not None}
        params.setdefault("access_token", self.access_token)

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.http.request(method, url, params=params, data=data or None)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"raw": response.text}

                if response.status_code >= 400:
                    error = ThreadsAPIError(response.status_code, payload, path)
                    # Троттлинг и 5xx имеет смысл повторить
                    if (error.is_rate_limit or response.status_code >= 500) and attempt < retries:
                        sleep_for = 2 ** (attempt + 2)
                        log.warning("Threads API %s, повтор через %sс", response.status_code, sleep_for)
                        time.sleep(sleep_for)
                        last_error = error
                        continue
                    raise error
                return payload
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ThreadsAPIError(0, {"error": {"message": str(exc)}}, path) from exc

        raise last_error  # type: ignore[misc]

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict | None = None) -> dict:
        return self._request("POST", path, data=data)

    # ------------------------------------------------------------------ OAuth

    @staticmethod
    def authorization_url(state: str) -> str:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "client_id": settings.threads_app_id,
                "redirect_uri": settings.threads_redirect_uri,
                "scope": settings.scopes,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{AUTH_URL}?{query}"

    @staticmethod
    def exchange_code(code: str) -> dict:
        """Код авторизации -> короткоживущий токен. Код валиден 1 час и одноразовый."""
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{GRAPH_HOST}/oauth/access_token",
                data={
                    "client_id": settings.threads_app_id,
                    "client_secret": settings.threads_app_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.threads_redirect_uri,
                    "code": code,
                },
            )
        payload = response.json()
        if response.status_code >= 400:
            raise ThreadsAPIError(response.status_code, payload, "/oauth/access_token")
        return payload  # {"access_token": ..., "user_id": ...}

    @staticmethod
    def exchange_long_lived(short_token: str) -> dict:
        """Короткоживущий -> долгоживущий токен на 60 дней."""
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{GRAPH_HOST}/access_token",
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": settings.threads_app_secret,
                    "access_token": short_token,
                },
            )
        payload = response.json()
        if response.status_code >= 400:
            raise ThreadsAPIError(response.status_code, payload, "/access_token")
        return payload  # {"access_token": ..., "token_type": ..., "expires_in": 5183944}

    @staticmethod
    def refresh_long_lived(long_token: str) -> dict:
        """Продление на 60 дней. Токен должен быть старше 24 часов и не истёкшим."""
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{GRAPH_HOST}/refresh_access_token",
                params={"grant_type": "th_refresh_token", "access_token": long_token},
            )
        payload = response.json()
        if response.status_code >= 400:
            raise ThreadsAPIError(response.status_code, payload, "/refresh_access_token")
        return payload

    # ------------------------------------------------------------------ Профиль

    def get_profile(self) -> dict:
        return self._get(
            f"/{self.user_id}",
            {
                "fields": "id,username,name,threads_profile_picture_url,threads_biography",
            },
        )

    def get_publishing_limit(self) -> dict:
        """Сколько постов из суточной квоты уже израсходовано."""
        return self._get(
            f"/{self.user_id}/threads_publishing_limit",
            {"fields": "quota_usage,config,reply_quota_usage,reply_config"},
        )

    # ------------------------------------------------------------------ Публикация

    def create_container(
        self,
        text: str | None = None,
        media_type: str = "TEXT",
        image_url: str | None = None,
        video_url: str | None = None,
        reply_to_id: str | None = None,
        reply_control: str | None = None,
        topic_tag: str | None = None,
        link_attachment: str | None = None,
        alt_text: str | None = None,
        is_carousel_item: bool | None = None,
        children: list[str] | None = None,
    ) -> str:
        """POST /{user-id}/threads -> id медиаконтейнера."""
        data: dict[str, Any] = {"media_type": media_type}
        if text is not None:
            data["text"] = text
        if image_url:
            data["image_url"] = image_url
        if video_url:
            data["video_url"] = video_url
        if reply_to_id:
            data["reply_to_id"] = reply_to_id
        if reply_control:
            data["reply_control"] = reply_control
        if topic_tag:
            data["topic_tag"] = topic_tag
        if link_attachment:
            data["link_attachment"] = link_attachment
        if alt_text:
            data["alt_text"] = alt_text
        if is_carousel_item is not None:
            data["is_carousel_item"] = str(is_carousel_item).lower()
        if children:
            data["children"] = ",".join(children)

        payload = self._post(f"/{self.user_id}/threads", data)
        container_id = payload.get("id")
        if not container_id:
            raise ThreadsAPIError(0, payload, "create_container")
        return container_id

    def get_container_status(self, container_id: str) -> dict:
        """status: EXPIRED | ERROR | FINISHED | IN_PROGRESS | PUBLISHED"""
        return self._get(f"/{container_id}", {"fields": "status,error_message"})

    def wait_for_container(self, container_id: str, timeout_sec: int = 90, interval: int = 5) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            status = self.get_container_status(container_id)
            state = status.get("status")
            if state == "FINISHED":
                return
            if state in ("ERROR", "EXPIRED"):
                raise ThreadsAPIError(
                    0,
                    {"error": {"message": f"Контейнер {state}: {status.get('error_message', '')}"}},
                    "wait_for_container",
                )
            time.sleep(interval)
        # Таймаут не всегда фатален: FINISHED мог не успеть проставиться
        log.warning("Контейнер %s не дошёл до FINISHED за %sс, пробуем публиковать", container_id, timeout_sec)

    def publish_container(self, creation_id: str) -> str:
        """POST /{user-id}/threads_publish -> id опубликованного поста."""
        payload = self._post(f"/{self.user_id}/threads_publish", {"creation_id": creation_id})
        media_id = payload.get("id")
        if not media_id:
            raise ThreadsAPIError(0, payload, "publish_container")
        return media_id

    def publish_text_post(
        self,
        text: str,
        reply_to_id: str | None = None,
        reply_control: str | None = None,
        topic_tag: str | None = None,
        link_attachment: str | None = None,
    ) -> str:
        """Создать и опубликовать текстовый пост одним вызовом."""
        if len(text) > MAX_POST_CHARS:
            raise ValueError(f"Пост длиннее {MAX_POST_CHARS} символов: {len(text)}")
        container_id = self.create_container(
            text=text,
            media_type="TEXT",
            reply_to_id=reply_to_id,
            reply_control=reply_control,
            topic_tag=topic_tag,
            link_attachment=link_attachment,
        )
        time.sleep(TEXT_PROCESSING_WAIT_SEC)
        return self.publish_container(container_id)

    # ------------------------------------------------------------------ Свои посты

    POST_FIELDS = (
        "id,media_product_type,media_type,text,permalink,timestamp,username,"
        "is_quote_post,shortcode,children,has_replies,is_reply,reply_audience"
    )

    def get_own_posts(self, limit: int = 50, since: int | None = None, until: int | None = None) -> list[dict]:
        params = {"fields": self.POST_FIELDS, "limit": min(limit, 100)}
        if since:
            params["since"] = since
        if until:
            params["until"] = until

        collected: list[dict] = []
        path = f"/{self.user_id}/threads"
        while path and len(collected) < limit:
            payload = self._get(path, params) if not path.startswith("http") else self._get(path)
            collected.extend(payload.get("data", []))
            path = payload.get("paging", {}).get("next", "")
            params = {}
        return collected[:limit]

    def get_post(self, media_id: str) -> dict:
        return self._get(f"/{media_id}", {"fields": self.POST_FIELDS})

    def get_replies(self, media_id: str, top_level_only: bool = True) -> list[dict]:
        endpoint = "replies" if top_level_only else "conversation"
        payload = self._get(
            f"/{media_id}/{endpoint}",
            {
                "fields": "id,text,username,permalink,timestamp,has_replies,root_post,replied_to,is_reply,hide_status",
                "reverse": "false",
            },
        )
        return payload.get("data", [])

    def hide_reply(self, reply_id: str, hide: bool = True) -> dict:
        return self._post(f"/{reply_id}/manage_reply", {"hide": str(hide).lower()})

    # ------------------------------------------------------------------ Аналитика

    MEDIA_METRICS = "views,likes,replies,reposts,quotes,shares"
    USER_METRICS = "views,likes,replies,reposts,quotes,clicks,followers_count"

    @staticmethod
    def _flatten_insights(payload: dict) -> dict[str, int]:
        """Приводит ответ insights к плоскому словарю metric -> значение."""
        result: dict[str, int] = {}
        for item in payload.get("data", []):
            name = item.get("name")
            if not name:
                continue
            if "total_value" in item:
                result[name] = int(item["total_value"].get("value", 0) or 0)
            elif item.get("values"):
                # Временной ряд: берём последнее непустое значение
                values = [v.get("value", 0) or 0 for v in item["values"]]
                result[name] = int(values[-1]) if values else 0
        return result

    def get_post_insights(self, media_id: str) -> dict[str, int]:
        payload = self._get(f"/{media_id}/insights", {"metric": self.MEDIA_METRICS})
        return self._flatten_insights(payload)

    def get_account_insights(self, since: int | None = None, until: int | None = None) -> dict[str, int]:
        params: dict[str, Any] = {"metric": self.USER_METRICS}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        payload = self._get(f"/{self.user_id}/threads_insights", params)
        return self._flatten_insights(payload)

    def get_follower_demographics(self, breakdown: str = "country") -> dict:
        """breakdown: country | city | age | gender"""
        return self._get(
            f"/{self.user_id}/threads_insights",
            {"metric": "follower_demographics", "breakdown": breakdown},
        )

    # ------------------------------------------------------------------ Поиск

    SEARCH_FIELDS = "id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply"

    def keyword_search(
        self,
        query: str,
        search_type: str = "TOP",
        search_mode: str = "KEYWORD",
        limit: int = 25,
        since: int | None = None,
        until: int | None = None,
        media_type: str | None = None,
        author_username: str | None = None,
    ) -> list[dict]:
        """GET /keyword_search — публичный поиск.

        search_type=TOP возвращает самые популярные посты по запросу,
        RECENT — самые свежие. Лимит платформы: 2200 запросов за 24 часа.
        """
        params: dict[str, Any] = {
            "q": query,
            "search_type": search_type,
            "search_mode": search_mode,
            "fields": self.SEARCH_FIELDS,
            "limit": min(limit, 100),
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if media_type:
            params["media_type"] = media_type
        if author_username:
            params["author_username"] = author_username

        payload = self._get("/keyword_search", params)
        return payload.get("data", [])


def parse_timestamp(value: str | None) -> datetime | None:
    return _parse_ts(value)


def now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())
