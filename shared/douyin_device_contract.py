"""Pure, versioned contract for a local Douyin Android device agent.

Core and Worker import this module so the endpoint identity, immutable task
binding, and read-only Probe/Shadow observations are hashed identically.  The
contract never contains the device-agent bearer token, the raw ADB serial, or
the raw device account identifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


DOUYIN_DEVICE_CONTRACT_VERSION = 1
DOUYIN_DEVICE_EXECUTION_PATH = "douyin_device_v1"
DOUYIN_DEVICE_CALIBRATION_CHECK_URL = "device-agent://douyin/health"
DOUYIN_DEVICE_PROBE_OBSERVATION_KIND = "douyin_device_probe_v1"
DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND = "douyin_device_shadow_v1"
DOUYIN_DEVICE_OBSERVATION_KINDS = frozenset(
    {
        DOUYIN_DEVICE_PROBE_OBSERVATION_KIND,
        DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND,
    }
)
DOUYIN_DEVICE_ACTION_ORDER = (
    "followed",
    "liked",
    "commented",
    "favorited",
)
DOUYIN_DEVICE_PUBLIC_CONFIG_FIELDS = frozenset(
    {
        "agent_id",
        "manifest_sha256",
        "device_serial_sha256",
        "account_id_sha256",
    }
)
DOUYIN_DEVICE_CREDENTIAL_KIND = "device_agent"
DOUYIN_DEVICE_CREDENTIAL_FIELDS = frozenset(
    {"contract_version", "credential_kind", "device_agent"}
)
DOUYIN_DEVICE_OBSERVATION_FIELDS = frozenset(
    {
        "contract_version",
        "platform",
        "execution_path_id",
        "lottery_id",
        "account_id",
        "execution_revision",
        "target_hash",
        "observed_target_hash",
        "rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "required_actions",
        "follow_target_handle_hash",
        "comment_text_hash",
        "observation_kind",
        "observed_at",
        "evidence_id",
        "side_effects",
        "agent_id",
        "manifest_sha256",
        "device_serial_sha256",
        "account_id_sha256",
        "package",
        "package_ok",
        "risk_blocked",
        "target_identity_verified",
        "follow_target_verified",
        "capability_checks",
    }
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    re.ASCII,
)


class DouyinDeviceContractError(ValueError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ValidatedDouyinDeviceObservation:
    observation: dict[str, Any]
    observation_hash: str
    observed_at: datetime


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DouyinDeviceContractError(
            "douyin_device_contract_not_canonicalizable"
        ) from exc


def _sha256_hex(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def compute_douyin_exact_text_hash(value: str) -> str:
    if not isinstance(value, str):
        raise DouyinDeviceContractError("douyin_device_text_invalid")
    try:
        return _sha256_hex(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DouyinDeviceContractError("douyin_device_text_invalid") from exc


def normalize_douyin_device_public_config(
    selector_config: Mapping[str, Any],
) -> dict[str, str]:
    if not isinstance(selector_config, Mapping):
        raise DouyinDeviceContractError("douyin_device_config_invalid")
    raw = selector_config.get("device_agent")
    if not isinstance(raw, Mapping) or set(raw) != DOUYIN_DEVICE_PUBLIC_CONFIG_FIELDS:
        raise DouyinDeviceContractError("douyin_device_config_invalid")
    result = {key: raw.get(key) for key in DOUYIN_DEVICE_PUBLIC_CONFIG_FIELDS}
    if (
        not isinstance(result["agent_id"], str)
        or not _TOKEN_PATTERN.fullmatch(result["agent_id"])
        or any(
            not isinstance(result[key], str)
            or not _SHA256_PATTERN.fullmatch(result[key])
            for key in (
                "manifest_sha256",
                "device_serial_sha256",
                "account_id_sha256",
            )
        )
    ):
        raise DouyinDeviceContractError("douyin_device_config_invalid")
    return {key: str(result[key]) for key in sorted(result)}


def parse_douyin_device_credential(value: Any) -> dict[str, Any]:
    """Parse the non-secret account envelope used to select device routing."""

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DouyinDeviceContractError(
                "douyin_device_credential_invalid"
            ) from exc
    if isinstance(value, str):
        def reject_duplicates(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = item
            return result

        try:
            value = json.loads(value, object_pairs_hook=reject_duplicates)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DouyinDeviceContractError(
                "douyin_device_credential_invalid"
            ) from exc
    if not isinstance(value, Mapping) or set(value) != DOUYIN_DEVICE_CREDENTIAL_FIELDS:
        raise DouyinDeviceContractError("douyin_device_credential_invalid")
    if (
        value.get("contract_version") != DOUYIN_DEVICE_CONTRACT_VERSION
        or value.get("credential_kind") != DOUYIN_DEVICE_CREDENTIAL_KIND
    ):
        raise DouyinDeviceContractError("douyin_device_credential_invalid")
    public_config = normalize_douyin_device_public_config(
        {"device_agent": value.get("device_agent")}
    )
    return {
        "contract_version": DOUYIN_DEVICE_CONTRACT_VERSION,
        "credential_kind": DOUYIN_DEVICE_CREDENTIAL_KIND,
        "device_agent": public_config,
    }


def normalize_douyin_device_credential(value: Any) -> str:
    return _canonical_json_bytes(parse_douyin_device_credential(value)).decode(
        "utf-8"
    )


def is_douyin_device_credential(value: Any) -> bool:
    try:
        parse_douyin_device_credential(value)
    except DouyinDeviceContractError:
        return False
    return True


def compute_douyin_device_config_hash(
    execution_revision: int,
    selector_config: Mapping[str, Any],
) -> str:
    if type(execution_revision) is not int or execution_revision <= 0:
        raise DouyinDeviceContractError("execution_revision_invalid")
    return _sha256_hex(
        _canonical_json_bytes(
            {
                "contract_version": DOUYIN_DEVICE_CONTRACT_VERSION,
                "execution_path_id": DOUYIN_DEVICE_EXECUTION_PATH,
                "execution_revision": execution_revision,
                "device_agent": normalize_douyin_device_public_config(
                    selector_config
                ),
            }
        )
    )


def format_douyin_device_observed_at(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DouyinDeviceContractError("douyin_device_observed_at_invalid")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def hash_douyin_device_observation(observation: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical_json_bytes(dict(observation)))


def _parse_observation(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DouyinDeviceContractError(
                "douyin_device_observation_invalid"
            ) from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DouyinDeviceContractError(
                "douyin_device_observation_invalid"
            ) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise DouyinDeviceContractError("douyin_device_observation_invalid")


def _parse_observed_at(value: Any) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise DouyinDeviceContractError("douyin_device_observed_at_invalid")
    try:
        return datetime.fromisoformat(f"{value[:-1]}+00:00").astimezone(
            timezone.utc
        )
    except ValueError as exc:
        raise DouyinDeviceContractError(
            "douyin_device_observed_at_invalid"
        ) from exc


def validate_douyin_device_observation(
    value: Any,
    *,
    expected_observation_kind: str,
    expected_evidence_id: str,
    expected_lottery_id: int,
    expected_account_id: int,
    expected_execution_revision: int,
    expected_target_hash: str,
    expected_rule_snapshot_id: int,
    expected_rule_hash: str,
    expected_action_plan_hash: str,
    expected_config_hash: str,
    expected_actions: tuple[str, ...] | list[str],
    expected_follow_target_handle_hash: str,
    expected_comment_text_hash: str,
    expected_public_config: Mapping[str, Any],
) -> ValidatedDouyinDeviceObservation:
    observation = _parse_observation(value)
    public_config = normalize_douyin_device_public_config(
        {"device_agent": dict(expected_public_config)}
    )
    actions = list(expected_actions)
    hashes = (
        expected_target_hash,
        expected_rule_hash,
        expected_action_plan_hash,
        expected_config_hash,
        expected_follow_target_handle_hash,
        expected_comment_text_hash,
    )
    checks = observation.get("capability_checks")
    if (
        set(observation) != DOUYIN_DEVICE_OBSERVATION_FIELDS
        or expected_observation_kind not in DOUYIN_DEVICE_OBSERVATION_KINDS
        or not isinstance(expected_evidence_id, str)
        or not _TOKEN_PATTERN.fullmatch(expected_evidence_id)
        or any(type(value) is not int or value <= 0 for value in (
            expected_lottery_id,
            expected_account_id,
            expected_execution_revision,
            expected_rule_snapshot_id,
        ))
        or any(not _SHA256_PATTERN.fullmatch(str(value or "")) for value in hashes)
        or not actions
        or actions
        != [action for action in DOUYIN_DEVICE_ACTION_ORDER if action in set(actions)]
        or len(actions) != len(set(actions))
        or observation.get("contract_version") != DOUYIN_DEVICE_CONTRACT_VERSION
        or observation.get("platform") != "douyin"
        or observation.get("execution_path_id") != DOUYIN_DEVICE_EXECUTION_PATH
        or observation.get("lottery_id") != expected_lottery_id
        or observation.get("account_id") != expected_account_id
        or observation.get("execution_revision") != expected_execution_revision
        or observation.get("target_hash") != expected_target_hash
        or observation.get("observed_target_hash") != expected_target_hash
        or observation.get("rule_snapshot_id") != expected_rule_snapshot_id
        or observation.get("rule_hash") != expected_rule_hash
        or observation.get("action_plan_hash") != expected_action_plan_hash
        or observation.get("config_hash") != expected_config_hash
        or observation.get("required_actions") != actions
        or observation.get("follow_target_handle_hash")
        != expected_follow_target_handle_hash
        or observation.get("comment_text_hash") != expected_comment_text_hash
        or observation.get("observation_kind") != expected_observation_kind
        or observation.get("evidence_id") != expected_evidence_id
        or observation.get("side_effects") is not False
        or observation.get("agent_id") != public_config["agent_id"]
        or observation.get("manifest_sha256") != public_config["manifest_sha256"]
        or observation.get("device_serial_sha256")
        != public_config["device_serial_sha256"]
        or observation.get("account_id_sha256")
        != public_config["account_id_sha256"]
        or observation.get("package") != "com.ss.android.ugc.aweme"
        or observation.get("package_ok") is not True
        or observation.get("risk_blocked") is not False
        or observation.get("target_identity_verified") is not True
        or (
            "followed" in actions
            and observation.get("follow_target_verified") is not True
        )
        or (
            "followed" not in actions
            and observation.get("follow_target_verified") is not False
        )
        or not isinstance(checks, Mapping)
        or set(checks) != set(actions)
        or any(checks.get(action) is not True for action in actions)
    ):
        raise DouyinDeviceContractError(
            "douyin_device_observation_binding_mismatch"
        )
    return ValidatedDouyinDeviceObservation(
        observation=observation,
        observation_hash=hash_douyin_device_observation(observation),
        observed_at=_parse_observed_at(observation.get("observed_at")),
    )


def validate_douyin_device_observation_binding(
    value: Any,
    *,
    source_observation_kind: str,
    source_observation_hash: str,
    evidence_observation_kind: str,
    evidence_observation_hash: str,
    **expected: Any,
) -> ValidatedDouyinDeviceObservation:
    expected_kind = str(expected.get("expected_observation_kind") or "")
    if (
        source_observation_kind != expected_kind
        or evidence_observation_kind != expected_kind
    ):
        raise DouyinDeviceContractError(
            "douyin_device_observation_kind_mismatch"
        )
    validated = validate_douyin_device_observation(value, **expected)
    if (
        validated.observation_hash != source_observation_hash
        or validated.observation_hash != evidence_observation_hash
    ):
        raise DouyinDeviceContractError(
            "douyin_device_observation_hash_mismatch"
        )
    return validated


__all__ = (
    "DOUYIN_DEVICE_ACTION_ORDER",
    "DOUYIN_DEVICE_CALIBRATION_CHECK_URL",
    "DOUYIN_DEVICE_CONTRACT_VERSION",
    "DOUYIN_DEVICE_CREDENTIAL_FIELDS",
    "DOUYIN_DEVICE_CREDENTIAL_KIND",
    "DOUYIN_DEVICE_EXECUTION_PATH",
    "DOUYIN_DEVICE_OBSERVATION_FIELDS",
    "DOUYIN_DEVICE_OBSERVATION_KINDS",
    "DOUYIN_DEVICE_PROBE_OBSERVATION_KIND",
    "DOUYIN_DEVICE_PUBLIC_CONFIG_FIELDS",
    "DOUYIN_DEVICE_SHADOW_OBSERVATION_KIND",
    "DouyinDeviceContractError",
    "ValidatedDouyinDeviceObservation",
    "compute_douyin_device_config_hash",
    "compute_douyin_exact_text_hash",
    "format_douyin_device_observed_at",
    "hash_douyin_device_observation",
    "normalize_douyin_device_public_config",
    "is_douyin_device_credential",
    "normalize_douyin_device_credential",
    "parse_douyin_device_credential",
    "validate_douyin_device_observation",
    "validate_douyin_device_observation_binding",
)
