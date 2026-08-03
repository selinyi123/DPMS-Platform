from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .calibration import SUPPORTED_ACTIONS, TARGET_HASH_RE
from .engine import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    DeviceActionEngine,
    SnapshotReport,
)


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_BODY_LIMIT_BYTES = 16 * 1024
DEFAULT_OPERATION_TIMEOUT_SECONDS = 60.0
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class HttpRequestError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


class ServiceBusyError(RuntimeError):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_json",
            "request body must be one strict UTF-8 JSON object",
        ) from exc
    if not isinstance(value, dict):
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_json",
            "request body must be a JSON object",
        )
    return value


def _validate_exact_fields(
    payload: Mapping[str, Any], *, required: set[str], optional: set[str]
) -> None:
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing:
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            "missing_fields",
            f"missing required fields: {sorted(missing)}",
        )
    if unknown:
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            "unknown_fields",
            f"unknown fields: {sorted(unknown)}",
        )


def _target_hash(payload: Mapping[str, Any]) -> str:
    value = payload.get("target_hash")
    if not isinstance(value, str) or not TARGET_HASH_RE.fullmatch(value):
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            "invalid_target_hash",
            "target_hash must be lowercase 64-character hex",
        )
    return value


def _optional_exact_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    maximum: int,
    allow_outer_whitespace: bool = False,
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or (not allow_outer_whitespace and value != value.strip())
        or len(value) > maximum
    ):
        raise HttpRequestError(
            HTTPStatus.BAD_REQUEST,
            f"invalid_{field}",
            f"{field} must be an exact non-empty string of at most {maximum} characters",
        )
    return value


class DeviceAgentHttpService:
    """Authenticated, loopback-only facade over one device action engine.

    Construction and server startup do not call ADB and do not execute an
    action.  Snapshot and action requests are serialized by one in-process
    task lock.  The engine retains its separate account-scoped file lock.
    """

    def __init__(
        self,
        *,
        engine: DeviceActionEngine,
        bearer_token: str,
        manifest_sha256: str,
        request_body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES,
        operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(bearer_token, str) or len(bearer_token) < 32:
            raise ValueError("bearer token must contain at least 32 characters")
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise ValueError("manifest_sha256 must be lowercase 64-character hex")
        if not (1024 <= request_body_limit_bytes <= 1024 * 1024):
            raise ValueError("request body limit must be between 1024 and 1048576 bytes")
        if not (1 <= operation_timeout_seconds <= 300):
            raise ValueError("operation timeout must be between 1 and 300 seconds")
        if not engine.manifest.target_markers:
            raise ValueError("HTTP service requires non-empty manifest target_markers")

        self.engine = engine
        self._bearer_token = bearer_token
        self.manifest_sha256 = manifest_sha256
        self.request_body_limit_bytes = int(request_body_limit_bytes)
        self.operation_timeout_seconds = float(operation_timeout_seconds)
        self.clock = clock
        self.device_serial_sha256 = _sha256(engine.adb.serial)
        self.account_id_sha256 = _sha256(engine.account_id)
        self.agent_id = _sha256(
            "dpms-device-agent-v1:"
            f"{self.manifest_sha256}:{self.device_serial_sha256}"
        )
        self._task_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dpms-device-operation"
        )

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith(
            "Bearer "
        ):
            return False
        supplied = authorization[len("Bearer ") :]
        return bool(supplied) and hmac.compare_digest(supplied, self._bearer_token)

    def health(self) -> dict[str, object]:
        try:
            adb_healthy = bool(self.engine.adb.health())
        except Exception:  # A transport failure is a redacted health state.
            adb_healthy = False
        busy = self._task_lock.locked()
        return {
            "version": 1,
            "status": "ok",
            "ready": adb_healthy and not busy,
            "healthy": adb_healthy,
            "busy": busy,
            "listen_host": LOOPBACK_HOST,
            "package": self.engine.manifest.package,
            "agent_id": self.agent_id,
            "manifest_sha256": self.manifest_sha256,
            "device_serial_sha256": self.device_serial_sha256,
            "account_id_sha256": self.account_id_sha256,
            "supported_actions": sorted(SUPPORTED_ACTIONS),
            "observed_at": float(self.clock()),
        }

    def _enrich_snapshot(
        self, report: SnapshotReport, *, target_hash: str
    ) -> dict[str, object]:
        payload = report.to_dict()
        payload.update(
            {
                "target_hash": target_hash,
                "agent_id": self.agent_id,
                "manifest_sha256": self.manifest_sha256,
                "device_serial_sha256": self.device_serial_sha256,
                "account_id_sha256": self.account_id_sha256,
            }
        )
        action_states = payload.get("action_states")
        if isinstance(action_states, dict):
            for action_name, state in action_states.items():
                if not isinstance(state, dict):
                    continue
                calibration = self.engine.manifest.actions.get(action_name)
                state["calibrated"] = bool(
                    state.get("exact_trigger")
                    and calibration is not None
                    and calibration.done
                )
        return payload

    @staticmethod
    def _snapshot_arguments(payload: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
        _validate_exact_fields(
            payload,
            required={"target_hash"},
            optional={"comment", "follow_target_handle"},
        )
        target_hash = _target_hash(payload)
        comment = _optional_exact_string(
            payload, "comment", maximum=500, allow_outer_whitespace=True
        )
        follow_target_handle = _optional_exact_string(
            payload, "follow_target_handle", maximum=200
        )
        return target_hash, comment, follow_target_handle

    @staticmethod
    def _action_request(payload: Mapping[str, Any]) -> tuple[str, ActionRequest]:
        _validate_exact_fields(
            payload,
            required={"request_id", "target_hash", "action"},
            optional={"comment", "follow_target_handle"},
        )
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_request_id",
                "request_id must be 1-128 safe ASCII characters",
            )
        action = payload.get("action")
        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_action",
                "action must be follow, like, comment, or favorite",
            )
        target_hash = _target_hash(payload)
        comment = _optional_exact_string(
            payload, "comment", maximum=500, allow_outer_whitespace=True
        )
        follow_target_handle = _optional_exact_string(
            payload, "follow_target_handle", maximum=200
        )
        try:
            request = ActionRequest(
                action=action,
                comment=comment,
                target_hash=target_hash,
                follow_target_handle=follow_target_handle,
            )
        except ValueError as exc:
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_action_request", str(exc)
            ) from exc
        return request_id, request

    def _run_with_lock(
        self,
        operation: Callable[[], dict[str, object]],
        *,
        timeout_payload: Callable[[], dict[str, object]],
    ) -> tuple[int, dict[str, object]]:
        if not self._task_lock.acquire(blocking=False):
            raise ServiceBusyError("another device task is still active")

        def guarded() -> dict[str, object]:
            try:
                return operation()
            finally:
                self._task_lock.release()

        try:
            future = self._executor.submit(guarded)
        except Exception:
            self._task_lock.release()
            raise
        try:
            return HTTPStatus.OK, future.result(timeout=self.operation_timeout_seconds)
        except FutureTimeoutError:
            # Python cannot safely kill an executing thread.  The task lock is
            # intentionally retained by the worker until it actually stops;
            # the caller gets an unknown outcome and must quarantine/reconcile.
            return HTTPStatus.GATEWAY_TIMEOUT, timeout_payload()

    def snapshot(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, object]]:
        target_hash, comment, follow_target_handle = self._snapshot_arguments(payload)

        def operation() -> dict[str, object]:
            report = self.engine.snapshot(
                comment=comment,
                target_hash=target_hash,
                follow_target_handle=follow_target_handle,
            )
            return {
                "snapshot": self._enrich_snapshot(
                    report, target_hash=target_hash
                )
            }

        return self._run_with_lock(
            operation,
            timeout_payload=lambda: {
                "error": "operation_timeout",
                "message": "snapshot did not finish before the configured total timeout",
            },
        )

    def execute(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, object]]:
        request_id, request = self._action_request(payload)
        deadline_monotonic = time.monotonic() + self.operation_timeout_seconds
        evidence: dict[str, object] = {
            "before_snapshot": None,
            "after_snapshot": None,
        }

        def unknown_result(reason: str) -> dict[str, object]:
            return ActionResult(
                status=ActionStatus.UNKNOWN,
                action=request.action,
                reason=reason,
                outcome_known=False,
                halt=True,
                # The service cannot prove whether a mutation has begun when
                # the operation did not return normally.
                mutation_attempted=True,
                before_done=None,
                after_done=None,
                observed_at=float(self.clock()),
            ).to_dict()

        def operation() -> dict[str, object]:
            try:
                before = self.engine.snapshot(
                    comment=request.comment,
                    target_hash=request.target_hash,
                    follow_target_handle=request.follow_target_handle,
                )
                evidence["before_snapshot"] = self._enrich_snapshot(
                    before, target_hash=request.target_hash or ""
                )
                result = self.engine.execute(
                    request, deadline_monotonic=deadline_monotonic
                )
                evidence["result"] = result.to_dict()
                after = self.engine.snapshot(
                    comment=request.comment,
                    target_hash=request.target_hash,
                    follow_target_handle=request.follow_target_handle,
                )
                evidence["after_snapshot"] = self._enrich_snapshot(
                    after, target_hash=request.target_hash or ""
                )
            except Exception:
                evidence["result"] = unknown_result("service_internal_failure")
            return {
                "request_id": request_id,
                "target_hash": request.target_hash,
                "action": request.action,
                "before_snapshot": evidence["before_snapshot"],
                "result": evidence["result"],
                "after_snapshot": evidence["after_snapshot"],
            }

        def timeout_payload() -> dict[str, object]:
            return {
                "request_id": request_id,
                "target_hash": request.target_hash,
                "action": request.action,
                "before_snapshot": evidence.get("before_snapshot"),
                "result": unknown_result("service_total_timeout"),
                "after_snapshot": evidence.get("after_snapshot"),
                "timed_out": True,
            }

        return self._run_with_lock(operation, timeout_payload=timeout_payload)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class DeviceAgentHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: DeviceAgentHttpService,
    ) -> None:
        if server_address[0] != LOOPBACK_HOST:
            raise ValueError("device HTTP service may only bind to 127.0.0.1")
        self.device_service = service
        super().__init__(server_address, DeviceAgentRequestHandler)


class DeviceAgentRequestHandler(BaseHTTPRequestHandler):
    server_version = "DPMSDeviceAgent/1"
    sys_version = ""

    @property
    def service(self) -> DeviceAgentHttpService:
        return self.server.device_service  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not copy authentication headers, request identifiers, comments,
        # device identifiers, or target information into default stderr logs.
        return

    def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        if self.service.authorized(self.headers.get("Authorization")):
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "unauthorized", "message": "valid bearer authentication required"},
        )
        return False

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST,
                "unsupported_transfer_encoding",
                "Transfer-Encoding is not accepted",
            )
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise HttpRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as exc:
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_content_length", "invalid Content-Length"
            ) from exc
        if length <= 0:
            raise HttpRequestError(
                HTTPStatus.BAD_REQUEST, "empty_body", "a non-empty JSON body is required"
            )
        if length > self.service.request_body_limit_bytes:
            self.close_connection = True
            raise HttpRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "request body exceeds the configured limit",
            )
        return _strict_json_object(self.rfile.read(length))

    def _path(self) -> str:
        return urlsplit(self.path).path

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        if self._path() != "/health":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "endpoint not found"},
            )
            return
        self._send_json(HTTPStatus.OK, self.service.health())

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        try:
            payload = self._read_json_body()
            path = self._path()
            if path == "/v1/snapshot":
                status, response = self.service.snapshot(payload)
            elif path == "/v1/execute":
                status, response = self.service.execute(payload)
            else:
                raise HttpRequestError(
                    HTTPStatus.NOT_FOUND, "not_found", "endpoint not found"
                )
            self._send_json(status, response)
        except HttpRequestError as exc:
            self._send_json(
                exc.status, {"error": exc.code, "message": exc.message}
            )
        except ServiceBusyError:
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "device_busy", "message": "another device task is active"},
            )
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error", "message": "device service failed closed"},
            )

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            return
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method_not_allowed", "message": "method not allowed"},
        )

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def create_http_server(
    *, service: DeviceAgentHttpService, port: int
) -> DeviceAgentHttpServer:
    if not isinstance(port, int) or not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535")
    return DeviceAgentHttpServer((LOOPBACK_HOST, port), service)
