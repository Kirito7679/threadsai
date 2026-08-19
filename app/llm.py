"""Клиент DeepSeek (API совместим с OpenAI Chat Completions)."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class Usage:
    """Расход токенов последнего вызова и его стоимость."""

    __slots__ = ("prompt_tokens", "completion_tokens", "cached_tokens", "model")

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        model: str = "",
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens
        self.model = model

    @classmethod
    def from_payload(cls, payload: dict, model: str) -> "Usage":
        raw = payload.get("usage") or {}
        # DeepSeek разделяет попадание и промах по кешу промпта — они стоят по-разному
        cached = int(raw.get("prompt_cache_hit_tokens", 0) or 0)
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(raw.get("completion_tokens", 0) or 0),
            cached_tokens=cached,
            model=model,
        )

    @property
    def cost_usd(self) -> float:
        miss = max(0, self.prompt_tokens - self.cached_tokens)
        cost = (
            miss * settings.deepseek_price_input
            + self.cached_tokens * settings.deepseek_price_cached
            + self.completion_tokens * settings.deepseek_price_output
        ) / 1_000_000
        return round(cost, 6)


def record_usage(db, account_id: int | None, job: str, usage: "Usage | None") -> None:
    """Пишет расход в журнал. Ошибка учёта не должна ронять генерацию."""
    if usage is None or not (usage.prompt_tokens or usage.completion_tokens):
        return
    try:
        from app.models import LlmUsage

        db.add(
            LlmUsage(
                account_id=account_id,
                job=job,
                model=usage.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_tokens,
                cost_usd=usage.cost_usd,
            )
        )
    except Exception as exc:  # noqa: BLE001 - учёт не критичен для основной работы
        log.warning("Расход токенов не записан: %s", exc)


def _extract_json(raw: str) -> Any:
    """DeepSeek иногда оборачивает JSON в ```json ... ``` — вытаскиваем."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Последняя попытка: найти первый сбалансированный объект или массив
        for opener, closer in (("{", "}"), ("[", "]")):
            start = raw.find(opener)
            end = raw.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    continue
        raise LLMError(f"Модель вернула не JSON: {raw[:400]}")


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ):
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self.model = model or settings.deepseek_model
        self.timeout = timeout
        # Расход последнего успешного вызова — забирается через record_usage()
        self.last_usage: Usage | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.8,
        max_tokens: int = 4000,
        json_mode: bool = False,
        model: str | None = None,
        retries: int = 2,
    ) -> str:
        if not self.is_configured:
            raise LLMError("DEEPSEEK_API_KEY не задан")

        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )
                if response.status_code >= 400:
                    detail = response.text[:500]
                    if response.status_code in (429, 500, 502, 503) and attempt < retries:
                        wait = 3 * (attempt + 1)
                        log.warning("DeepSeek %s, повтор через %sс", response.status_code, wait)
                        time.sleep(wait)
                        continue
                    raise LLMError(f"DeepSeek {response.status_code}: {detail}")
                payload = response.json()
                self.last_usage = Usage.from_payload(payload, body["model"])
                return payload["choices"][0]["message"]["content"] or ""
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise LLMError(f"Сеть недоступна: {exc}") from exc

        raise LLMError(f"DeepSeek не ответил: {last_error}")

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.7,
        max_tokens: int = 4000,
        model: str | None = None,
    ) -> Any:
        """Запрос со строгим JSON-ответом."""
        raw = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            model=model,
        )
        return _extract_json(raw)


def get_llm() -> DeepSeekClient:
    return DeepSeekClient()
