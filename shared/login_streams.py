"""Durable browser-login request transport contract."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit
import uuid

from shared.platform_ids import PLATFORM_IDS


LOGIN_REQUEST_STREAM_KEY = "login_requests"
LOGIN_REQUEST_GROUP_NAME = "login-workers"
LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX = "login-request:"
LOGIN_REQUEST_STREAM_FIELDS = frozenset(
    {"session_id", "platform", "login_url"}
)


@dataclass(frozen=True)
class LoginRequestStreamBinding:
    stream_key: str
    group_name: str


LOGIN_REQUEST_STREAM_BINDING = LoginRequestStreamBinding(
    stream_key=LOGIN_REQUEST_STREAM_KEY,
    group_name=LOGIN_REQUEST_GROUP_NAME,
)
_BINDINGS_BY_STREAM = MappingProxyType(
    {LOGIN_REQUEST_STREAM_KEY: LOGIN_REQUEST_STREAM_BINDING}
)


def login_request_stream_binding_for_key(
    stream_key: str,
) -> LoginRequestStreamBinding | None:
    return _BINDINGS_BY_STREAM.get(str(stream_key or "").strip())


def is_login_request_stream(stream_key: str) -> bool:
    return login_request_stream_binding_for_key(stream_key) is not None


def validate_login_request_stream_message(message: dict) -> None:
    if not isinstance(message, dict) or set(message) != LOGIN_REQUEST_STREAM_FIELDS:
        raise ValueError("login_request_stream_message_contract_invalid")
    session_id = str(message.get("session_id") or "").strip()
    try:
        parsed_session_id = uuid.UUID(session_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("login_request_session_id_invalid") from exc
    if str(parsed_session_id) != session_id.casefold():
        raise ValueError("login_request_session_id_invalid")
    platform = str(message.get("platform") or "").strip().casefold()
    if platform not in PLATFORM_IDS:
        raise ValueError("login_request_platform_invalid")
    login_url = str(message.get("login_url") or "").strip()
    if not login_url or len(login_url) > 512:
        raise ValueError("login_request_url_invalid")
    try:
        parsed_url = urlsplit(login_url)
        port = parsed_url.port
    except (TypeError, ValueError) as exc:
        raise ValueError("login_request_url_invalid") from exc
    if (
        parsed_url.scheme.casefold() not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("login_request_url_invalid")


__all__ = (
    "LOGIN_REQUEST_GROUP_NAME",
    "LOGIN_REQUEST_OUTBOX_DEDUP_PREFIX",
    "LOGIN_REQUEST_STREAM_BINDING",
    "LOGIN_REQUEST_STREAM_FIELDS",
    "LOGIN_REQUEST_STREAM_KEY",
    "LoginRequestStreamBinding",
    "is_login_request_stream",
    "login_request_stream_binding_for_key",
    "validate_login_request_stream_message",
)
