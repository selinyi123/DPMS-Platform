"""Non-secret account credential classification helpers."""

from __future__ import annotations

import json

from app.platforms import get_platform
from app.utils.cookies import (
    parse_cookie_payload,
    validate_api_cookie_name_uniqueness,
    validate_required_cookies,
)
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.weibo_oauth_credential import (
    is_weibo_oauth_credential_envelope,
    parse_weibo_oauth_credential,
    parse_weibo_oauth_credential_for_identity,
)
from shared.douyin_device_contract import (
    is_douyin_device_credential,
    parse_douyin_device_credential,
)


def decrypt_weibo_oauth_credential(encrypted_credential) -> dict:
    """Decrypt and strictly validate one Weibo OAuth envelope."""

    if isinstance(encrypted_credential, memoryview):
        encrypted_credential = encrypted_credential.tobytes()
    if not encrypted_credential:
        raise ValueError("weibo_oauth_credential_required")
    decrypted = cookie_vault.decrypt_strict(
        encrypted_credential,
        aad=CREDENTIAL_AAD,
    )
    return parse_weibo_oauth_credential(decrypted)


def decrypt_douyin_device_credential(encrypted_credential) -> dict:
    """Decrypt one purpose-bound, non-secret device identity envelope."""

    if isinstance(encrypted_credential, memoryview):
        encrypted_credential = encrypted_credential.tobytes()
    if not encrypted_credential:
        raise ValueError("douyin_device_credential_required")
    decrypted = cookie_vault.decrypt_strict(
        encrypted_credential,
        aad=CREDENTIAL_AAD,
    )
    return parse_douyin_device_credential(decrypted)


def account_remote_subject(platform: str, credential_plaintext: str) -> str | None:
    """Return a stable, non-secret remote identity for executable credentials.

    Browser session tokens themselves are never returned or hashed.  Only the
    account identifier already embedded in the normalized credential is used.
    Platforms whose current real-run contract cannot prove such an identifier
    return ``None`` and callers must fail closed when identity continuity is
    required.
    """

    normalized_platform = str(platform or "").strip().casefold()
    if normalized_platform == "weibo":
        try:
            credential = parse_weibo_oauth_credential_for_identity(
                credential_plaintext
            )
            uid = str(credential["uid"])
        except Exception:
            return None
        return f"weibo:{uid}"
    if normalized_platform == "bilibili":
        try:
            cookies = parse_cookie_payload(
                normalized_platform,
                credential_plaintext,
            )
            uid_values = [
                str(cookie.get("value") or "").strip()
                for cookie in cookies
                if cookie.get("name") == "DedeUserID"
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        # A Cookie header has no portable duplicate-name ordering contract.
        # Refuse to infer identity when a legacy or unnormalised credential
        # contains more than one remote subject, even when values happen to
        # match.
        if len(uid_values) != 1:
            return None
        uid = uid_values[0]
        if (
            not uid.isascii()
            or not uid.isdecimal()
            or not 1 <= len(uid) <= 20
            or int(uid) <= 0
        ):
            return None
        return f"bilibili:{uid}"
    if normalized_platform == "douyin":
        try:
            credential = parse_douyin_device_credential(credential_plaintext)
        except Exception:
            return None
        return (
            "douyin-device:"
            f"{credential['device_agent']['account_id_sha256']}"
        )
    return None


def account_credential_kind(platform: str, encrypted_credential) -> str:
    """Classify a purpose-bound executable credential without exposing it."""

    if not encrypted_credential:
        return "none"
    if isinstance(encrypted_credential, memoryview):
        encrypted_credential = encrypted_credential.tobytes()
    normalized_platform = str(platform or "").strip().casefold()
    if normalized_platform in {"weibo", "douyin"}:
        # OAuth credentials are executable bearer tokens. Unlike browser
        # sessions, they must never inherit the legacy no-AAD compatibility
        # fallback: an unbound ciphertext could otherwise be selected by Core
        # and then rejected by the strict Worker decryptor.
        try:
            strictly_bound = cookie_vault.decrypt_strict(
                encrypted_credential,
                aad=CREDENTIAL_AAD,
            )
        except Exception:
            strictly_bound = None
        if strictly_bound is not None and normalized_platform == "weibo":
            if is_weibo_oauth_credential_envelope(strictly_bound):
                return "weibo_oauth"
            try:
                strictly_parsed = json.loads(strictly_bound)
            except (TypeError, json.JSONDecodeError):
                strictly_parsed = None
            if isinstance(strictly_parsed, dict) and (
                "credential_kind" in strictly_parsed
                or "access_token" in strictly_parsed
            ):
                return "invalid"
        if strictly_bound is not None and normalized_platform == "douyin":
            if is_douyin_device_credential(strictly_bound):
                return "device_agent"
            try:
                strictly_parsed = json.loads(strictly_bound)
            except (TypeError, json.JSONDecodeError):
                strictly_parsed = None
            if isinstance(strictly_parsed, dict) and (
                "credential_kind" in strictly_parsed
                or "device_agent" in strictly_parsed
            ):
                return "invalid"

    try:
        # Browser sessions intentionally retain the legacy unbound decrypt
        # path so old cookie accounts can still be inspected and migrated.
        decrypted = cookie_vault.decrypt(
            encrypted_credential,
            aad=CREDENTIAL_AAD,
        )
    except Exception:
        return "invalid"

    if normalized_platform in {"weibo", "douyin"}:
        try:
            parsed = json.loads(decrypted)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        # A damaged OAuth-like object must never silently fall back to
        # browser-cookie routing just because both formats are JSON.
        if isinstance(parsed, dict) and "credential_kind" in parsed:
            return "invalid"
        if normalized_platform == "weibo" and isinstance(parsed, dict) and (
            "access_token" in parsed
        ):
            return "invalid"
        if normalized_platform == "douyin" and isinstance(parsed, dict) and (
            "device_agent" in parsed
        ):
            return "invalid"

    platform_cfg = get_platform(normalized_platform)
    if not platform_cfg:
        return "invalid"
    try:
        cookies = parse_cookie_payload(normalized_platform, decrypted)
        validate_required_cookies(
            cookies,
            platform_cfg.get("required_cookies", []),
        )
        validate_api_cookie_name_uniqueness(normalized_platform, cookies)
    except Exception:
        return "invalid"
    return "browser_session"
