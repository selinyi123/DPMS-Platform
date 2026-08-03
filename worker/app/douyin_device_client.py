"""Strict client for the loopback-only Windows Douyin device agent."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from shared.douyin_device_contract import (
    DOUYIN_DEVICE_ACTION_ORDER,
    normalize_douyin_device_public_config,
)


_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ACTION_TO_DEVICE = {
    "followed": "follow",
    "liked": "like",
    "commented": "comment",
    "favorited": "favorite",
}


class DouyinDeviceClientError(RuntimeError):
    pass


class DouyinDevicePreflightBlocked(DouyinDeviceClientError):
    pass


class DouyinDeviceActionRejected(DouyinDeviceClientError):
    def __init__(self, action: str, reason: str, *, risk: bool = False):
        self.action = action
        self.reason = str(reason or "device_action_rejected")
        self.risk = bool(risk)
        super().__init__(f"douyin_device_action_rejected:{action}:{self.reason}")


class DouyinDeviceActionOutcomeUnknown(DouyinDeviceClientError):
    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = str(reason or "device_action_outcome_unknown")
        super().__init__(f"douyin_device_action_outcome_unknown:{action}:{self.reason}")


@dataclass(frozen=True)
class DouyinDeviceReceipt:
    operation_key: str
    target_hash: str
    action: str
    status: str
    mutation_attempted: bool
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    reason: str


def _validated_url(value: str, *, allow_loopback: bool = False) -> str:
    parsed = urlsplit(str(value or "").strip())
    allowed_hosts = {"host.docker.internal"}
    if allow_loopback:
        allowed_hosts.update({"127.0.0.1", "localhost"})
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise DouyinDeviceClientError("douyin_device_agent_url_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DouyinDeviceClientError("douyin_device_agent_url_invalid") from exc
    if port is None or not (1 <= port <= 65535):
        raise DouyinDeviceClientError("douyin_device_agent_url_invalid")
    return f"http://{parsed.hostname}:{port}"


def _required_token(value: str) -> str:
    token = str(value or "")
    if (
        not token
        or token != token.strip()
        or len(token.encode("utf-8", errors="strict")) < 32
        or len(token.encode("utf-8", errors="strict")) > 512
        or "\r" in token
        or "\n" in token
    ):
        raise DouyinDeviceClientError("douyin_device_agent_token_invalid")
    return token


def _json_object(response: httpx.Response, *, code: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (TypeError, ValueError) as exc:
        raise DouyinDeviceClientError(code) from exc
    if not isinstance(value, Mapping):
        raise DouyinDeviceClientError(code)
    return dict(value)


def _validated_identity(
    value: Mapping[str, Any], expected: Mapping[str, Any]
) -> dict[str, str]:
    actual = {
        "agent_id": value.get("agent_id"),
        "manifest_sha256": value.get("manifest_sha256"),
        "device_serial_sha256": value.get("device_serial_sha256"),
        "account_id_sha256": value.get("account_id_sha256"),
    }
    normalized = normalize_douyin_device_public_config(
        {"device_agent": actual}
    )
    if normalized != dict(expected):
        raise DouyinDevicePreflightBlocked("douyin_device_identity_mismatch")
    return normalized


def _validated_snapshot(
    value: Any,
    *,
    target_hash: str,
    required_actions: tuple[str, ...],
    expected_identity: Mapping[str, Any],
    follow_required: bool,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DouyinDeviceClientError("douyin_device_snapshot_invalid")
    snapshot = dict(value)
    _validated_identity(snapshot, expected_identity)
    action_states = snapshot.get("action_states")
    if (
        snapshot.get("package") != "com.ss.android.ugc.aweme"
        or snapshot.get("package_ok") is not True
        or snapshot.get("blocked") is not False
        or snapshot.get("target_hash") != target_hash
        or snapshot.get("target_identity_verified") is not True
        or (follow_required and snapshot.get("follow_target_verified") is not True)
        or (
            not follow_required
            and snapshot.get("follow_target_verified") not in {False, None}
        )
        or not isinstance(snapshot.get("xml_sha256"), str)
        or not _SHA256.fullmatch(snapshot["xml_sha256"])
        or not isinstance(action_states, Mapping)
    ):
        raise DouyinDevicePreflightBlocked("douyin_device_snapshot_not_ready")
    for action in required_actions:
        device_action = _ACTION_TO_DEVICE[action]
        state = action_states.get(device_action)
        if not isinstance(state, Mapping) or state.get("calibrated") is not True:
            raise DouyinDevicePreflightBlocked(
                f"douyin_device_action_not_calibrated:{action}"
            )
    return snapshot


class DouyinDeviceClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_loopback: bool = False,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not (
            1 <= float(timeout_seconds) <= 120
        ):
            raise DouyinDeviceClientError("douyin_device_timeout_invalid")
        self.base_url = _validated_url(base_url, allow_loopback=allow_loopback)
        self.timeout_seconds = float(timeout_seconds)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {_required_token(token)}"},
            timeout=httpx.Timeout(self.timeout_seconds),
            transport=transport,
            trust_env=False,
        )

    @classmethod
    def from_environment(cls) -> "DouyinDeviceClient":
        return cls(
            base_url=os.getenv("DOUYIN_DEVICE_AGENT_URL", ""),
            token=os.getenv("DOUYIN_DEVICE_AGENT_TOKEN", ""),
            timeout_seconds=float(
                os.getenv("DOUYIN_DEVICE_AGENT_TIMEOUT_SECONDS", "45")
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(
        self,
        *,
        expected_identity: Mapping[str, Any],
        required_actions: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            response = await self._client.get("/health")
        except httpx.HTTPError as exc:
            raise DouyinDevicePreflightBlocked(
                "douyin_device_health_unreachable"
            ) from exc
        if response.status_code != 200:
            raise DouyinDevicePreflightBlocked(
                f"douyin_device_health_http_{response.status_code}"
            )
        payload = _json_object(response, code="douyin_device_health_invalid")
        _validated_identity(payload, expected_identity)
        supported = payload.get("supported_actions")
        expected_device_actions = [_ACTION_TO_DEVICE[action] for action in required_actions]
        if (
            payload.get("status") != "ok"
            or payload.get("ready") is not True
            or payload.get("package") != "com.ss.android.ugc.aweme"
            or not isinstance(supported, list)
            or any(action not in supported for action in expected_device_actions)
        ):
            raise DouyinDevicePreflightBlocked("douyin_device_health_not_ready")
        return payload

    async def snapshot(
        self,
        *,
        operation_key: str,
        target_hash: str,
        required_actions: tuple[str, ...],
        comment: str | None = None,
        follow_target_handle: str,
        expected_identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        # Snapshot is read-only and the host service intentionally accepts only
        # target-bound inputs; operation_key remains a caller-side trace key.
        if not operation_key:
            raise DouyinDeviceClientError("douyin_device_operation_key_invalid")
        payload: dict[str, Any] = {"target_hash": target_hash}
        if comment is not None:
            payload["comment"] = comment
        if "followed" in required_actions:
            payload["follow_target_handle"] = follow_target_handle
        try:
            response = await self._client.post("/v1/snapshot", json=payload)
        except httpx.HTTPError as exc:
            raise DouyinDevicePreflightBlocked(
                "douyin_device_snapshot_unreachable"
            ) from exc
        if response.status_code != 200:
            raise DouyinDevicePreflightBlocked(
                f"douyin_device_snapshot_http_{response.status_code}"
            )
        body = _json_object(response, code="douyin_device_snapshot_invalid")
        return _validated_snapshot(
            body.get("snapshot"),
            target_hash=target_hash,
            required_actions=required_actions,
            expected_identity=expected_identity,
            follow_required="followed" in required_actions,
        )

    async def execute(
        self,
        *,
        operation_key: str,
        target_hash: str,
        action: str,
        comment: str | None,
        follow_target_handle: str,
        required_actions: tuple[str, ...],
        expected_identity: Mapping[str, Any],
    ) -> DouyinDeviceReceipt:
        if action not in DOUYIN_DEVICE_ACTION_ORDER:
            raise DouyinDeviceClientError("douyin_device_action_invalid")
        device_action = _ACTION_TO_DEVICE[action]
        payload: dict[str, Any] = {
            "request_id": operation_key,
            "target_hash": target_hash,
            "action": device_action,
        }
        if action == "commented":
            payload["comment"] = comment
        if action == "followed":
            payload["follow_target_handle"] = follow_target_handle
        try:
            response = await self._client.post("/v1/execute", json=payload)
        except httpx.HTTPError as exc:
            raise DouyinDeviceActionOutcomeUnknown(
                action, "transport_error"
            ) from exc
        if response.status_code in {400, 401, 403, 409, 422, 429}:
            raise DouyinDeviceActionRejected(
                action, f"device_agent_http_{response.status_code}"
            )
        if response.status_code != 200:
            raise DouyinDeviceActionOutcomeUnknown(
                action, f"device_agent_http_{response.status_code}"
            )
        try:
            body = _json_object(response, code="douyin_device_receipt_invalid")
            result = body.get("result")
            if not isinstance(result, Mapping):
                raise DouyinDeviceClientError("douyin_device_receipt_invalid")
            before = _validated_snapshot(
                body.get("before_snapshot"),
                target_hash=target_hash,
                required_actions=required_actions,
                expected_identity=expected_identity,
                follow_required=action == "followed",
            )
            after = _validated_snapshot(
                body.get("after_snapshot"),
                target_hash=target_hash,
                required_actions=required_actions,
                expected_identity=expected_identity,
                follow_required=action == "followed",
            )
            status = str(result.get("status") or "").strip().lower()
            mutation_attempted = result.get("mutation_attempted")
            outcome_known = result.get("outcome_known")
            if (
                body.get("request_id") != operation_key
                or body.get("target_hash") != target_hash
                or body.get("action") != device_action
                or result.get("action") != device_action
                or type(mutation_attempted) is not bool
                or type(outcome_known) is not bool
                or status not in {"succeeded", "already_done", "blocked", "unknown"}
            ):
                raise DouyinDeviceClientError("douyin_device_receipt_invalid")
        except (DouyinDevicePreflightBlocked, DouyinDeviceClientError) as exc:
            raise DouyinDeviceActionOutcomeUnknown(
                action, "invalid_receipt"
            ) from exc
        reason = str(result.get("reason") or "")[:512]
        if status in {"succeeded", "already_done"}:
            if outcome_known is not True or after.get("blocked") is not False:
                raise DouyinDeviceActionOutcomeUnknown(action, "unconfirmed_success")
        elif status == "unknown" or outcome_known is not True:
            raise DouyinDeviceActionOutcomeUnknown(action, reason or status)
        else:
            raise DouyinDeviceActionRejected(
                action,
                reason or "blocked",
                risk=bool(before.get("risk_texts") or after.get("risk_texts")),
            )
        return DouyinDeviceReceipt(
            operation_key=operation_key,
            target_hash=target_hash,
            action=action,
            status=status,
            mutation_attempted=bool(mutation_attempted),
            before_snapshot=before,
            after_snapshot=after,
            reason=reason,
        )


__all__ = (
    "DouyinDeviceActionOutcomeUnknown",
    "DouyinDeviceActionRejected",
    "DouyinDeviceClient",
    "DouyinDeviceClientError",
    "DouyinDevicePreflightBlocked",
    "DouyinDeviceReceipt",
)
