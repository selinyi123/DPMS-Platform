"""Strict parsing for secret Weibo OAuth credentials and request IPs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.action_plan import WEIBO_ACTION_ORDER, WEIBO_RIP_ACTIONS
from app.config import settings
from app.utils.crypto import cookie_vault


CREDENTIAL_KEYS = frozenset(
    {"credential_kind", "access_token", "uid", "expires_at"}
)
WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS = 900
WEIBO_RIP_AAD = "dpms:weibo-rip:v1"
WEIBO_RIP_HMAC_CONTEXT = b"dpms:weibo-rip-hmac:v1"


class WeiboOAuthCredentialError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "weibo_oauth_credential_invalid")
        super().__init__(self.code)


def _json_object_without_duplicate_keys(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WeiboOAuthCredentialError(
                "weibo_oauth_credential_contract_mismatch"
            ) from exc
    if not isinstance(value, str):
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        )
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        ) from exc
    if encoded_length > 16_384:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        )

    def exact_object(pairs):
        output = {}
        for key, item in pairs:
            if key in output:
                raise WeiboOAuthCredentialError(
                    "weibo_oauth_credential_contract_mismatch"
                )
            output[key] = item
        return output

    try:
        parsed = json.loads(value, object_pairs_hook=exact_object)
    except WeiboOAuthCredentialError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        ) from exc
    if not isinstance(parsed, dict):
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        )
    return parsed


def _utc_timestamp(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WeiboOAuthCredentialError(code)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WeiboOAuthCredentialError(code) from exc
    if parsed.tzinfo is None:
        raise WeiboOAuthCredentialError(code)
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise WeiboOAuthCredentialError("weibo_oauth_clock_invalid")
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, repr=False)
class WeiboOAuthCredential:
    """Secret credential whose repr deliberately never contains the token."""

    access_token: str
    uid: str
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "WeiboOAuthCredential(credential_kind='weibo_oauth', "
            f"uid={self.uid!r}, expires_at={self.expires_at.isoformat()!r}, "
            "access_token=<redacted>)"
        )

    def require_fresh(
        self,
        *,
        now: datetime | None = None,
        min_remaining_seconds: int = 0,
    ) -> None:
        current = _utc_now(now)
        if self.expires_at <= current:
            raise WeiboOAuthCredentialError("weibo_oauth_credential_expired")
        if self.expires_at < current + timedelta(seconds=min_remaining_seconds):
            raise WeiboOAuthCredentialError(
                "weibo_oauth_credential_expiring_soon"
            )


def is_weibo_oauth_credential_envelope(value: Any) -> bool:
    """Identify the strict envelope shape without validating secret freshness."""

    try:
        payload = _json_object_without_duplicate_keys(value)
    except WeiboOAuthCredentialError:
        return False
    return set(payload) == CREDENTIAL_KEYS and payload.get("credential_kind") == "weibo_oauth"


def parse_weibo_oauth_credential(
    value: Any,
    *,
    expected_uid: str | None = None,
    now: datetime | None = None,
    min_remaining_seconds: int = WEIBO_OAUTH_MIN_REMAINING_TTL_SECONDS,
) -> WeiboOAuthCredential:
    payload = _json_object_without_duplicate_keys(value)
    if set(payload) != CREDENTIAL_KEYS:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_contract_mismatch"
        )
    if payload.get("credential_kind") != "weibo_oauth":
        raise WeiboOAuthCredentialError("weibo_oauth_credential_kind_invalid")
    token = payload.get("access_token")
    if (
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or len(token) > 4096
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)
    ):
        raise WeiboOAuthCredentialError("weibo_oauth_access_token_invalid")
    uid = payload.get("uid")
    if (
        not isinstance(uid, str)
        or not uid.isascii()
        or not uid.isdecimal()
        or not 1 <= len(uid) <= 20
        or int(uid) <= 0
    ):
        raise WeiboOAuthCredentialError("weibo_oauth_uid_invalid")
    if expected_uid is not None and uid != str(expected_uid):
        raise WeiboOAuthCredentialError("weibo_oauth_identity_binding_mismatch")
    credential = WeiboOAuthCredential(
        access_token=token,
        uid=uid,
        expires_at=_utc_timestamp(
            payload.get("expires_at"), code="weibo_oauth_expiry_invalid"
        ),
    )
    credential.require_fresh(
        now=now,
        min_remaining_seconds=min_remaining_seconds,
    )
    return credential


def validate_weibo_rip(value: Any, *, required: bool) -> str:
    """Return a canonical public IP without ever including it in errors."""

    raw = value if isinstance(value, str) else str(value or "")
    if not raw:
        if required:
            raise WeiboOAuthCredentialError("weibo_rip_required")
        return ""
    if not required:
        raise WeiboOAuthCredentialError("weibo_rip_not_applicable")
    if raw != raw.strip():
        raise WeiboOAuthCredentialError("weibo_rip_invalid")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise WeiboOAuthCredentialError("weibo_rip_invalid") from exc
    canonical = str(address)
    if not address.is_global:
        raise WeiboOAuthCredentialError("weibo_rip_not_public")
    if canonical != raw:
        raise WeiboOAuthCredentialError("weibo_rip_invalid")
    return canonical


def decrypt_weibo_rip(value: Any, *, required: bool) -> str:
    """Strictly decrypt the queue handoff and return only a canonical public IP."""

    raw = value if isinstance(value, str) else ""
    if not raw:
        if required:
            raise WeiboOAuthCredentialError("weibo_rip_encrypted_required")
        return ""
    if not required:
        raise WeiboOAuthCredentialError("weibo_rip_encrypted_not_applicable")
    if raw != raw.strip() or len(raw) > 256:
        raise WeiboOAuthCredentialError("weibo_rip_encrypted_invalid")
    try:
        encoded = raw.encode("ascii", errors="strict")
        blob = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(blob).decode("ascii") != raw:
            raise ValueError("non_canonical_base64")
        plaintext = cookie_vault.decrypt_strict(blob, aad=WEIBO_RIP_AAD)
    except Exception as exc:
        raise WeiboOAuthCredentialError("weibo_rip_decryption_failed") from exc
    return validate_weibo_rip(plaintext, required=True)


def weibo_rip_hmac(value: Any) -> str:
    """Return a purpose-bound keyed digest that cannot be offline-enumerated."""

    raw = str(value or "")
    if not raw:
        return ""
    canonical_ip = validate_weibo_rip(raw, required=True)
    try:
        master = base64.b64decode(
            settings.encryption_key.encode("ascii"), validate=True
        )
    except Exception as exc:
        raise WeiboOAuthCredentialError("weibo_rip_hmac_key_invalid") from exc
    if len(master) != 32:
        raise WeiboOAuthCredentialError("weibo_rip_hmac_key_invalid")
    derived_key = hmac.new(
        master, WEIBO_RIP_HMAC_CONTEXT, hashlib.sha256
    ).digest()
    return hmac.new(
        derived_key, canonical_ip.encode("ascii"), hashlib.sha256
    ).hexdigest()


def weibo_rip_required(required_actions) -> bool:
    selected = set(required_actions or ())
    if selected - set(WEIBO_ACTION_ORDER):
        raise WeiboOAuthCredentialError("weibo_oauth_action_contract_invalid")
    return bool(selected & WEIBO_RIP_ACTIONS)
