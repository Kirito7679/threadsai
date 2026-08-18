"""Шифрование токенов и подписанные сессионные cookie."""
import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 дней


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


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate, settings.dashboard_password)


def new_state_token() -> str:
    return secrets.token_urlsafe(24)
