"""Transport-injectable client for official Weibo OAuth APIs.

Mutations are deliberately single-attempt.  A timeout, cancellation, 5xx or
unverifiable success receipt is an unknown remote outcome and must be settled
through the task intent journal before an operator decides whether to retry.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from app.action_plan import canonical_json_bytes
from app.weibo.capabilities import validate_weibo_oauth_capability_attestation
from app.weibo.credentials import validate_weibo_rip, weibo_rip_hmac


API_BASE = "https://api.weibo.com/2"
STATUS_ID_PATTERN = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
MAX_STATUS_ID = (1 << 63) - 1
MBLOG_ID_PATTERN = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9]{6,16}\Z")
UID_PATTERN = re.compile(r"\d{1,20}\Z")
OPERATION_KEY_PATTERN = re.compile(r"[A-Za-z0-9:_-]{8,128}\Z")
REMOTE_ERROR_CODE_PATTERN = re.compile(r"[1-9][0-9]{0,9}\Z", re.ASCII)

ENDPOINTS = {
    "followed": f"{API_BASE}/friendships/create.json",
    "liked": f"{API_BASE}/attitudes/create.json",
    "commented": f"{API_BASE}/comments/create.json",
    "favorited": f"{API_BASE}/favorites/create.json",
    "reposted": f"{API_BASE}/statuses/repost.json",
}

# Official Weibo API error table (reviewed 2026-07-22):
# https://open.weibo.com/wiki/Error_code
# Keep this deliberately small.  Remote message text is untrusted/localized and
# must never be used to guess an account state.
WEIBO_AUTHENTICATION_ERROR_CODES = frozenset(
    {21301, 21314, 21315, 21316, 21317, 21319, 21327}
)
WEIBO_PERMISSION_ERROR_CODES = frozenset({10005, 10014, 21321})
WEIBO_RATE_LIMIT_ERROR_CODES = frozenset({10004, 10022, 10023, 10024})
WEIBO_PUBLISH_RATE_LIMIT_ERROR_CODES = frozenset({20016})
WEIBO_REMOTE_SUCCESS_POSSIBLE_ERROR_CODES = frozenset({20032})


@dataclass(frozen=True)
class WeiboApiRejectionDisposition:
    category: str
    account_status: str
    confirmed_no_effect: bool


def classify_weibo_api_rejection(
    action: str,
    error_code: int,
    *,
    http_status: int | None = None,
) -> WeiboApiRejectionDisposition:
    """Classify a remote error envelope without guessing from message text."""

    normalized_action = str(action or "").strip().lower()
    code = int(error_code)
    status = int(http_status) if http_status is not None else None
    if code in WEIBO_AUTHENTICATION_ERROR_CODES:
        return WeiboApiRejectionDisposition(
            "authentication_invalid", "login_required", True
        )
    if code in WEIBO_PERMISSION_ERROR_CODES:
        return WeiboApiRejectionDisposition("permission_denied", "warming", True)
    if code in WEIBO_RATE_LIMIT_ERROR_CODES:
        return WeiboApiRejectionDisposition("rate_limited", "cooling", True)
    if code in WEIBO_PUBLISH_RATE_LIMIT_ERROR_CODES:
        return WeiboApiRejectionDisposition(
            "rate_limited",
            "cooling",
            normalized_action in {"commented", "reposted"},
        )
    if code in WEIBO_REMOTE_SUCCESS_POSSIBLE_ERROR_CODES:
        return WeiboApiRejectionDisposition(
            "remote_success_possible", "cooling", False
        )
    # HTTP status alone classifies the account problem but is not enough to
    # prove that a mutation had no effect.  The caller must retain the intent
    # as unknown and require reconciliation.
    if status == 401:
        return WeiboApiRejectionDisposition(
            "authentication_invalid", "login_required", False
        )
    if status == 403:
        return WeiboApiRejectionDisposition("permission_denied", "warming", False)
    if status == 429:
        return WeiboApiRejectionDisposition("rate_limited", "cooling", False)
    # An unrecognised error_code may acquire new semantics (including delayed
    # success), so it is never evidence of a confirmed no-effect rejection.
    return WeiboApiRejectionDisposition("platform_rejected", "cooling", False)


class AsyncHttpClient(Protocol):
    async def request(self, method: str, url: str, **kwargs): ...

    async def aclose(self) -> None: ...


class WeiboApiError(RuntimeError):
    pass


class WeiboApiCapabilityDenied(WeiboApiError):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(f"weibo_oauth_action_not_granted:{action}")


class WeiboDuplicateOperation(WeiboApiError):
    def __init__(self, operation_key: str) -> None:
        self.operation_key = operation_key
        super().__init__("weibo_oauth_duplicate_operation_key")


class WeiboApiRejected(WeiboApiError):
    def __init__(
        self,
        action: str,
        error_code: int,
        message: str = "",
        *,
        http_status: int | None = None,
    ) -> None:
        self.action = action
        self.error_code = int(error_code)
        self.remote_message = message
        self.http_status = int(http_status) if http_status is not None else None
        disposition = classify_weibo_api_rejection(
            self.action,
            self.error_code,
            http_status=self.http_status,
        )
        self.category = disposition.category
        self.account_status = disposition.account_status
        self.confirmed_no_effect = disposition.confirmed_no_effect
        super().__init__(f"weibo_api_rejected:{action}:{self.error_code}")


class WeiboApiActionOutcomeUnknown(WeiboApiError):
    def __init__(self, action: str, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"weibo_action_outcome_unknown:{action}:{reason}")


@dataclass(frozen=True)
class WeiboActionReceipt:
    action: str
    target_id: str
    remote_id: str
    operation_key: str
    request_payload_hash: str


@dataclass(frozen=True)
class WeiboMutationRequest:
    action: str
    target_id: str
    endpoint: str
    data: dict[str, Any]
    audit_spec: dict[str, Any]
    audit_spec_hash: str


def _required_token(value: Any) -> str:
    token = value if isinstance(value, str) else str(value or "")
    if (
        not token
        or token != token.strip()
        or len(token) > 4096
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token)
    ):
        raise WeiboApiError("weibo_oauth_access_token_invalid")
    return token


def _remote_error_code(value: Any) -> int:
    if isinstance(value, bool):
        raise WeiboApiError("weibo_api_error_code_invalid")
    if isinstance(value, int):
        if value > 0:
            return value
        raise WeiboApiError("weibo_api_error_code_invalid")
    if isinstance(value, str) and REMOTE_ERROR_CODE_PATTERN.fullmatch(value):
        return int(value)
    raise WeiboApiError("weibo_api_error_code_invalid")


def _operation_key(value: Any) -> str:
    key = str(value or "").strip()
    if not OPERATION_KEY_PATTERN.fullmatch(key):
        raise WeiboApiError("weibo_oauth_operation_key_invalid")
    return key


def _status_id(value: Any) -> str:
    result = str(value or "").strip()
    if not _is_numeric_status_id(result):
        raise WeiboApiError("weibo_status_numeric_id_required")
    return result


def _is_numeric_status_id(value: Any) -> bool:
    result = value if isinstance(value, str) else str(value or "")
    return bool(
        STATUS_ID_PATTERN.fullmatch(result)
        and int(result) <= MAX_STATUS_ID
    )


def _uid(value: Any) -> str:
    result = str(value or "").strip()
    if not UID_PATTERN.fullmatch(result) or int(result) <= 0:
        raise WeiboApiError("weibo_target_uid_invalid")
    return result


def validate_weibo_text(
    action: str,
    value: Any,
    *,
    allow_none: bool = False,
) -> str | None:
    """Validate exact reviewed text without truncating or normalizing it."""

    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise WeiboApiError(f"weibo_{action}_text_invalid")
    text = value
    if not text.strip():
        raise WeiboApiError(f"weibo_{action}_text_invalid")
    try:
        utf8_length = len(text.encode("utf-8"))
        utf16_units = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise WeiboApiError(f"action_payload_{action}_text_invalid") from exc
    if utf8_length > 4096:
        raise WeiboApiError(f"action_payload_{action}_text_too_large")
    if utf16_units > 140:
        raise WeiboApiError(f"weibo_{action}_text_too_long")
    return text


def build_weibo_mutation_request(
    action: str,
    target_id: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    rip: str = "",
) -> WeiboMutationRequest:
    """Build the exact HTTP body and its non-secret durable audit contract."""

    body_payload = dict(payload or {})
    if action == "followed":
        target = _uid(target_id)
        canonical_rip = validate_weibo_rip(rip, required=True)
        data = {"uid": target, "rip": canonical_rip}
    elif action == "liked":
        target = _status_id(target_id)
        data = {"id": target}
    elif action == "commented":
        target = _status_id(target_id)
        canonical_rip = validate_weibo_rip(rip, required=True)
        data = {
            "id": target,
            "comment": validate_weibo_text(
                "commented", body_payload.get("text")
            ),
            "comment_ori": 0,
            "rip": canonical_rip,
        }
    elif action == "favorited":
        target = _status_id(target_id)
        data = {"id": target}
    elif action == "reposted":
        target = _status_id(target_id)
        canonical_rip = validate_weibo_rip(rip, required=True)
        data = {"id": target, "is_comment": 0, "rip": canonical_rip}
        if "text" in body_payload and body_payload.get("text") is not None:
            data["status"] = validate_weibo_text(
                "reposted", body_payload.get("text")
            )
    else:
        raise WeiboApiError("weibo_action_invalid")

    audit_body = dict(data)
    if "rip" in audit_body:
        audit_body["rip_hash"] = weibo_rip_hmac(audit_body.pop("rip"))
    audit_spec = {
        "method": "POST",
        "endpoint": ENDPOINTS[action],
        "body": audit_body,
    }
    audit_spec_hash = hashlib.sha256(
        canonical_json_bytes(audit_spec)
    ).hexdigest()
    return WeiboMutationRequest(
        action=action,
        target_id=target,
        endpoint=ENDPOINTS[action],
        data=data,
        audit_spec=audit_spec,
        audit_spec_hash=audit_spec_hash,
    )


def _object_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    return str(value or "").strip()


def _id_from(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return _object_id(value.get("idstr") or value.get("id"))


def status_identifier_from_canonical_uri(value: Any) -> str:
    parsed = urlparse(str(value or "").strip())
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "canonical"
        or parsed.netloc != "weibo"
        or len(parts) != 2
        or parts[0] != "status"
    ):
        raise WeiboApiError("weibo_canonical_status_invalid")
    identifier = parts[1]
    if not (
        _is_numeric_status_id(identifier)
        or MBLOG_ID_PATTERN.fullmatch(identifier)
    ):
        raise WeiboApiError("weibo_canonical_status_invalid")
    return identifier


class WeiboApiClient:
    """Official OAuth client with capability and local replay fences.

    The in-memory operation-key fence is a second line of defence.  Callers
    still need the durable ``external_action_intents`` journal before invoking
    a method; a process restart cannot make an HTTP client a durable ledger.
    """

    def __init__(
        self,
        access_token: str,
        *,
        capability_attestation: Mapping[str, Any],
        calibration_id: str,
        account_id: int,
        execution_revision: int,
        runtime_capability_requirements: Mapping[str, Any],
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: AsyncHttpClient | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if http_client is not None and transport is not None:
            raise WeiboApiError("weibo_http_client_injection_ambiguous")
        self._access_token = _required_token(access_token)
        self._calibration_id = str(calibration_id or "").strip()
        self._account_id = account_id
        self._execution_revision = execution_revision
        self._runtime_capability_requirements = copy.deepcopy(
            dict(runtime_capability_requirements)
        )
        self.capability_attestation = validate_weibo_oauth_capability_attestation(
            copy.deepcopy(dict(capability_attestation)),
            calibration_id=self._calibration_id,
            account_id=account_id,
            execution_revision=execution_revision,
            runtime_capability_requirements=self._runtime_capability_requirements,
        )
        self._required_actions = frozenset(
            dict(self._runtime_capability_requirements.get("actions") or {})
        )
        self._owns_client = http_client is None
        self._client: AsyncHttpClient = http_client or httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._started_operation_keys: set[str] = set()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "WeiboApiClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _require_capability(self, action: str) -> None:
        # Recheck freshness at the mutation boundary. Constructing a client is
        # not permission to keep using an attestation after its 24-hour window.
        validate_weibo_oauth_capability_attestation(
            self.capability_attestation,
            calibration_id=self._calibration_id,
            account_id=self._account_id,
            execution_revision=self._execution_revision,
            runtime_capability_requirements=self._runtime_capability_requirements,
        )
        if action not in self._required_actions:
            raise WeiboApiCapabilityDenied(action)

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        action: str,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = dict(params or {})
        body = dict(data or {})
        if method.upper() == "GET":
            query["access_token"] = self._access_token
        else:
            body["access_token"] = self._access_token
        response = await self._client.request(
            method,
            url,
            params=query or None,
            data=body or None,
            headers=(
                {"Content-Type": "application/x-www-form-urlencoded"}
                if method.upper() != "GET"
                else None
            ),
        )
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        try:
            payload = response.json()
        except Exception as exc:
            if response.status_code in {401, 403, 429}:
                raise WeiboApiRejected(
                    action,
                    -1,
                    f"HTTP {response.status_code}",
                    http_status=response.status_code,
                ) from exc
            raise WeiboApiError("weibo_api_response_not_json") from exc
        if not isinstance(payload, dict):
            raise WeiboApiError("weibo_api_response_not_object")
        if "error_code" in payload:
            try:
                error_code = _remote_error_code(payload.get("error_code"))
            except WeiboApiError:
                if response.status_code in {401, 403, 429}:
                    error_code = -1
                else:
                    raise
            raise WeiboApiRejected(
                action,
                error_code,
                str(payload.get("error") or ""),
                http_status=response.status_code,
            )
        if not 200 <= response.status_code < 300:
            raise WeiboApiRejected(
                action,
                -1,
                f"HTTP {response.status_code}",
                http_status=response.status_code,
            )
        return payload

    async def check_identity(self) -> str:
        try:
            payload = await self._json_request(
                "GET", f"{API_BASE}/account/get_uid.json", action="identity"
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiError("weibo_oauth_identity_request_failed") from exc
        return _uid(payload.get("uid"))

    async def resolve_status_id(self, canonical_id: str) -> str:
        value = str(canonical_id or "").strip()
        if _is_numeric_status_id(value):
            return value
        if not MBLOG_ID_PATTERN.fullmatch(value):
            raise WeiboApiError("weibo_status_id_invalid")
        try:
            payload = await self._json_request(
                "GET",
                f"{API_BASE}/statuses/queryid.json",
                action="resolve_status",
                params={"mid": value, "type": 1, "isBase62": 1},
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiError("weibo_status_id_resolution_failed") from exc
        return _status_id(payload.get("id"))

    async def resolve_user_uid(self, handle: str) -> str:
        screen_name = str(handle or "").strip()
        if not screen_name.startswith("@") or len(screen_name) <= 1:
            raise WeiboApiError("weibo_follow_handle_invalid")
        screen_name = screen_name[1:]
        try:
            payload = await self._json_request(
                "GET",
                f"{API_BASE}/users/show.json",
                action="resolve_follow_target",
                params={"screen_name": screen_name},
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiError("weibo_follow_identity_resolution_failed") from exc
        if str(payload.get("screen_name") or "") != screen_name:
            raise WeiboApiError("weibo_follow_identity_mismatch")
        return _uid(payload.get("idstr") or payload.get("id"))

    async def _post_action(
        self,
        request: WeiboMutationRequest,
        *,
        operation_key: str,
    ) -> WeiboActionReceipt:
        action = request.action
        target_id = request.target_id
        self._require_capability(action)
        key = _operation_key(operation_key)
        if key in self._started_operation_keys:
            raise WeiboDuplicateOperation(key)
        self._started_operation_keys.add(key)
        request_body = dict(request.data)
        try:
            payload = await self._json_request(
                "POST",
                request.endpoint,
                action=action,
                data=request_body,
            )
        except asyncio.CancelledError as exc:
            raise WeiboApiActionOutcomeUnknown(action, "cancelled") from exc
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiActionOutcomeUnknown(action, type(exc).__name__) from exc
        except WeiboApiRejected as exc:
            if exc.confirmed_no_effect:
                raise
            raise WeiboApiActionOutcomeUnknown(
                action,
                (
                    f"remote_error_code_{exc.error_code}"
                    if exc.error_code >= 0
                    else f"http_status_{exc.http_status or 'unknown'}"
                ),
            ) from exc
        except WeiboApiError as exc:
            raise WeiboApiActionOutcomeUnknown(action, type(exc).__name__) from exc
        try:
            remote_id = self._validate_receipt(
                action, target_id, payload, request_body=request_body
            )
        except WeiboApiError as exc:
            raise WeiboApiActionOutcomeUnknown(
                action, "receipt_unverified"
            ) from exc
        return WeiboActionReceipt(
            action=action,
            target_id=target_id,
            remote_id=remote_id,
            operation_key=key,
            request_payload_hash=request.audit_spec_hash,
        )

    @staticmethod
    def _validate_receipt(
        action: str,
        target_id: str,
        payload: Mapping[str, Any],
        *,
        request_body: Mapping[str, Any],
    ) -> str:
        if action == "commented":
            if _id_from(payload.get("status")) != target_id:
                raise WeiboApiError("weibo_comment_receipt_target_mismatch")
            if payload.get("text") != request_body.get("comment"):
                raise WeiboApiError("weibo_comment_receipt_text_mismatch")
            remote_id = _id_from(payload)
        elif action == "reposted":
            if _id_from(payload.get("retweeted_status")) != target_id:
                raise WeiboApiError("weibo_repost_receipt_target_mismatch")
            if "status" in request_body and payload.get("text") != request_body.get("status"):
                raise WeiboApiError("weibo_repost_receipt_text_mismatch")
            remote_id = _id_from(payload)
        elif action == "favorited":
            if _id_from(payload.get("status")) != target_id:
                raise WeiboApiError("weibo_favorite_receipt_target_mismatch")
            remote_id = _id_from(payload.get("status"))
        elif action == "liked":
            observed = _id_from(payload.get("status")) or _id_from(payload)
            if observed != target_id:
                raise WeiboApiError("weibo_like_receipt_target_mismatch")
            remote_id = observed
        elif action == "followed":
            observed = _id_from(payload)
            if observed != target_id:
                raise WeiboApiError("weibo_follow_receipt_target_mismatch")
            remote_id = observed
        else:
            raise WeiboApiError("weibo_action_invalid")
        if not remote_id:
            raise WeiboApiError("weibo_receipt_id_missing")
        return remote_id

    async def preflight_status(self, status_id: str) -> str:
        """Prove the numeric target exists before any ordered write begins."""

        target = _status_id(status_id)
        try:
            payload = await self._json_request(
                "GET",
                f"{API_BASE}/statuses/show.json",
                action="resolve_status",
                params={"id": target},
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiError("weibo_status_preflight_failed") from exc
        if _id_from(payload) != target:
            raise WeiboApiError("weibo_status_preflight_target_mismatch")
        return target

    async def comment(
        self,
        status_id: str,
        text: str,
        *,
        rip: str,
        operation_key: str,
    ) -> WeiboActionReceipt:
        request = build_weibo_mutation_request(
            "commented",
            status_id,
            payload={"text": text},
            rip=rip,
        )
        return await self._post_action(
            request,
            operation_key=operation_key,
        )

    async def repost(
        self,
        status_id: str,
        text: str | None = None,
        *,
        rip: str,
        operation_key: str,
    ) -> WeiboActionReceipt:
        payload = {"text": text} if text is not None else {}
        request = build_weibo_mutation_request(
            "reposted", status_id, payload=payload, rip=rip
        )
        return await self._post_action(
            request,
            operation_key=operation_key,
        )

    async def favorite(
        self, status_id: str, *, operation_key: str
    ) -> WeiboActionReceipt:
        request = build_weibo_mutation_request("favorited", status_id)
        return await self._post_action(
            request,
            operation_key=operation_key,
        )

    async def like(
        self, status_id: str, *, operation_key: str
    ) -> WeiboActionReceipt:
        request = build_weibo_mutation_request("liked", status_id)
        return await self._post_action(
            request,
            operation_key=operation_key,
        )

    async def follow(
        self, target_uid: str, *, rip: str, operation_key: str
    ) -> WeiboActionReceipt:
        request = build_weibo_mutation_request(
            "followed", target_uid, rip=rip
        )
        return await self._post_action(
            request,
            operation_key=operation_key,
        )


class WeiboOAuthIdentityClient:
    """Read-only OAuth identity client used before capability evidence exists."""

    def __init__(
        self,
        access_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: AsyncHttpClient | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        if http_client is not None and transport is not None:
            raise WeiboApiError("weibo_http_client_injection_ambiguous")
        self._access_token = _required_token(access_token)
        self._owns_client = http_client is None
        self._client: AsyncHttpClient = http_client or httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "WeiboOAuthIdentityClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def check_identity(self) -> str:
        try:
            response = await self._client.request(
                "GET",
                f"{API_BASE}/account/get_uid.json",
                params={"access_token": self._access_token},
                headers=None,
            )
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            payload = response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise WeiboApiError("weibo_oauth_identity_request_failed") from exc
        except Exception as exc:
            raise WeiboApiError("weibo_oauth_identity_response_invalid") from exc
        if not isinstance(payload, dict):
            raise WeiboApiError("weibo_oauth_identity_response_invalid")
        if "error_code" in payload:
            try:
                error_code = _remote_error_code(payload.get("error_code"))
            except WeiboApiError:
                error_code = -1
            raise WeiboApiRejected(
                "identity",
                error_code,
                str(payload.get("error") or ""),
                http_status=response.status_code,
            )
        if not 200 <= response.status_code < 300:
            raise WeiboApiError("weibo_oauth_identity_request_failed")
        return _uid(payload.get("uid"))
