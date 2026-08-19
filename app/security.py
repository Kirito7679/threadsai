"""Шифрование токенов, подписанные cookie и защита входа по паролю."""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

log = logging.getLogger(__name__)

_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 дней

# Пароли из примеров конфигурации: с ними вход не работает вообще
_UNSAFE_PASSWORDS = {"", "admin", "change-me-please", "password", "123456"}


def _fernet() -> Fernet:
    """Ключ Fernet детерминированно выводится из SECRET_KEY."""
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # ключ поменяли -> токен не расшифровать
        raise ValueError("Не удалось расшифровать токен: SECRET_KEY изменился") from exc


_serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")


def make_session_cookie(payload: dict) -> str:
    return _serializer.dumps(payload)


def read_session_cookie(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=_SESSION_MAX_AGE)
    except (BadSignature, Exception):  # noqa: B014 - истёкшая или битая подпись
        return None


def password_login_available() -> bool:
    """Разрешён ли запасной вход по паролю.

    Выключается флагом ENABLE_PASSWORD_LOGIN и не работает с паролем из
    примера конфигурации: иначе на публичном сервисе это готовый чёрный ход
    в кабинет первого пользователя.
    """
    if not settings.enable_password_login:
        return False
    return settings.dashboard_password.strip().lower() not in _UNSAFE_PASSWORDS


def check_password(candidate: str) -> bool:
    if not password_login_available():
        return False
    # Сравниваем байты, а не строки: compare_digest на строках с не-ASCII
    # бросает TypeError, и любой пароль с кириллицей ронял вход в 500-ю
    return hmac.compare_digest(
        candidate.encode("utf-8"), settings.dashboard_password.encode("utf-8")
    )


# ------------------------------------------------------- Ограничение перебора

_MAX_ATTEMPTS = 5
_WINDOW_SEC = 15 * 60
_attempts: dict[str, list[float]] = defaultdict(list)


def _prune(key: str, now: float) -> list[float]:
    fresh = [t for t in _attempts[key] if now - t < _WINDOW_SEC]
    _attempts[key] = fresh
    return fresh


def login_locked(key: str) -> int:
    """Сколько секунд осталось до конца блокировки. 0 — вход разрешён."""
    now = time.time()
    fresh = _prune(key, now)
    if len(fresh) < _MAX_ATTEMPTS:
        return 0
    return int(_WINDOW_SEC - (now - min(fresh))) + 1


def register_failed_login(key: str) -> None:
    now = time.time()
    _prune(key, now)
    _attempts[key].append(now)
    if len(_attempts[key]) >= _MAX_ATTEMPTS:
        log.warning("Вход по паролю заблокирован для %s: слишком много попыток", key)


def reset_failed_logins(key: str) -> None:
    _attempts.pop(key, None)


# --------------------------------------------- Подписанные запросы от Meta


def parse_signed_request(signed_request: str) -> dict | None:
    """Разбирает signed_request от Meta, проверяя подпись секретом приложения.

    Формат: <base64url подпись>.<base64url полезная нагрузка>, подпись —
    HMAC-SHA256 от строки нагрузки. Без проверки подписи колбэк удаления
    данных мог бы дёрнуть кто угодно.
    """
    if not signed_request or "." not in signed_request:
        return None
    secret = settings.threads_app_secret
    if not secret:
        log.error("THREADS_APP_SECRET не задан — подпись Meta проверить нечем")
        return None

    encoded_sig, _, payload = signed_request.partition(".")

    def _b64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    try:
        actual = _b64(encoded_sig)
        data = json.loads(_b64(payload))
    except (ValueError, TypeError) as exc:
        log.warning("signed_request не разобран: %s", exc)
        return None

    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, actual):
        log.warning("signed_request с неверной подписью отклонён")
        return None

    if str(data.get("algorithm", "")).upper() != "HMAC-SHA256":
        log.warning("signed_request с неожиданным алгоритмом: %s", data.get("algorithm"))
        return None
    return data


def new_state_token() -> str:
    return secrets.token_urlsafe(24)
