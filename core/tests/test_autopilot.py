import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.autopilot import (
    REAL_RUN_ACK_VALUE,
    AutopilotConfig,
    DispatchCandidate,
    bilibili_probe_decision,
    build_autopilot_heartbeat_payload,
    dispatch_candidate,
    latest_successful_shadow_account,
    platform_probe_decision,
    real_run_authorized,
    report_autopilot_heartbeat,
    run_round,
    select_candidates,
    write_health_heartbeat,
)


def config(**overrides) -> AutopilotConfig:
    values = {
        "core_api_url": "http://core-api:8000",
        "admin_token": "test-admin-token",
        "enabled": True,
        "real_run_enabled": False,
        "real_run_ack": "",
        "platform_allowlist": frozenset({"bilibili"}),
        "round_limit": 2,
        "scan_limit": 100,
        "failure_limit": 2,
        "poll_interval_seconds": 1.0,
        "request_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return AutopilotConfig(**values)


class CandidateSelectionTests(unittest.TestCase):
    def test_filters_platform_mode_active_runs_and_round_limit(self):
        payload = {
            "items": [
                {
                    "lottery_id": 1,
                    "platform": "weibo",
                    "recommended_mode": "dry_run",
                },
                {
                    "lottery_id": 2,
                    "platform": "bilibili",
                    "recommended_mode": "blocked",
                },
                {
                    "lottery_id": 3,
                    "platform": "bilibili",
                    "recommended_mode": "dry_run",
                    "active_runs": 1,
                },
                {
                    "lottery_id": 4,
                    "platform": "bilibili",
                    "recommended_mode": "dry_run",
                    "recommended_account": {"account_id": 41},
                },
                {
                    "lottery_id": 5,
                    "platform": "bilibili",
                    "recommended_mode": "shadow_run",
                },
                {
                    "lottery_id": 6,
                    "platform": "bilibili",
                    "recommended_mode": "dry_run",
                },
            ]
        }

        selected = select_candidates(payload, config())

        self.assertEqual(
            selected,
            [
                DispatchCandidate(4, "bilibili", "dry_run", 41),
                DispatchCandidate(5, "bilibili", "shadow_run", None),
            ],
        )

    def test_real_run_requires_all_three_acknowledgements(self):
        base = config(real_run_enabled=True, real_run_ack=REAL_RUN_ACK_VALUE)
        self.assertTrue(real_run_authorized(base))
        self.assertFalse(real_run_authorized(config(
            enabled=False,
            real_run_enabled=True,
            real_run_ack=REAL_RUN_ACK_VALUE,
        )))
        self.assertFalse(real_run_authorized(config(
            real_run_enabled=False,
            real_run_ack=REAL_RUN_ACK_VALUE,
        )))
        self.assertFalse(real_run_authorized(config(
            real_run_enabled=True,
            real_run_ack=REAL_RUN_ACK_VALUE + "-wrong",
        )))

        real_item = {
            "items": [
                {
                    "lottery_id": 7,
                    "platform": "bilibili",
                    "recommended_mode": "real_run",
                    "real_run_enabled": True,
                    "target_valid": True,
                    "breaker_allowed": True,
                    "execution_readiness_ready": True,
                }
            ]
        }
        self.assertEqual(len(select_candidates(real_item, base)), 1)
        self.assertEqual(
            select_candidates(
                real_item,
                config(real_run_enabled=True, real_run_ack=""),
            ),
            [],
        )

    def test_skips_completed_validation_rungs_and_failure_limit(self):
        payload = {
            "items": [
                {
                    "lottery_id": 10,
                    "platform": "bilibili",
                    "recommended_mode": "dry_run",
                    "dry_success": 1,
                },
                {
                    "lottery_id": 11,
                    "platform": "bilibili",
                    "recommended_mode": "shadow_run",
                    "shadow_success": 1,
                },
                {
                    "lottery_id": 12,
                    "platform": "bilibili",
                    "recommended_mode": "dry_run",
                    "failed_runs": 2,
                },
                {
                    "lottery_id": 13,
                    "platform": "bilibili",
                    "recommended_mode": "shadow_run",
                },
            ]
        }

        self.assertEqual(
            select_candidates(payload, config()),
            [DispatchCandidate(13, "bilibili", "shadow_run", None)],
        )

    def test_successful_bilibili_shadow_becomes_probe_work(self):
        selected = select_candidates(
            {
                "items": [
                    {
                        "lottery_id": 14,
                        "platform": "bilibili",
                        "recommended_mode": "shadow_run",
                        "shadow_success": 1,
                        "execution_readiness_ready": False,
                        "execution_readiness_blockers": [
                            "exact_execution_evidence_required"
                        ],
                    }
                ]
            },
            config(),
        )

        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].probe_required)
        self.assertEqual(selected[0].shadow_success, 1)

    def test_successful_xiaohongshu_shadow_becomes_probe_work(self):
        selected = select_candidates(
            {
                "items": [
                    {
                        "lottery_id": 15,
                        "platform": "xiaohongshu",
                        "recommended_mode": "shadow_run",
                        "shadow_success": 1,
                        "execution_readiness_ready": False,
                        "execution_readiness_blockers": [
                            "exact_execution_evidence_required"
                        ],
                        "execution_readiness": {
                            "action_plan_ready": True,
                            "rule_snapshot_ready": True,
                        },
                    }
                ]
            },
            config(platform_allowlist=frozenset({"xiaohongshu"})),
        )

        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].probe_required)
        self.assertEqual(selected[0].platform, "xiaohongshu")

    def test_plan_bound_platform_is_not_retried_before_review(self):
        selected = select_candidates(
            {
                "items": [
                    {
                        "lottery_id": 16,
                        "platform": "xiaohongshu",
                        "recommended_mode": "dry_run",
                        "execution_readiness": {
                            "action_plan_ready": False,
                            "rule_snapshot_ready": False,
                        },
                        "execution_readiness_blockers": [
                            "lottery_action_plan_v2_required"
                        ],
                    }
                ]
            },
            config(platform_allowlist=frozenset({"xiaohongshu"})),
        )

        self.assertEqual(selected, [])

    def test_probe_decision_prevents_duplicate_or_unbounded_probe(self):
        now = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            bilibili_probe_decision(
                [{"lottery_id": 1, "account_id": 2, "platform": "bilibili", "status": "queued"}],
                lottery_id=1,
                account_id=2,
                failure_limit=2,
            ),
            "wait",
        )
        self.assertEqual(
            bilibili_probe_decision(
                [{"lottery_id": 1, "account_id": 2, "platform": "bilibili", "status": "succeeded", "finished_at": now}],
                lottery_id=1,
                account_id=2,
                failure_limit=2,
            ),
            "succeeded",
        )
        self.assertEqual(
            bilibili_probe_decision(
                [
                    {"lottery_id": 1, "account_id": 2, "platform": "bilibili", "status": "failed"},
                    {"lottery_id": 1, "account_id": 2, "platform": "bilibili", "status": "failed"},
                ],
                lottery_id=1,
                account_id=2,
                failure_limit=2,
            ),
            "failure_limit",
        )
        self.assertEqual(
            platform_probe_decision(
                [
                    {
                        "lottery_id": 3,
                        "account_id": 4,
                        "platform": "xiaohongshu",
                        "status": "queued",
                    }
                ],
                lottery_id=3,
                account_id=4,
                platform="xiaohongshu",
                failure_limit=2,
            ),
            "wait",
        )

    def test_health_heartbeat_is_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health"
            write_health_heartbeat(path)
            self.assertTrue(path.is_file())

    def test_api_heartbeat_payload_exposes_no_secret_values(self):
        cfg = config(
            admin_token="do-not-expose-admin-token",
            real_run_enabled=True,
            real_run_ack=REAL_RUN_ACK_VALUE,
            platform_allowlist=frozenset({"bilibili", "not-a-platform"}),
            poll_interval_seconds=300,
        )

        payload = build_autopilot_heartbeat_payload(
            cfg,
            round_status="ok",
            summary={"selected": 2, "dispatched": 1},
        )

        encoded = json.dumps(payload)
        self.assertNotIn(cfg.admin_token, encoded)
        self.assertNotIn(REAL_RUN_ACK_VALUE, encoded)
        self.assertNotIn("not-a-platform", encoded)
        self.assertEqual(payload["platform_allowlist"], ["bilibili"])
        self.assertFalse(payload["platform_allowlist_valid"])
        self.assertTrue(payload["real_run_ack_valid"])
        self.assertEqual(payload["selected"], 2)
        self.assertEqual(payload["dispatched"], 1)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_heartbeat_failure_is_best_effort(self):
        observed = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            observed["path"] = request.url.path
            observed["payload"] = json.loads(request.content)
            return httpx.Response(503, json={"detail": "unavailable"})

        cfg = config()
        with patch("app.autopilot.structured_log") as structured_log:
            async with httpx.AsyncClient(
                base_url=cfg.core_api_url,
                headers={"x-admin-token": cfg.admin_token},
                transport=httpx.MockTransport(handler),
            ) as client:
                reported = await report_autopilot_heartbeat(
                    client,
                    cfg,
                    round_status="ok",
                    summary={"selected": 1},
                )

        self.assertFalse(reported)
        self.assertEqual(
            observed["path"],
            "/api/metrics/autopilot/heartbeat",
        )
        self.assertEqual(observed["payload"]["selected"], 1)
        structured_log.assert_called_once()

    async def test_real_dispatch_uses_confirmations_and_recommended_account(self):
        observed = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            observed["path"] = request.url.path
            observed["token"] = request.headers.get("x-admin-token")
            observed["confirm"] = request.headers.get("x-confirm-action")
            observed["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "queued"})

        cfg = config(
            real_run_enabled=True,
            real_run_ack=REAL_RUN_ACK_VALUE,
        )
        async with httpx.AsyncClient(
            base_url=cfg.core_api_url,
            headers={"x-admin-token": cfg.admin_token},
            transport=httpx.MockTransport(handler),
        ) as client:
            result = await dispatch_candidate(
                client,
                cfg,
                DispatchCandidate(9, "bilibili", "real_run", 22),
            )

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(observed["path"], "/api/lotteries/9/dispatch")
        self.assertEqual(observed["token"], "test-admin-token")
        self.assertEqual(observed["confirm"], "true")
        self.assertEqual(
            observed["body"],
            {
                "mode": "real_run",
                "dry_run": False,
                "confirm": True,
                "account_id": 22,
            },
        )

    async def test_round_stops_at_failure_limit(self):
        dispatches = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                self.assertEqual(request.url.params["limit"], "100")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "lottery_id": lottery_id,
                                "platform": "bilibili",
                                "recommended_mode": "dry_run",
                            }
                            for lottery_id in (1, 2, 3)
                        ]
                    },
                )
            dispatches.append(request.url.path)
            return httpx.Response(500, json={"detail": "failed"})

        cfg = config(round_limit=3, failure_limit=2)
        with patch("app.autopilot.structured_log"):
            async with httpx.AsyncClient(
                base_url=cfg.core_api_url,
                headers={"x-admin-token": cfg.admin_token},
                transport=httpx.MockTransport(handler),
            ) as client:
                summary = await run_round(client, cfg)

        self.assertEqual(
            summary,
            {
                "selected": 3,
                "dispatched": 0,
                "failures": 2,
                "probes_requested": 0,
                "deferred": 0,
            },
        )
        self.assertEqual(
            dispatches,
            [
                "/api/lotteries/1/dispatch",
                "/api/lotteries/2/dispatch",
            ],
        )

    async def test_shadow_terminal_requests_probe_for_same_account(self):
        observed = []

        async def handler(request: httpx.Request) -> httpx.Response:
            observed.append((request.method, request.url.path))
            if request.url.path == "/api/lotteries/strategy/queue":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "lottery_id": 21,
                                "platform": "bilibili",
                                "recommended_mode": "shadow_run",
                                "shadow_success": 1,
                                "execution_readiness_ready": False,
                                "execution_readiness_blockers": [
                                    "exact_execution_evidence_required"
                                ],
                                "execution_readiness": {
                                    "action_plan_ready": True,
                                    "rule_snapshot_ready": True,
                                },
                            }
                        ]
                    },
                )
            if request.url.path == "/api/lotteries/tasks/runs":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "lottery_id": 21,
                            "account_id": 44,
                            "platform": "bilibili",
                            "task_mode": "shadow_run",
                            "status": "succeeded",
                        }
                    ],
                )
            if request.url.path == "/api/lotteries/probes":
                return httpx.Response(200, json=[])
            if request.url.path == "/api/lotteries/21/probe":
                self.assertEqual(json.loads(request.content), {"account_id": 44})
                return httpx.Response(200, json={"status": "queued"})
            return httpx.Response(500)

        cfg = config(round_limit=1)
        with patch("app.autopilot.structured_log"):
            async with httpx.AsyncClient(
                base_url=cfg.core_api_url,
                headers={"x-admin-token": cfg.admin_token},
                transport=httpx.MockTransport(handler),
            ) as client:
                summary = await run_round(client, cfg)

        self.assertEqual(
            summary,
            {
                "selected": 1,
                "dispatched": 0,
                "failures": 0,
                "probes_requested": 1,
                "deferred": 0,
            },
        )
        self.assertIn(("POST", "/api/lotteries/21/probe"), observed)
        self.assertNotIn(("POST", "/api/lotteries/21/dispatch"), observed)

    async def test_xiaohongshu_shadow_terminal_requests_platform_probe(self):
        observed = []

        async def handler(request: httpx.Request) -> httpx.Response:
            observed.append((request.method, request.url.path))
            if request.url.path == "/api/lotteries/strategy/queue":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "lottery_id": 22,
                                "platform": "xiaohongshu",
                                "recommended_mode": "shadow_run",
                                "shadow_success": 1,
                                "execution_readiness_ready": False,
                                "execution_readiness_blockers": [
                                    "exact_execution_evidence_required"
                                ],
                                "execution_readiness": {
                                    "action_plan_ready": True,
                                    "rule_snapshot_ready": True,
                                },
                            }
                        ]
                    },
                )
            if request.url.path == "/api/lotteries/tasks/runs":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "lottery_id": 22,
                            "account_id": 45,
                            "platform": "xiaohongshu",
                            "task_mode": "shadow_run",
                            "status": "succeeded",
                        }
                    ],
                )
            if request.url.path == "/api/lotteries/probes":
                return httpx.Response(200, json=[])
            if request.url.path == "/api/lotteries/22/probe":
                self.assertEqual(json.loads(request.content), {"account_id": 45})
                return httpx.Response(200, json={"status": "queued"})
            return httpx.Response(500)

        cfg = config(
            round_limit=1,
            platform_allowlist=frozenset({"xiaohongshu"}),
        )
        with patch("app.autopilot.structured_log"):
            async with httpx.AsyncClient(
                base_url=cfg.core_api_url,
                headers={"x-admin-token": cfg.admin_token},
                transport=httpx.MockTransport(handler),
            ) as client:
                summary = await run_round(client, cfg)

        self.assertEqual(
            summary,
            {
                "selected": 1,
                "dispatched": 0,
                "failures": 0,
                "probes_requested": 1,
                "deferred": 0,
            },
        )
        self.assertIn(("POST", "/api/lotteries/22/probe"), observed)

    def test_latest_shadow_account_uses_terminal_binding(self):
        self.assertEqual(
            latest_successful_shadow_account(
                [
                    {
                        "lottery_id": 21,
                        "account_id": 44,
                        "platform": "bilibili",
                        "task_mode": "shadow_run",
                        "status": "succeeded",
                    }
                ],
                21,
            ),
            44,
        )
        self.assertEqual(
            latest_successful_shadow_account(
                [
                    {
                        "lottery_id": 22,
                        "account_id": 45,
                        "platform": "xiaohongshu",
                        "task_mode": "shadow_run",
                        "status": "succeeded",
                    }
                ],
                22,
                "xiaohongshu",
            ),
            45,
        )


if __name__ == "__main__":
    unittest.main()
