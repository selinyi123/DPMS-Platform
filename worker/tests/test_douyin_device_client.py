"""HTTP-contract tests for the Docker Worker to Windows device-agent bridge."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import account_calibrator
from app.douyin_device_client import (
    DouyinDeviceActionOutcomeUnknown,
    DouyinDeviceClient,
    DouyinDeviceClientError,
)
from shared.douyin_device_contract import DOUYIN_DEVICE_CALIBRATION_CHECK_URL


TARGET_HASH = "a" * 64
TOKEN = "test-only-token-0123456789-abcdef"
IDENTITY = {
    "agent_id": "b" * 64,
    "manifest_sha256": "c" * 64,
    "device_serial_sha256": "d" * 64,
    "account_id_sha256": "e" * 64,
}
REQUIRED_ACTIONS = ("followed", "liked", "commented", "favorited")


def snapshot(*, follow_verified: bool | None) -> dict:
    return {
        **IDENTITY,
        "target_hash": TARGET_HASH,
        "package": "com.ss.android.ugc.aweme",
        "package_ok": True,
        "blocked": False,
        "reason": "ok",
        "risk_texts": [],
        "node_count": 12,
        "xml_sha256": "f" * 64,
        "target_identity_verified": True,
        "follow_target_verified": follow_verified,
        "action_states": {
            action: {
                "trigger_matches": 1,
                "done": False,
                "exact_trigger": True,
                "calibrated": True,
            }
            for action in ("follow", "like", "comment", "favorite")
        },
        "observed_at": 1.0,
    }


class DouyinDeviceClientValidationTests(unittest.TestCase):
    def test_production_url_and_token_fail_closed(self):
        with self.assertRaisesRegex(
            DouyinDeviceClientError, "douyin_device_agent_url_invalid"
        ):
            DouyinDeviceClient(
                base_url="http://127.0.0.1:8765", token=TOKEN
            )
        with self.assertRaisesRegex(
            DouyinDeviceClientError, "douyin_device_agent_token_invalid"
        ):
            DouyinDeviceClient(
                base_url="http://host.docker.internal:8765", token="short"
            )


class DouyinDeviceCalibrationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_purpose_bound_device_envelope_selects_read_only_health_route(self):
        credential = json.dumps(
            {
                "contract_version": 1,
                "credential_kind": "device_agent",
                "device_agent": IDENTITY,
            }
        )
        with (
            patch.object(
                account_calibrator.database,
                "fetch_one",
                new=AsyncMock(
                    return_value={"encrypted_credential": b"bound-envelope"}
                ),
            ),
            patch.object(
                account_calibrator.cookie_vault,
                "decrypt_strict",
                return_value=credential,
            ),
        ):
            kind = await account_calibrator.resolve_douyin_calibration_kind(
                {
                    "account_id": 7,
                    "check_url": DOUYIN_DEVICE_CALIBRATION_CHECK_URL,
                }
            )

        self.assertEqual("device_agent", kind)


class DouyinDeviceClientHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_and_snapshot_use_host_service_strict_schema(self):
        requests: list[tuple[str, dict | None]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content) if request.content else None
            requests.append((request.url.path, payload))
            self.assertEqual(
                request.headers.get("Authorization"), f"Bearer {TOKEN}"
            )
            if request.url.path == "/health":
                return httpx.Response(
                    200,
                    json={
                        **IDENTITY,
                        "version": 1,
                        "status": "ok",
                        "ready": True,
                        "healthy": True,
                        "busy": False,
                        "listen_host": "127.0.0.1",
                        "package": "com.ss.android.ugc.aweme",
                        "supported_actions": [
                            "comment",
                            "favorite",
                            "follow",
                            "like",
                        ],
                        "observed_at": 1.0,
                    },
                )
            return httpx.Response(
                200, json={"snapshot": snapshot(follow_verified=True)}
            )

        client = DouyinDeviceClient(
            base_url="http://127.0.0.1:8765",
            token=TOKEN,
            transport=httpx.MockTransport(handler),
            allow_loopback=True,
        )
        try:
            await client.health(
                expected_identity=IDENTITY,
                required_actions=REQUIRED_ACTIONS,
            )
            result = await client.snapshot(
                operation_key="probe-1",
                target_hash=TARGET_HASH,
                required_actions=REQUIRED_ACTIONS,
                comment="精确评论文案",
                follow_target_handle="@exact-author",
                expected_identity=IDENTITY,
            )
        finally:
            await client.aclose()

        self.assertTrue(result["target_identity_verified"])
        self.assertEqual(
            requests,
            [
                ("/health", None),
                (
                    "/v1/snapshot",
                    {
                        "target_hash": TARGET_HASH,
                        "comment": "精确评论文案",
                        "follow_target_handle": "@exact-author",
                    },
                ),
            ],
        )

    async def test_execute_sends_only_current_action_fields(self):
        request_payloads: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            request_payloads.append(payload)
            follow_verified = payload["action"] == "follow"
            return httpx.Response(
                200,
                json={
                    "request_id": payload["request_id"],
                    "target_hash": payload["target_hash"],
                    "action": payload["action"],
                    "before_snapshot": snapshot(
                        follow_verified=True if follow_verified else None
                    ),
                    "result": {
                        "status": "succeeded",
                        "action": payload["action"],
                        "reason": "confirmed",
                        "outcome_known": True,
                        "halt": False,
                        "mutation_attempted": True,
                        "before_done": False,
                        "after_done": True,
                        "observed_at": 2.0,
                        "retry_after_seconds": 0.0,
                    },
                    "after_snapshot": snapshot(
                        follow_verified=True if follow_verified else None
                    ),
                },
            )

        client = DouyinDeviceClient(
            base_url="http://127.0.0.1:8765",
            token=TOKEN,
            transport=httpx.MockTransport(handler),
            allow_loopback=True,
        )
        try:
            await client.execute(
                operation_key="intent-1:1",
                target_hash=TARGET_HASH,
                action="liked",
                comment=None,
                follow_target_handle="@exact-author",
                required_actions=REQUIRED_ACTIONS,
                expected_identity=IDENTITY,
            )
            await client.execute(
                operation_key="intent-2:1",
                target_hash=TARGET_HASH,
                action="commented",
                comment="精确评论文案",
                follow_target_handle="@exact-author",
                required_actions=REQUIRED_ACTIONS,
                expected_identity=IDENTITY,
            )
        finally:
            await client.aclose()

        self.assertEqual(
            request_payloads,
            [
                {
                    "request_id": "intent-1:1",
                    "target_hash": TARGET_HASH,
                    "action": "like",
                },
                {
                    "request_id": "intent-2:1",
                    "target_hash": TARGET_HASH,
                    "action": "comment",
                    "comment": "精确评论文案",
                },
            ],
        )

    async def test_non_200_execute_is_unknown_not_retryable_success(self):
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(504, json={"error": "operation_timeout"})

        client = DouyinDeviceClient(
            base_url="http://127.0.0.1:8765",
            token=TOKEN,
            transport=httpx.MockTransport(handler),
            allow_loopback=True,
        )
        try:
            with self.assertRaises(DouyinDeviceActionOutcomeUnknown) as caught:
                await client.execute(
                    operation_key="intent-3:1",
                    target_hash=TARGET_HASH,
                    action="liked",
                    comment=None,
                    follow_target_handle="",
                    required_actions=("liked",),
                    expected_identity=IDENTITY,
                )
        finally:
            await client.aclose()

        self.assertEqual(caught.exception.reason, "device_agent_http_504")


if __name__ == "__main__":
    unittest.main()
