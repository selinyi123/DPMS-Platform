from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from device_agent.calibration import CalibrationManifest
from device_agent.engine import DeviceActionEngine
from device_agent.guard import RateLimitPolicy
from device_agent.http_service import (
    LOOPBACK_HOST,
    DeviceAgentHttpService,
    create_http_server,
)


DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"
TARGET_HASH = "a" * 64
WRONG_TARGET_HASH = "b" * 64
AUTHOR_HANDLE = "@exact-author"
TOKEN = "test-only-token-0123456789-abcdef"
MANIFEST_SHA256 = "c" * 64


def node(
    *,
    text: str = "",
    resource_id: str = "",
    content_desc: str = "",
    bounds: str = "[10,20][110,120]",
    enabled: bool = True,
    clickable: bool = True,
) -> dict[str, str]:
    return {
        "text": text,
        "resource-id": resource_id,
        "content-desc": content_desc,
        "bounds": bounds,
        "enabled": str(enabled).lower(),
        "clickable": str(clickable).lower(),
    }


def hierarchy(*nodes: dict[str, str]) -> str:
    root = ET.Element("hierarchy", {"rotation": "0"})
    for attributes in nodes:
        ET.SubElement(root, "node", attributes)
    return ET.tostring(root, encoding="unicode")


def manifest_mapping() -> dict[str, object]:
    return {
        "version": 1,
        "package": DOUYIN_PACKAGE,
        "risk_texts": ["captcha", "face verification", "account risk"],
        "settle_seconds": 0,
        "target_markers": {
            TARGET_HASH: {
                "markers": [{"resource_id": "note-id", "text": "note-123"}],
                "author_handle": AUTHOR_HANDLE,
                "follow_markers": [
                    {"resource_id": "author-handle", "text": AUTHOR_HANDLE}
                ],
            }
        },
        "actions": {
            "follow": {
                "trigger": {"resource_id": "follow", "text": "Follow"},
                "done": [{"resource_id": "follow", "text": "Following"}],
            },
            "like": {
                "trigger": {"resource_id": "like", "content_desc": "Like"},
                "done": [{"resource_id": "like", "content_desc": "Liked"}],
            },
            "comment": {
                "trigger": {"resource_id": "comment-input"},
                "typed": {"resource_id": "comment-input", "text": "$comment"},
                "submit": {"resource_id": "comment-submit", "text": "Send"},
                "done": [{"resource_id": "comment-item", "text": "$comment"}],
            },
            "favorite": {
                "trigger": {"resource_id": "favorite", "content_desc": "Favorite"},
                "done": [
                    {"resource_id": "favorite", "content_desc": "Favorited"}
                ],
            },
        },
    }


def identity_nodes() -> tuple[dict[str, str], dict[str, str]]:
    return (
        node(resource_id="note-id", text="note-123", clickable=False),
        node(resource_id="author-handle", text=AUTHOR_HANDLE, clickable=False),
    )


class FakeAdb:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.index = 0
        self.serial = "FAKE-DEVICE-SERIAL"
        self.operations: list[tuple[object, ...]] = []

    def foreground_package(self) -> str:
        return DOUYIN_PACKAGE

    def dump_ui_xml(self) -> str:
        return self.frames[self.index]

    def tap(self, x: int, y: int) -> None:
        self.operations.append(("tap", x, y))
        if self.index + 1 < len(self.frames):
            self.index += 1

    def input_text(self, value: str) -> None:
        self.operations.append(("input_text", value))
        if self.index + 1 < len(self.frames):
            self.index += 1

    def health(self) -> bool:
        return True


class DeviceAgentHttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.manifest = CalibrationManifest.from_mapping(manifest_mapping())

    def engine(self, adb: FakeAdb) -> DeviceActionEngine:
        return DeviceActionEngine(
            adb=adb,
            manifest=self.manifest,
            account_id="local-test-account",
            state_dir=self.state_dir,
            rate_policy=RateLimitPolicy(
                min_interval_seconds=0,
                max_actions_per_hour=100,
                unknown_cooldown_seconds=0,
                blocked_cooldown_seconds=0,
            ),
            sleeper=lambda _seconds: None,
        )

    def start_service(
        self,
        adb: FakeAdb,
        *,
        operation_timeout_seconds: float = 5,
        request_body_limit_bytes: int = 16 * 1024,
    ) -> tuple[DeviceAgentHttpService, int]:
        service = DeviceAgentHttpService(
            engine=self.engine(adb),
            bearer_token=TOKEN,
            manifest_sha256=MANIFEST_SHA256,
            operation_timeout_seconds=operation_timeout_seconds,
            request_body_limit_bytes=request_body_limit_bytes,
        )
        server = create_http_server(service=service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup() -> None:
            server.shutdown()
            server.server_close()
            service.close()
            thread.join(timeout=2)

        self.addCleanup(cleanup)
        return service, int(server.server_address[1])

    @staticmethod
    def request(
        port: int,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        token: str | None = TOKEN,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            body = raw_body
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            decoded = json.loads(response.read().decode("utf-8"))
            return response.status, decoded
        finally:
            connection.close()

    def test_startup_is_loopback_only_and_does_not_touch_device(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes())])
        service, port = self.start_service(adb)
        self.assertGreater(port, 0)
        self.assertEqual(adb.operations, [])
        with self.assertRaises(ValueError):
            # The concrete server type itself rejects every non-loopback bind.
            from device_agent.http_service import DeviceAgentHttpServer

            DeviceAgentHttpServer(("0.0.0.0", 0), service)

    def test_health_requires_auth_and_contains_only_hashed_identity(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes())])
        _, port = self.start_service(adb)
        status, unauthorized = self.request(port, "GET", "/health", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"], "unauthorized")

        status, health = self.request(port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["ready"])
        self.assertEqual(health["listen_host"], LOOPBACK_HOST)
        self.assertEqual(health["package"], DOUYIN_PACKAGE)
        self.assertEqual(health["manifest_sha256"], MANIFEST_SHA256)
        self.assertEqual(
            health["device_serial_sha256"],
            hashlib.sha256(adb.serial.encode()).hexdigest(),
        )
        self.assertEqual(
            health["agent_id"],
            hashlib.sha256(
                (
                    "dpms-device-agent-v1:"
                    f"{MANIFEST_SHA256}:{health['device_serial_sha256']}"
                ).encode()
            ).hexdigest(),
        )
        serialized = json.dumps(health)
        self.assertNotIn(adb.serial, serialized)
        self.assertNotIn("local-test-account", serialized)
        self.assertNotIn(TOKEN, serialized)
        self.assertEqual(
            health["supported_actions"], ["comment", "favorite", "follow", "like"]
        )

    def test_snapshot_verifies_bound_target_and_follow_identity_without_action(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes(), node(resource_id="like"))])
        service, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/snapshot",
            {
                "target_hash": TARGET_HASH,
                "follow_target_handle": AUTHOR_HANDLE,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["snapshot"]["target_identity_verified"])
        self.assertTrue(response["snapshot"]["follow_target_verified"])
        self.assertFalse(response["snapshot"]["blocked"])
        self.assertEqual(response["snapshot"]["target_hash"], TARGET_HASH)
        self.assertEqual(response["snapshot"]["agent_id"], service.agent_id)
        self.assertIn("account_id_sha256", response["snapshot"])
        self.assertEqual(adb.operations, [])

    def test_execute_returns_before_result_after_and_preserves_exact_comment(self) -> None:
        comment = "  exact comment text  "
        adb = FakeAdb(
            [
                hierarchy(*identity_nodes(), node(resource_id="comment-input")),
                hierarchy(*identity_nodes(), node(resource_id="comment-input")),
                hierarchy(
                    *identity_nodes(),
                    node(resource_id="comment-input", text=comment),
                    node(resource_id="comment-submit", text="Send"),
                ),
                hierarchy(*identity_nodes(), node(resource_id="comment-item", text=comment)),
            ]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-123:comment",
                "target_hash": TARGET_HASH,
                "action": "comment",
                "comment": comment,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["request_id"], "task-123:comment")
        self.assertEqual(response["target_hash"], TARGET_HASH)
        self.assertEqual(response["action"], "comment")
        self.assertTrue(response["before_snapshot"]["target_identity_verified"])
        self.assertTrue(
            response["before_snapshot"]["action_states"]["comment"]["calibrated"]
        )
        self.assertEqual(response["result"]["status"], "succeeded")
        self.assertTrue(response["after_snapshot"]["target_identity_verified"])
        self.assertIn(("input_text", comment), adb.operations)

    def test_unknown_target_hash_fails_before_mutation(self) -> None:
        adb = FakeAdb(
            [hierarchy(*identity_nodes(), node(resource_id="like", content_desc="Like"))]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-wrong-target",
                "target_hash": WRONG_TARGET_HASH,
                "action": "like",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["before_snapshot"]["target_identity_verified"])
        self.assertEqual(response["result"]["status"], "blocked")
        self.assertEqual(response["result"]["reason"], "unknown_target_hash")
        self.assertFalse(response["result"]["mutation_attempted"])
        self.assertEqual(adb.operations, [])

    def test_wrong_follow_handle_fails_before_mutation(self) -> None:
        adb = FakeAdb(
            [hierarchy(*identity_nodes(), node(resource_id="follow", text="Follow"))]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-wrong-author",
                "target_hash": TARGET_HASH,
                "action": "follow",
                "follow_target_handle": "@different-author",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["before_snapshot"]["follow_target_verified"])
        self.assertEqual(
            response["result"]["reason"], "follow_target_unverified_before_action"
        )
        self.assertFalse(response["result"]["mutation_attempted"])
        self.assertEqual(adb.operations, [])

    def test_ambiguous_target_marker_fails_before_mutation(self) -> None:
        marker, author = identity_nodes()
        adb = FakeAdb(
            [
                hierarchy(
                    marker,
                    dict(marker, bounds="[200,20][300,120]"),
                    author,
                    node(resource_id="favorite", content_desc="Favorite"),
                )
            ]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-ambiguous-target",
                "target_hash": TARGET_HASH,
                "action": "favorite",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            response["result"]["reason"],
            "target_identity_unverified_before_action",
        )
        self.assertFalse(response["result"]["mutation_attempted"])
        self.assertEqual(adb.operations, [])

    def test_risk_text_is_preserved_as_blocked_without_mutation(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(
                    *identity_nodes(),
                    node(resource_id="like", content_desc="Like"),
                    node(text="captcha", clickable=False),
                )
            ]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-risk",
                "target_hash": TARGET_HASH,
                "action": "like",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["before_snapshot"]["blocked"])
        self.assertEqual(response["result"]["status"], "blocked")
        self.assertIn("risk_text_detected", response["result"]["reason"])
        self.assertFalse(response["result"]["mutation_attempted"])
        self.assertEqual(adb.operations, [])

    def test_target_drift_after_click_is_unknown_and_halts(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(
                    *identity_nodes(), node(resource_id="like", content_desc="Like")
                ),
                hierarchy(node(resource_id="like", content_desc="Liked")),
            ]
        )
        _, port = self.start_service(adb)
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-target-drift",
                "target_hash": TARGET_HASH,
                "action": "like",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["status"], "unknown")
        self.assertEqual(
            response["result"]["reason"],
            "target_identity_unverified_after_action",
        )
        self.assertTrue(response["result"]["mutation_attempted"])
        self.assertTrue(response["result"]["halt"])
        self.assertFalse(response["after_snapshot"]["target_identity_verified"])
        self.assertEqual(adb.operations, [("tap", 60, 70)])

    def test_single_task_lock_rejects_concurrent_request(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes())])
        service, port = self.start_service(adb)
        original_snapshot = service.engine.snapshot
        entered = threading.Event()
        release = threading.Event()

        def blocking_snapshot(**kwargs):
            entered.set()
            release.wait(timeout=5)
            return original_snapshot(**kwargs)

        service.engine.snapshot = blocking_snapshot  # type: ignore[method-assign]
        first_response: list[tuple[int, dict[str, Any]]] = []
        first = threading.Thread(
            target=lambda: first_response.append(
                self.request(
                    port,
                    "POST",
                    "/v1/snapshot",
                    {"target_hash": TARGET_HASH},
                )
            )
        )
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        status, response = self.request(
            port, "POST", "/v1/snapshot", {"target_hash": TARGET_HASH}
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "device_busy")
        release.set()
        first.join(timeout=3)
        self.assertEqual(first_response[0][0], 200)

    def test_request_body_limit_is_enforced_before_json_processing(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes())])
        _, port = self.start_service(adb, request_body_limit_bytes=1024)
        status, response = self.request(
            port,
            "POST",
            "/v1/snapshot",
            raw_body=b"{" + b" " * 2048 + b"}",
        )
        self.assertEqual(status, 413)
        self.assertEqual(response["error"], "body_too_large")
        self.assertEqual(adb.operations, [])

    def test_total_timeout_returns_unknown_and_keeps_task_busy_until_worker_stops(self) -> None:
        adb = FakeAdb([hierarchy(*identity_nodes())])
        service, port = self.start_service(adb, operation_timeout_seconds=1)
        original_snapshot = service.engine.snapshot
        release = threading.Event()

        def slow_snapshot(**kwargs):
            release.wait(timeout=3)
            return original_snapshot(**kwargs)

        service.engine.snapshot = slow_snapshot  # type: ignore[method-assign]
        started = time.monotonic()
        status, response = self.request(
            port,
            "POST",
            "/v1/execute",
            {
                "request_id": "task-timeout",
                "target_hash": TARGET_HASH,
                "action": "like",
            },
        )
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(response["result"]["status"], "unknown")
        self.assertTrue(response["result"]["halt"])
        self.assertTrue(service.health()["busy"])
        release.set()
        deadline = time.monotonic() + 2
        while service.health()["busy"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(service.health()["busy"])
        self.assertEqual(adb.operations, [])


if __name__ == "__main__":
    unittest.main()
