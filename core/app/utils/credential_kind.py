"""Non-secret account credential classification helpers."""

from __future__ import annotations

import json

from app.platforms import get_platform
from app.utils.cookies import parse_cookie_payload, validate_required_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.weibo_oauth_credential import (
    is_weibo_oauth_credential_envelope,
    parse_weibo_oauth_credential,
)


def decrypt_weibo_oauth_credential(encrypted_credential) -> dict:
    """Decrypt and strictly validate one Weibo OAuth envelope."""

    if isinstance(encrypted_credential, memoryview):
        encrypted_credential = encrypted_credential.tobytes()
    if not encrypted_credential:
        raise ValueError("weibo_oauth_credential_required")
    decrypted = cookie_vault.decrypt(
        encrypted_credential,
        aad=CREDENTIAL_AAD,
    )
    return parse_weibo_oauth_credential(decrypted)


def account_credential_kind(platform: str, encrypted_credential) -> str:
    """Return ``weibo_oauth``/``browser_session`` without exposing a secret."""

    if not encrypted_credential:
        return "none"
    if isinstance(encrypted_credential, memoryview):
        encrypted_credential = encrypted_credential.tobytes()
    try:
        decrypted = cookie_vault.decrypt(
            encrypted_credential,
            aad=CREDENTIAL_AAD,
        )
    except Exception:
        return "invalid"

    if platform == "weibo":
        if is_weibo_oauth_credential_envelope(decrypted):
            return "weibo_oauth"
        try:
            parsed = json.loads(decrypted)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        # A damaged OAuth-like object must never silently fall back to
        # browser-cookie routing just because both formats are JSON.
        if isinstance(parsed, dict) and (
            "credential_kind" in parsed or "access_token" in parsed
        ):
            return "invalid"

    platform_cfg = get_platform(platform)
    if not platform_cfg:
        return "invalid"
    try:
        cookies = parse_cookie_payload(platform, decrypted)
        validate_required_cookies(
            cookies,
            platform_cfg.get("required_cookies", []),
        )
    except Exception:
        return "invalid"
    return "browser_session"
