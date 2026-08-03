"""Strict, non-logging parser for encrypted Weibo OAuth credential envelopes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


WEIBO_OAUTH_CREDENTIAL_KEYS = frozenset(
    {
        "credential_kind",
        "access_token",
        "uid",
        "expires_at",
    }
)
WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS = 900


class WeiboOAuthCredentialError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "weibo_oauth_credential_invalid")
        super().__init__(self.code)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WeiboOAuthCredentialError("weibo_oauth_credential_invalid")
        result[key] = value
    return result


def is_weibo_oauth_credential_envelope(payload: str | bytes) -> bool:
    """Classify the exact envelope shape without conflating kind and expiry."""

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(payload, str):
        return False
    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (TypeError, json.JSONDecodeError, WeiboOAuthCredentialError):
        return False
    return bool(
        isinstance(parsed, Mapping)
        and set(parsed) == WEIBO_OAUTH_CREDENTIAL_KEYS
        and parsed.get("credential_kind") == "weibo_oauth"
    )


def _utc_expiry(value: Any) -> datetime:
    """Parse and normalize an expiry without making a freshness decision."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise WeiboOAuthCredentialError("weibo_oauth_credential_expiry_invalid")
    token = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_expiry_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_expiry_invalid")
    return parsed.astimezone(timezone.utc)


def _require_fresh_expiry(
    expiry: datetime,
    *,
    now: datetime | None = None,
    min_remaining_seconds: int = WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS,
) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_expiry_invalid")
    current_utc = current.astimezone(timezone.utc)
    if expiry <= current_utc:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_expired")
    if expiry < current_utc + timedelta(seconds=min_remaining_seconds):
        raise WeiboOAuthCredentialError("weibo_oauth_credential_expiring_soon")


def _validate_weibo_oauth_envelope(value: Any) -> tuple[dict[str, Any], datetime]:
    """Validate exact shape and field syntax, independent of token freshness."""

    if not isinstance(value, Mapping) or set(value) != WEIBO_OAUTH_CREDENTIAL_KEYS:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_invalid")
    credential = dict(value)
    if credential.get("credential_kind") != "weibo_oauth":
        raise WeiboOAuthCredentialError("weibo_oauth_credential_kind_invalid")
    access_token = credential.get("access_token")
    if (
        not isinstance(access_token, str)
        or not 1 <= len(access_token) <= 4096
        or access_token != access_token.strip()
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in access_token)
    ):
        raise WeiboOAuthCredentialError("weibo_oauth_access_token_invalid")
    uid = credential.get("uid")
    if (
        not isinstance(uid, str)
        or not uid.isascii()
        or not uid.isdecimal()
        or not 1 <= len(uid) <= 20
        or int(uid) <= 0
    ):
        raise WeiboOAuthCredentialError("weibo_oauth_uid_invalid")
    expiry = _utc_expiry(credential.get("expires_at"))
    credential["expires_at"] = expiry.isoformat().replace("+00:00", "Z")
    return credential, expiry


def validate_weibo_oauth_credential(
    value: Any,
    *,
    now: datetime | None = None,
    min_remaining_seconds: int = WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS,
) -> dict[str, Any]:
    """Validate an exact decrypted envelope for new executable use."""

    credential, expiry = _validate_weibo_oauth_envelope(value)
    _require_fresh_expiry(
        expiry,
        now=now,
        min_remaining_seconds=min_remaining_seconds,
    )
    return credential


def parse_weibo_oauth_credential(
    payload: str | bytes,
    *,
    now: datetime | None = None,
    min_remaining_seconds: int = WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS,
) -> dict[str, Any]:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeiboOAuthCredentialError(
                "weibo_oauth_credential_invalid"
            ) from exc
    if not isinstance(payload, str):
        raise WeiboOAuthCredentialError("weibo_oauth_credential_invalid")
    try:
        parsed = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_invalid"
        ) from exc
    return validate_weibo_oauth_credential(
        parsed,
        now=now,
        min_remaining_seconds=min_remaining_seconds,
    )


def parse_weibo_oauth_credential_for_identity(
    payload: str | bytes,
) -> dict[str, Any]:
    """Parse a stored envelope solely to recover its stable remote identity.

    This deliberately validates duplicate keys, exact shape, field syntax and
    expiry formatting while ignoring whether the old token is still fresh.
    The result must never be used as an executable credential.
    """

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WeiboOAuthCredentialError(
                "weibo_oauth_credential_invalid"
            ) from exc
    if not isinstance(payload, str):
        raise WeiboOAuthCredentialError("weibo_oauth_credential_invalid")
    try:
        parsed = json.loads(payload, object_pairs_hook=_object_without_duplicate_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_invalid"
        ) from exc
    credential, _expiry = _validate_weibo_oauth_envelope(parsed)
    return credential


def normalize_weibo_oauth_credential(payload: str) -> str:
    credential = parse_weibo_oauth_credential(payload)
    return json.dumps(
        credential,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
