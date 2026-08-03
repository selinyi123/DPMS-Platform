import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from app.xiaohongshu_target_pursuit_daemon import (
    PursuitDaemonConfig,
    PursuitDaemonState,
    SOURCE_LIST_PATH,
    SOURCE_SCAN_PATH,
    run_round,
)


def config(**overrides) -> PursuitDaemonConfig:
    values = {
        "core_api_url": "http://core-api:8000",
        "admin_token": "test-admin-token",
        "enabled": True,
        "platform_allowlist": frozenset({"xiaohongshu"}),
        "cadence_seconds": 1800.0,
        "poll_interval_seconds": 60.0,
        "source_limit": 100,
        "scan_limit": 10,
        "failure_limit": 2,
        "max_candidates": 20,
        "request_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return PursuitDaemonConfig(**values)


def source(
    source_id: int,
    *,
    last_scan_at=None,
    source_type="keyword",
    status="succeeded",
):
    return {
        "id": source_id,
        "source_type": source_type,
        "source_value": "抽奖",
        "active": True,
        "last_scan_at": last_scan_at,
        "status": status,
    }


class XiaohongshuTargetPursuitDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_daemon_makes_no_http_requests(self):
        def handler(request):  # pragma: no cover - assertion is the behavior
            raise AssertionError(f"unexpected request: {request.url}")

        async with httpx.AsyncClient(
            base_url="http://core-api:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            summary = await run_round(
                client,
                config(enabled=False),
                PursuitDaemonState(),
            )

        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["scanned"], 0)

    async def test_recent_last_scan_is_throttled_by_cadence(self):
        now = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.path, SOURCE_LIST_PATH)
            return httpx.Response(
                200,
                json={
                    "items": [
                        source(
                            11,
                            last_scan_at=(now - timedelta(minutes=5)).isoformat(),
                        )
                    ]
                },
            )

        async with httpx.AsyncClient(
            base_url="http://core-api:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            summary = await run_round(
                client,
                config(cadence_seconds=1800.0),
                PursuitDaemonState(),
                now=now,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(summary["selected"], 0)
        self.assertEqual(summary["deferred"], 1)

    async def test_failure_limit_prevents_repeat_scan(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == SOURCE_LIST_PATH:
                return httpx.Response(200, json={"items": [source(12)]})
            self.assertEqual(request.url.path, SOURCE_SCAN_PATH)
            return httpx.Response(502, json={"detail": {"code": "failed"}})

        state = PursuitDaemonState()
        async with httpx.AsyncClient(
            base_url="http://core-api:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            first = await run_round(
                client,
                config(failure_limit=1),
                state,
            )
            second = await run_round(
                client,
                config(failure_limit=1),
                state,
            )

        self.assertEqual(first["failures"], 1)
        self.assertEqual(second["selected"], 0)
        self.assertEqual(
            [request.url.path for request in requests].count(SOURCE_SCAN_PATH),
            1,
        )

    async def test_success_scans_only_bounded_due_source(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == SOURCE_LIST_PATH:
                self.assertEqual(request.url.params["active"], "true")
                self.assertEqual(request.url.params["limit"], "2")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            source(21),
                            source(22, source_type="author_profile"),
                            source(23, source_type="offline_search_result"),
                        ]
                    },
                )
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, SOURCE_SCAN_PATH)
            self.assertEqual(
                json.loads(request.content),
                {"source_id": 21, "max_candidates": 7},
            )
            return httpx.Response(200, json={"status": "scanned"})

        async with httpx.AsyncClient(
            base_url="http://core-api:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            summary = await run_round(
                client,
                config(source_limit=2, scan_limit=1, max_candidates=7),
                PursuitDaemonState(),
            )

        self.assertEqual(summary["fetched"], 3)
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("decision", requests[1].url.path)


if __name__ == "__main__":
    unittest.main()
