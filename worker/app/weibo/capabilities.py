"""Fail-closed validation for admin-attested Weibo OAuth capabilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from app.action_plan import (
    WEIBO_ACTION_CAPABILITY_REQUIREMENTS,
    WEIBO_ACTION_ORDER,
    WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
)


ATTESTATION_KEYS = frozenset(
    {
        "contract_version",
        "calibration_id",
        "account_id",
        "execution_revision",
        "credential_kind",
        "identity_verified",
        "app_review_status",
        "client_type",
        "verified_at",
        "evidence_source",
        "attested_by",
        "attested_at",
        "actions",
    }
)
ACTION_ATTESTATION_KEYS = frozenset({"endpoint", "permission", "granted"})
OPERATOR_ENVELOPE_KEYS = frozenset({"operator_attestation"})
OPERATOR_ATTESTATION_KEYS = frozenset(
    {
        "version",
        "attested_by",
        "attested_at",
        "app_review_status",
        "client_type",
        "granted_actions",
    }
)
APP_REVIEW_STATUSES = frozenset({"approved", "test_only", "unknown"})
CLIENT_TYPES = frozenset({"weibo", "other"})
EVIDENCE_SOURCE = "operator_attested_app_capabilities"
CREDENTIAL_KIND = "weibo_oauth"
MAX_EVIDENCE_AGE = timedelta(hours=24)


class WeiboOAuthCapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "weibo_oauth_capability_invalid")
        super().__init__(self.code)


def _positive_int(value: Any, *, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise WeiboOAuthCapabilityError(code)
    return value


def _required_actor(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_actor_invalid")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WeiboOAuthCapabilityError(
            "weibo_oauth_attestation_actor_invalid"
        ) from exc
    if encoded_length > 128:
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_actor_invalid")
    return value


def _calibration_id(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise WeiboOAuthCapabilityError("weibo_oauth_calibration_binding_invalid")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise WeiboOAuthCapabilityError(
            "weibo_oauth_calibration_binding_invalid"
        ) from exc
    if str(parsed) != value.lower():
        raise WeiboOAuthCapabilityError("weibo_oauth_calibration_binding_invalid")
    return value.lower()


def _utc_timestamp(value: Any, *, code: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WeiboOAuthCapabilityError(code)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WeiboOAuthCapabilityError(code) from exc
    if parsed.tzinfo is None:
        raise WeiboOAuthCapabilityError(code)
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise WeiboOAuthCapabilityError("weibo_oauth_clock_invalid")
    return current.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_grants(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != set(WEIBO_ACTION_ORDER):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    if any(type(granted) is not bool for granted in value.values()):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    return {action: value[action] for action in WEIBO_ACTION_ORDER}


def validate_weibo_operator_attestation(
    value: Any,
    *,
    now: datetime | None = None,
    max_age: timedelta = MAX_EVIDENCE_AGE,
) -> dict[str, Any]:
    """Validate the exact server-created admin confirmation staged in DB."""

    if not isinstance(value, Mapping) or set(value) != OPERATOR_ENVELOPE_KEYS:
        raise WeiboOAuthCapabilityError("weibo_oauth_operator_attestation_required")
    raw = value.get("operator_attestation")
    if not isinstance(raw, Mapping) or set(raw) != OPERATOR_ATTESTATION_KEYS:
        raise WeiboOAuthCapabilityError("weibo_oauth_operator_attestation_invalid")
    attestation = dict(raw)
    if type(attestation.get("version")) is not int or attestation["version"] != 1:
        raise WeiboOAuthCapabilityError("weibo_oauth_operator_attestation_invalid")
    attestation["attested_by"] = _required_actor(attestation.get("attested_by"))
    if attestation.get("app_review_status") not in APP_REVIEW_STATUSES:
        raise WeiboOAuthCapabilityError("weibo_oauth_app_review_status_invalid")
    if attestation.get("client_type") not in CLIENT_TYPES:
        raise WeiboOAuthCapabilityError("weibo_oauth_client_type_invalid")
    attestation["granted_actions"] = _validated_grants(
        attestation.get("granted_actions")
    )
    current = _utc_now(now)
    attested_at = _utc_timestamp(
        attestation.get("attested_at"),
        code="weibo_oauth_attested_at_invalid",
    )
    if attested_at > current:
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_from_future")
    if current - attested_at > max_age:
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_stale")
    return attestation


def build_weibo_oauth_capability_attestation(
    *,
    calibration_id: str,
    account_id: int,
    execution_revision: int,
    operator_attestation: Mapping[str, Any],
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Bind official identity verification to a separate admin attestation.

    This function does not accept a token and cannot turn credential-import
    metadata into capability evidence. ``operator_attestation`` must already
    have been persisted by Core's admin-confirmed endpoint.
    """

    verification_time = _utc_now(verified_at)
    staged = validate_weibo_operator_attestation(
        {"operator_attestation": operator_attestation},
        now=verification_time,
    )
    attested_at = _utc_timestamp(
        staged["attested_at"], code="weibo_oauth_attested_at_invalid"
    )
    if attested_at > verification_time:
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_after_verification")

    actions: dict[str, dict[str, Any]] = {}
    for action in WEIBO_ACTION_ORDER:
        requirement = WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]
        actions[action] = {
            "endpoint": requirement["endpoint"],
            "permission": requirement["permission"],
            "granted": staged["granted_actions"][action],
        }
    return {
        "contract_version": WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
        "calibration_id": _calibration_id(calibration_id),
        "account_id": _positive_int(
            account_id, code="weibo_oauth_account_binding_invalid"
        ),
        "execution_revision": _positive_int(
            execution_revision,
            code="weibo_oauth_execution_revision_invalid",
        ),
        "credential_kind": CREDENTIAL_KIND,
        "identity_verified": True,
        "app_review_status": staged["app_review_status"],
        "client_type": staged["client_type"],
        "verified_at": _iso_utc(verification_time),
        "evidence_source": EVIDENCE_SOURCE,
        "attested_by": staged["attested_by"],
        "attested_at": _iso_utc(attested_at),
        "actions": actions,
    }


def validate_weibo_oauth_capability_attestation(
    value: Any,
    *,
    calibration_id: str,
    account_id: int,
    execution_revision: int,
    runtime_capability_requirements: Mapping[str, Any],
    now: datetime | None = None,
    max_age: timedelta = MAX_EVIDENCE_AGE,
    require_approved_app: bool = True,
) -> dict[str, Any]:
    """Validate exact provenance, account/revision and per-action grants."""

    if not isinstance(value, Mapping) or set(value) != ATTESTATION_KEYS:
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    attestation = dict(value)
    if (
        type(attestation.get("contract_version")) is not int
        or attestation.get("contract_version")
        != WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION
        or attestation.get("credential_kind") != CREDENTIAL_KIND
        or attestation.get("identity_verified") is not True
        or attestation.get("evidence_source") != EVIDENCE_SOURCE
    ):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    if _calibration_id(attestation.get("calibration_id")) != _calibration_id(
        calibration_id
    ):
        raise WeiboOAuthCapabilityError("weibo_oauth_calibration_binding_invalid")
    expected_account_id = _positive_int(
        account_id, code="weibo_oauth_account_binding_invalid"
    )
    if (
        type(attestation.get("account_id")) is not int
        or attestation.get("account_id") != expected_account_id
    ):
        raise WeiboOAuthCapabilityError("weibo_oauth_account_binding_invalid")
    expected_revision = _positive_int(
        execution_revision, code="weibo_oauth_execution_revision_invalid"
    )
    if (
        type(attestation.get("execution_revision")) is not int
        or attestation.get("execution_revision") != expected_revision
    ):
        raise WeiboOAuthCapabilityError("weibo_oauth_execution_revision_mismatch")

    review_status = attestation.get("app_review_status")
    if review_status not in APP_REVIEW_STATUSES:
        raise WeiboOAuthCapabilityError("weibo_oauth_app_review_status_invalid")
    if require_approved_app and review_status != "approved":
        raise WeiboOAuthCapabilityError("weibo_oauth_app_not_approved")
    client_type = attestation.get("client_type")
    if client_type not in CLIENT_TYPES:
        raise WeiboOAuthCapabilityError("weibo_oauth_client_type_invalid")
    _required_actor(attestation.get("attested_by"))

    current = _utc_now(now)
    verified_at = _utc_timestamp(
        attestation.get("verified_at"),
        code="weibo_oauth_capability_evidence_time_invalid",
    )
    attested_at = _utc_timestamp(
        attestation.get("attested_at"), code="weibo_oauth_attested_at_invalid"
    )
    if verified_at > current or attested_at > current:
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_evidence_from_future")
    if attested_at > verified_at:
        raise WeiboOAuthCapabilityError("weibo_oauth_attestation_after_verification")
    if current - verified_at > max_age or current - attested_at > max_age:
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_evidence_stale")

    if not isinstance(runtime_capability_requirements, Mapping):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    requirements = dict(runtime_capability_requirements)
    if (
        type(requirements.get("contract_version")) is not int
        or requirements.get("contract_version")
        != WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION
        or set(requirements) != {"contract_version", "actions"}
    ):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    required_actions = requirements.get("actions")
    actions = attestation.get("actions")
    if not isinstance(required_actions, Mapping) or not isinstance(actions, Mapping):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
    if set(actions) != set(WEIBO_ACTION_ORDER):
        raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")

    for action, grant_value in actions.items():
        if not isinstance(grant_value, Mapping) or set(grant_value) != ACTION_ATTESTATION_KEYS:
            raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
        expected = WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]
        if (
            grant_value.get("endpoint") != expected["endpoint"]
            or grant_value.get("permission") != expected["permission"]
            or type(grant_value.get("granted")) is not bool
        ):
            raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")

    for action, requirement_value in required_actions.items():
        if action not in WEIBO_ACTION_ORDER or not isinstance(
            requirement_value, Mapping
        ):
            raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
        expected = WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]
        if dict(requirement_value) != expected:
            raise WeiboOAuthCapabilityError("weibo_oauth_capability_contract_mismatch")
        grant = actions[action]
        if grant["granted"] is not True:
            raise WeiboOAuthCapabilityError(f"weibo_oauth_action_not_granted:{action}")
        if action == "followed" and client_type != "weibo":
            raise WeiboOAuthCapabilityError("weibo_oauth_follow_client_type_required")
    return attestation
