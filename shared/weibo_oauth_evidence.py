"""Exact, non-secret envelope for Weibo OAuth calibration evidence."""

from __future__ import annotations

from typing import Any, Mapping


WEIBO_OAUTH_CALIBRATION_RESULT_KEYS = frozenset(
    {
        "identity",
        "calibration_scope",
        "requires_manual_identity_review",
        "account_status_target",
        "oauth_capabilities",
    }
)
WEIBO_OAUTH_IDENTITY_KEYS = frozenset(
    {"verified", "method", "uid"}
)
SECRET_MATERIAL_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "oauth_token",
    }
)


class WeiboOAuthCalibrationEnvelopeError(ValueError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def contains_secret_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key or "").strip().casefold()
            if normalized in SECRET_MATERIAL_KEYS:
                return True
            if contains_secret_material(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_secret_material(item) for item in value)
    return False


def validate_weibo_oauth_calibration_envelope(
    value: Any,
    *,
    expected_uid: str | None = None,
) -> dict[str, Any]:
    """Validate the exact Core/Worker hand-off before action grants."""

    if (
        not isinstance(value, Mapping)
        or set(value) != WEIBO_OAUTH_CALIBRATION_RESULT_KEYS
        or contains_secret_material(value)
    ):
        raise WeiboOAuthCalibrationEnvelopeError(
            "weibo_oauth_capability_contract_mismatch"
        )
    result = dict(value)
    identity = result.get("identity")
    if (
        not isinstance(identity, Mapping)
        or set(identity) != WEIBO_OAUTH_IDENTITY_KEYS
        or identity.get("verified") is not True
        or identity.get("method") != "weibo_account_get_uid"
    ):
        raise WeiboOAuthCalibrationEnvelopeError(
            "weibo_oauth_identity_verification_required"
        )
    uid = identity.get("uid")
    if (
        not isinstance(uid, str)
        or not uid.isascii()
        or not uid.isdecimal()
        or not 1 <= len(uid) <= 20
        or int(uid) <= 0
        or (
            expected_uid is not None
            and uid != str(expected_uid)
        )
        or result.get("calibration_scope")
        != "oauth_identity_and_capabilities"
        or result.get("requires_manual_identity_review") is not False
        or result.get("account_status_target") != "ready"
    ):
        raise WeiboOAuthCalibrationEnvelopeError(
            "weibo_oauth_identity_verification_required"
        )
    if not isinstance(result.get("oauth_capabilities"), Mapping):
        raise WeiboOAuthCalibrationEnvelopeError(
            "weibo_oauth_capability_contract_mismatch"
        )
    result["identity"] = dict(identity)
    result["oauth_capabilities"] = dict(result["oauth_capabilities"])
    return result


__all__ = [
    "SECRET_MATERIAL_KEYS",
    "WEIBO_OAUTH_CALIBRATION_RESULT_KEYS",
    "WEIBO_OAUTH_IDENTITY_KEYS",
    "WeiboOAuthCalibrationEnvelopeError",
    "contains_secret_material",
    "validate_weibo_oauth_calibration_envelope",
]
