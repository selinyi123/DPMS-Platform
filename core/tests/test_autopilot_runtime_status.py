import asyncio
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.api import metrics
from app.models.schemas import AutopilotHeartbeatReport


def heartbeat_payload(**overrides) -> AutopilotHeartbeatReport:
    values = {
        "enabled": True,
        "deployment_real_run_enabled": False,
        "real_run_ack_valid": False,
        "platform_allowlist": ["bilibili", "xiaohongshu"],
        "platform_allowlist_valid": True,
        "poll_interval_seconds": 300,
        "round_status": "ok",
        "selected": 2,
        "dispatched": 1,
        "failures": 0,
        "probes_requested": 0,
        "deferred": 1,
    }
    values.update(overrides)
    return AutopilotHeartbeatReport(**values)


def platform_readiness(
    platform="bilibili",
    *,
    label="Bilibili",
    ready=True,
    adapter_kind="api",
):
    return {
        "platform": platform,
        "label": label,
        "safe_accounts": 1 if ready else 0,
        "dry_run_supported": True,
        "ready_for_dry_run": ready,
        "ready_for_real_run": ready,
        "task_transport_ready": ready,
        "task_transport_blocker_codes": [] if ready else ["transport_missing"],
        "task_transport": {"ready": ready},
        "action_adapter": True,
        "adapter_kind": adapter_kind,
        "real_actions_ready": ready,
        "latest_probe": {"status": "succeeded"} if ready else None,
    }


class AutopilotHeartbeatApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_heartbeat_persists_only_bounded_status(self):
        request = SimpleNamespace()
        payload = heartbeat_payload()
        with patch.object(
            metrics,
            "require_min_role",
            new=Mock(return_value={"actor_id": "autopilot"}),
        ) as require_role, patch.object(
            metrics.database,
            "execute",
            new=AsyncMock(),
        ) as execute:
            result = await metrics.record_autopilot_heartbeat(
                payload,
                request,
            )

        require_role.assert_called_once_with(request, "admin")
        execute.assert_awaited_once()
        values = execute.await_args.args[1]
        detail = json.loads(values["detail"])
        self.assertEqual(values["worker_id"], "core-autopilot")
        self.assertEqual(values["service_name"], "core-autopilot")
        self.assertEqual(values["status"], "ok")
        self.assertEqual(detail["platform_allowlist"], [
            "bilibili",
            "xiaohongshu",
        ])
        self.assertEqual(detail["last_round"]["dispatched"], 1)
        self.assertNotIn("token", values["detail"].lower())
        self.assertNotIn("credential", values["detail"].lower())
        self.assertEqual(result, {"status": "recorded"})

    def test_heartbeat_schema_rejects_arbitrary_secret_fields(self):
        with self.assertRaises(ValidationError):
            heartbeat_payload(admin_token="do-not-store")

    async def test_runtime_projection_drops_unrecognized_detail(self):
        stored_detail = metrics._autopilot_heartbeat_detail(
            heartbeat_payload()
        )
        stored = json.loads(stored_detail)
        stored["admin_token"] = "must-not-leak"
        stored["platform_allowlist"].append("unknown-platform")
        row = {
            "status": "ok",
            "detail": json.dumps(stored),
            "last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "heartbeat_age_seconds": 10,
        }
        with patch.object(
            metrics.database,
            "fetch_one",
            new=AsyncMock(return_value=row),
        ):
            status = await metrics._autopilot_runtime_status()

        self.assertTrue(status["available"])
        self.assertTrue(status["reported"])
        self.assertTrue(status["fresh"])
        self.assertEqual(status["stale_after_seconds"], 630)
        self.assertEqual(status["platform_allowlist"], [
            "bilibili",
            "xiaohongshu",
        ])
        self.assertFalse(status["platform_allowlist_valid"])
        self.assertFalse(status["dispatch_configured"])
        self.assertNotIn("admin_token", status)
        self.assertNotIn("must-not-leak", json.dumps(status, default=str))

    async def test_runtime_settings_includes_autopilot_projection(self):
        projected = metrics._empty_autopilot_runtime_status(available=True)
        fetch_one = AsyncMock(side_effect=[
            {"status": "closed", "reason": None},
            {"updated_at": None},
        ])
        with patch.object(
            metrics.database,
            "fetch_one",
            new=fetch_one,
        ), patch.object(
            metrics,
            "is_real_run_enabled",
            new=AsyncMock(return_value=False),
        ), patch.object(
            metrics,
            "_real_run_inflight_counts",
            new=AsyncMock(return_value={"queued": 0, "running": 0}),
        ), patch.object(
            metrics,
            "_autopilot_runtime_status",
            new=AsyncMock(return_value=projected),
        ):
            result = await metrics.runtime_settings()

        self.assertIs(result["autopilot"], projected)


class RuntimeProductionGateTests(unittest.TestCase):
    @staticmethod
    def action_summary(*, breaker, autopilot):
        return {
            "workers_online": 1,
            "real_run_enabled": True,
            "notification_channels_configured": 1,
            "notification_delivery": {
                "available": True,
                "ready": True,
                "sent_count_24h": 1,
                "last_success_at": "2026-08-01T05:00:00Z",
                "last_success_channel": "webhook",
                "configured_channels": ["webhook"],
                "blocker_code": None,
            },
            "recent_risk_events_24h": 0,
            "proxy_exits_total": 1,
            "dry_run_ready": 0,
            "real_run_ready": 1,
            "redis_consumer_group_retention_alerts": [],
            "autopilot_targets": {
                "available": True,
                "scope_configured": True,
                "pending_targets": 1,
                "observed_targets": 1,
                "eligible_targets": 1,
                "eligible_by_platform": {"bilibili": 1},
                "plan_ready_targets": 1,
                "missing_plan_targets": 0,
                "exact_real_candidate": {
                    "available": True,
                    "ready": True,
                    "candidate_count": 1,
                    "candidate": {
                        "lottery_id": 1,
                        "platform": "bilibili",
                        "account_id": 4,
                        "target_valid": True,
                        "account_lease_available": True,
                        "execution_readiness_allowed": True,
                    },
                    "observed_targets": 1,
                    "observation_limit": 100,
                    "observation_truncated": False,
                    "account_candidate_truncated_platforms": [],
                    "blocker_counts": {},
                },
            },
            "global_circuit_breaker": breaker,
            "autopilot": autopilot,
        }

    def test_runtime_checks_gate_on_breaker_and_autopilot_state(self):
        summary = {
            "platforms_total": 1,
            "dry_run_supported": 1,
            "dry_run_ready": 1,
            "real_run_ready": 1,
            "safe_accounts_total": 1,
            "task_transport_ready": 1,
            "workers_online": 1,
            "real_run_enabled": True,
            "notification_channels_configured": 1,
            "pending_targets": 1,
            "global_circuit_breaker": {
                "available": True,
                "status": "open",
                "allows_real_run": False,
            },
            "autopilot": {
                "available": True,
                "reported": True,
                "fresh": False,
                "heartbeat_age_seconds": 700,
                "stale_after_seconds": 630,
                "enabled": True,
                "dispatch_configured": True,
                "platform_allowlist_valid": True,
                "platform_allowlist": ["bilibili"],
                "real_run_authorized": False,
            },
            "autopilot_targets": {
                "available": True,
                "pending_targets": 1,
                "observed_targets": 1,
                "eligible_targets": 1,
                "eligible_by_platform": {"bilibili": 1},
            },
        }

        checks = {
            item["code"]: item
            for item in metrics.build_production_checks(
                [platform_readiness()], summary
            )
        }

        self.assertFalse(checks["global_circuit_breaker_closed"]["passed"])
        self.assertFalse(checks["autopilot_heartbeat_fresh"]["passed"])
        self.assertTrue(checks["autopilot_dispatch_configured"]["passed"])
        self.assertFalse(checks["autopilot_real_run_authorized"]["passed"])
        self.assertTrue(checks["active_proxy_exit"]["passed"] is False)
        self.assertFalse(checks["active_proxy_exit"]["blocking"])
        self.assertIn("evidence", checks["autopilot_heartbeat_fresh"])

    def test_next_action_reports_only_the_first_autopilot_remediation(self):
        breaker = {
            "available": True,
            "status": "open",
            "allows_real_run": False,
        }
        autopilot = metrics._empty_autopilot_runtime_status(available=True)
        actions = metrics.build_next_actions(
            [],
            self.action_summary(breaker=breaker, autopilot=autopilot),
        )

        self.assertEqual(
            [item["code"] for item in actions],
            [
                "restore_autopilot_heartbeat",
                "review_global_circuit_breaker",
            ],
        )

    def test_next_actions_put_prerequisites_before_authority_and_p1(self):
        breaker = {
            "available": True,
            "status": "open",
            "allows_real_run": False,
        }
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        summary = self.action_summary(
            breaker=breaker,
            autopilot=autopilot,
        )
        summary["notification_channels_configured"] = 0
        summary["proxy_exits_total"] = 0
        summary["autopilot_targets"]["eligible_targets"] = 0
        actions = metrics.build_next_actions(
            [platform_readiness("bilibili", ready=False)],
            summary,
        )
        codes = [item["code"] for item in actions]

        self.assertLess(
            codes.index("configure_notification"),
            codes.index("review_global_circuit_breaker"),
        )
        self.assertLess(
            codes.index("review_global_circuit_breaker"),
            codes.index("add_proxy_exit"),
        )

    def test_next_action_advances_from_config_to_real_authorization(self):
        breaker = {
            "available": True,
            "status": "closed",
            "allows_real_run": True,
        }
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "heartbeat_age_seconds": 1,
            "stale_after_seconds": 90,
        }
        configure_actions = metrics.build_next_actions(
            [],
            self.action_summary(breaker=breaker, autopilot=autopilot),
        )
        self.assertEqual(
            [item["code"] for item in configure_actions],
            ["configure_autopilot_dispatch"],
        )

        autopilot.update({
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        })
        authorize_actions = metrics.build_next_actions(
            [platform_readiness()],
            self.action_summary(breaker=breaker, autopilot=autopilot),
        )
        self.assertEqual(
            [item["code"] for item in authorize_actions],
            ["authorize_autopilot_real_run"],
        )

    def test_scope_excludes_platform_not_in_autopilot_allowlist(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili", "douyin", "xiaohongshu"],
        }
        platforms = [
            platform_readiness("bilibili"),
            platform_readiness("douyin", label="Douyin"),
            platform_readiness("xiaohongshu", label="Xiaohongshu"),
            platform_readiness("weibo", label="Weibo", ready=False),
        ]
        summary = {
            **self.action_summary(
                breaker={
                    "available": True,
                    "status": "closed",
                    "allows_real_run": True,
                },
                autopilot=autopilot,
            ),
            "platforms_total": 4,
            "task_transport_ready": 3,
            "safe_accounts_total": 3,
            "pending_targets": 1,
            "active_proxy_exits": 0,
            "proxied_safe_accounts": 0,
        }

        checks = {
            item["code"]: item
            for item in metrics.build_production_checks(platforms, summary)
        }

        self.assertTrue(checks["all_platform_task_transports_ready"]["passed"])
        self.assertTrue(checks["all_platforms_dry_ready"]["passed"])
        self.assertIn("3/3", checks["all_platforms_dry_ready"]["detail"])
        self.assertNotIn("weibo", checks["all_platforms_dry_ready"]["scope"])
        action_targets = {
            item["target"]
            for item in metrics.build_next_actions(platforms, summary)
        }
        self.assertNotIn("weibo", action_targets)

    def test_api_adapter_without_probe_has_a_p0_remediation_path(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        platform = platform_readiness(ready=False, adapter_kind="api")
        platform.update({
            "safe_accounts": 1,
            "ready_for_dry_run": True,
            "task_transport_ready": True,
            "task_transport": {"ready": True},
        })
        actions = metrics.build_next_actions(
            [platform],
            self.action_summary(
                breaker={
                    "available": True,
                    "status": "closed",
                    "allows_real_run": True,
                },
                autopilot=autopilot,
            ),
        )
        probe = next(
            item for item in actions if item["code"] == "complete_adapter_probe"
        )

        self.assertEqual(probe["priority"], "P0")
        self.assertEqual(probe["evidence"]["observed"]["adapter_kind"], "api")
        self.assertIn("example", probe)

    def test_missing_target_plan_is_a_structured_p0_action_and_check(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["xiaohongshu"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "closed",
                "allows_real_run": True,
            },
            autopilot=autopilot,
        )
        summary["autopilot_targets"].update({
            "pending_targets": 2,
            "observed_targets": 2,
            "eligible_targets": 0,
            "eligible_by_platform": {},
            "plan_ready_targets": 0,
            "missing_plan_targets": 2,
            "missing_plan_target_ids": [11, 12],
            "plan_blocker_counts": {"lottery_action_plan_v2_required": 2},
        })
        platform = platform_readiness("xiaohongshu", label="Xiaohongshu")
        actions = metrics.build_next_actions([platform], summary)
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks([platform], summary)
        }
        action = next(
            item for item in actions if item["code"] == "complete_target_action_plan"
        )

        self.assertEqual(action["priority"], "P0")
        self.assertEqual(
            action["evidence"]["observed"]["missing_plan_target_ids"],
            [11, 12],
        )
        self.assertFalse(checks["autopilot_target_plan_ready"]["passed"])
        self.assertEqual(
            checks["autopilot_target_plan_ready"]["next_action_code"],
            "complete_target_action_plan",
        )

    def test_notification_requires_recent_success_before_authorization(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "closed",
                "allows_real_run": True,
            },
            autopilot=autopilot,
        )
        summary["notification_delivery"].update({
            "ready": False,
            "sent_count_24h": 0,
            "last_success_at": None,
            "last_success_channel": None,
            "blocker_code": "notification_recent_success_required",
        })
        platform = platform_readiness()
        actions = metrics.build_next_actions([platform], summary)
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks([platform], summary)
        }

        self.assertIn("verify_notification_delivery", {
            item["code"] for item in actions
        })
        self.assertNotIn("authorize_autopilot_real_run", {
            item["code"] for item in actions
        })
        self.assertFalse(checks["notification_ready"]["passed"])
        self.assertEqual(
            checks["notification_ready"]["next_action_code"],
            "verify_notification_delivery",
        )

    def test_real_action_capability_is_independent_of_switch_and_breaker(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "open",
                "allows_real_run": False,
            },
            autopilot=autopilot,
        )
        summary["real_run_enabled"] = False
        platform = platform_readiness()
        platform["ready_for_real_run"] = False
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks([platform], summary)
        }

        self.assertTrue(checks["real_run_available"]["passed"])
        self.assertIsNone(checks["real_run_available"]["next_action_code"])
        self.assertFalse(checks["real_run_global_switch"]["passed"])
        self.assertFalse(checks["global_circuit_breaker_closed"]["passed"])

    def test_real_capability_action_points_to_missing_account_before_probe(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "closed",
                "allows_real_run": True,
            },
            autopilot=autopilot,
        )
        platform = platform_readiness(ready=False)
        platform.update({
            "task_transport_ready": True,
            "task_transport": {"ready": True},
            "real_actions_ready": True,
        })
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks([platform], summary)
        }

        self.assertFalse(checks["real_run_available"]["passed"])
        self.assertEqual(
            checks["real_run_available"]["next_action_code"],
            "add_calibrated_account",
        )

    def test_declared_allowlist_must_fully_resolve_to_registry_items(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili", "xiaohongshu"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "closed",
                "allows_real_run": True,
            },
            autopilot=autopilot,
        )
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks(
                [platform_readiness("bilibili")], summary
            )
        }
        actions = metrics.build_next_actions(
            [platform_readiness("bilibili")], summary
        )

        dispatch = checks["autopilot_dispatch_configured"]
        self.assertFalse(dispatch["passed"])
        self.assertEqual(
            dispatch["evidence"]["observed"]["scope_resolution"][
                "missing_platforms"
            ],
            ["xiaohongshu"],
        )
        self.assertIn("configure_autopilot_dispatch", {
            item["code"] for item in actions
        })
        self.assertNotIn("authorize_autopilot_real_run", {
            item["code"] for item in actions
        })

    def test_platform_aggregate_cannot_replace_exact_candidate(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili", "xiaohongshu"],
        }
        summary = self.action_summary(
            breaker={
                "available": True,
                "status": "closed",
                "allows_real_run": True,
            },
            autopilot=autopilot,
        )
        summary["autopilot_targets"].update({
            "eligible_targets": 1,
            "eligible_by_platform": {"xiaohongshu": 1},
            "exact_real_candidate": {
                "available": True,
                "ready": False,
                "candidate_count": 0,
                "candidate": None,
                "blocker_code": "autopilot_exact_real_candidate_required",
                "blocker_counts": {
                    "exact_execution_evidence_required": 1,
                },
                "observed_targets": 1,
                "observation_limit": 100,
                "observation_truncated": False,
                "account_candidate_truncated_platforms": [],
            },
        })
        bilibili = platform_readiness("bilibili")
        xiaohongshu = platform_readiness(
            "xiaohongshu",
            label="Xiaohongshu",
            ready=False,
            adapter_kind="selector",
        )
        xiaohongshu.update({
            "safe_accounts": 1,
            "ready_for_dry_run": True,
            "task_transport_ready": True,
            "task_transport": {"ready": True},
            "action_adapter": True,
        })
        platforms = [bilibili, xiaohongshu]
        checks = {
            item["code"]: item
            for item in metrics.build_production_checks(platforms, summary)
        }
        actions = metrics.build_next_actions(platforms, summary)

        self.assertTrue(checks["autopilot_target_plan_ready"]["passed"])
        self.assertTrue(checks["real_run_available"]["passed"])
        exact = checks["autopilot_exact_real_candidate_ready"]
        self.assertFalse(exact["passed"])
        self.assertEqual(
            exact["evidence"]["observed"]["candidate_count"],
            0,
        )
        self.assertIn("complete_exact_real_candidate", {
            item["code"] for item in actions
        })
        self.assertNotIn("authorize_autopilot_real_run", {
            item["code"] for item in actions
        })

    def test_probe_action_evidence_drops_error_message(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili"],
        }
        platform = platform_readiness(ready=False, adapter_kind="api")
        platform.update({
            "safe_accounts": 1,
            "ready_for_dry_run": True,
            "task_transport_ready": True,
            "task_transport": {"ready": True},
            "latest_probe": {
                "status": "failed",
                "created_at": "2026-08-01T05:00:00Z",
                "ready_phase_count": 0,
                "ready_for_real_actions": False,
                "error_message": "sensitive remote response",
            },
        })
        actions = metrics.build_next_actions(
            [platform],
            self.action_summary(
                breaker={
                    "available": True,
                    "status": "closed",
                    "allows_real_run": True,
                },
                autopilot=autopilot,
            ),
        )
        probe = next(
            item for item in actions if item["code"] == "complete_adapter_probe"
        )
        latest = probe["evidence"]["observed"]["latest_probe"]

        self.assertEqual(set(latest), {
            "status",
            "created_at",
            "ready_phase_count",
            "ready_for_real_actions",
        })
        self.assertNotIn("sensitive remote response", json.dumps(probe))

    def test_weibo_oauth_is_p1_when_another_platform_has_capability(self):
        autopilot = {
            **metrics._empty_autopilot_runtime_status(available=True),
            "reported": True,
            "fresh": True,
            "enabled": True,
            "dispatch_configured": True,
            "platform_allowlist_valid": True,
            "platform_allowlist": ["bilibili", "weibo"],
        }
        bilibili = platform_readiness("bilibili")
        weibo = platform_readiness(
            "weibo",
            label="Weibo",
            ready=False,
            adapter_kind="oauth",
        )
        weibo.update({
            "safe_accounts": 1,
            "ready_for_dry_run": True,
            "task_transport_ready": True,
            "task_transport": {"ready": True},
            "action_adapter": True,
        })
        actions = metrics.build_next_actions(
            [bilibili, weibo],
            self.action_summary(
                breaker={
                    "available": True,
                    "status": "closed",
                    "allows_real_run": True,
                },
                autopilot=autopilot,
            ),
        )
        oauth = next(
            item for item in actions if item["code"] == "configure_weibo_oauth"
        )

        self.assertEqual(oauth["priority"], "P1")


class NotificationDeliveryStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_success_is_scoped_to_configured_real_channels(self):
        created_at = datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
        fetch_one = AsyncMock(return_value={
            "channel": "webhook",
            "created_at": created_at,
            "sent_count_24h": 2,
        })
        with patch.object(
            metrics.database,
            "fetch_one",
            new=fetch_one,
        ), patch.object(
            metrics,
            "notification_config_revision",
            new=AsyncMock(return_value="4:" + "a" * 64),
        ):
            status = await metrics._notification_delivery_status(
                ["webhook", "manual", "dispatch"]
            )

        query, values = fetch_one.await_args.args
        self.assertIn("success = 1", query)
        self.assertIn("channel NOT IN ('manual', 'dispatch')", query)
        self.assertIn("INTERVAL 24 HOUR", query)
        self.assertIn("config_revision = :notification_revision_0", query)
        self.assertEqual(values, {
            "notification_channel_0": "webhook",
            "notification_revision_0": "4:" + "a" * 64,
        })
        self.assertTrue(status["ready"])
        self.assertEqual(status["sent_count_24h"], 2)
        self.assertEqual(status["last_success_at"], created_at)
        self.assertEqual(status["last_success_channel"], "webhook")

    async def test_delivery_query_failure_is_observable_and_fail_closed(self):
        with patch.object(
            metrics.database,
            "fetch_one",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ), patch.object(
            metrics,
            "notification_config_revision",
            new=AsyncMock(return_value="4:" + "a" * 64),
        ):
            status = await metrics._notification_delivery_status(["feishu"])

        self.assertFalse(status["available"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["sent_count_24h"], 0)
        self.assertEqual(
            status["blocker_code"],
            "notification_delivery_status_unavailable",
        )

    async def test_missing_current_revision_is_fail_closed(self):
        fetch_one = AsyncMock()
        with patch.object(
            metrics.database,
            "fetch_one",
            new=fetch_one,
        ), patch.object(
            metrics,
            "notification_config_revision",
            new=AsyncMock(return_value=None),
        ):
            status = await metrics._notification_delivery_status(["feishu"])

        fetch_one.assert_not_awaited()
        self.assertFalse(status["available"])
        self.assertFalse(status["ready"])
        self.assertEqual(
            status["blocker_code"],
            "notification_config_revision_unavailable",
        )

    async def test_old_or_mismatched_revision_success_does_not_pass(self):
        fetch_one = AsyncMock(return_value=None)
        current_revision = "5:" + "c" * 64
        with patch.object(
            metrics.database,
            "fetch_one",
            new=fetch_one,
        ), patch.object(
            metrics,
            "notification_config_revision",
            new=AsyncMock(return_value=current_revision),
        ):
            status = await metrics._notification_delivery_status(["feishu"])

        query, values = fetch_one.await_args.args
        self.assertIn("config_revision = :notification_revision_0", query)
        self.assertEqual(values["notification_revision_0"], current_revision)
        self.assertFalse(status["ready"])
        self.assertEqual(
            status["blocker_code"],
            "notification_recent_success_required",
        )


class AutopilotTargetReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_gate_uses_bounded_unexpired_scope_and_dispatch_validator(self):
        rows = [
            {
                "id": 11,
                "platform": "xiaohongshu",
                "scoped_pending_count": 2,
            },
            {
                "id": 12,
                "platform": "xiaohongshu",
                "scoped_pending_count": 2,
            },
        ]
        fetch_all = AsyncMock(return_value=rows)
        validate = AsyncMock(side_effect=[
            {
                "action_plan_ready": False,
                "rule_snapshot_ready": False,
                "blockers": ["lottery_action_plan_v2_required"],
            },
            {
                "action_plan_ready": True,
                "rule_snapshot_ready": True,
                "blockers": ["execution_account_scope_required"],
            },
        ])
        with patch.object(
            metrics.database,
            "fetch_all",
            new=fetch_all,
        ), patch.object(
            metrics,
            "load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=object()),
        ), patch.object(
            metrics,
            "validate_real_run_evidence",
            new=validate,
        ), patch.object(
            metrics,
            "evaluate_exact_real_candidate_observation",
            new=AsyncMock(return_value={
                "available": True,
                "ready": False,
                "candidate_count": 0,
                "candidate": None,
                "blocker_code": "autopilot_exact_real_candidate_required",
                "observed_targets": 2,
                "observation_limit": 100,
                "observation_truncated": False,
                "account_candidate_truncated_platforms": [],
                "blocker_counts": {},
            }),
        ):
            status = await metrics._autopilot_target_readiness({
                "platform_allowlist_valid": True,
                "platform_allowlist": ["xiaohongshu"],
            })

        query, values = fetch_all.await_args.args
        self.assertIn("expires_at > UTC_TIMESTAMP()", query)
        self.assertIn("LIMIT :target_limit", query)
        self.assertIn("ORDER BY l.value_score DESC, l.id ASC", query)
        self.assertEqual(
            values["target_limit"],
            metrics.AUTOPILOT_TARGET_READINESS_LIMIT + 1,
        )
        self.assertEqual(status["pending_targets"], 2)
        self.assertEqual(status["plan_ready_targets"], 1)
        self.assertEqual(status["eligible_targets"], 1)
        self.assertEqual(
            status["eligible_by_platform"],
            {"xiaohongshu": 1},
        )
        self.assertEqual(status["missing_plan_target_ids"], [11])
        self.assertEqual(
            status["plan_blocker_counts"],
            {"lottery_action_plan_v2_required": 1},
        )

    async def test_plan_cancellation_cancels_parallel_exact_observation(self):
        exact_started = asyncio.Event()
        exact_cancelled = asyncio.Event()

        async def slow_exact(*_args, **_kwargs):
            exact_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                exact_cancelled.set()

        async def cancel_plan(*_args, **_kwargs):
            await exact_started.wait()
            raise asyncio.CancelledError

        rows = [{
            "id": 11,
            "platform": "xiaohongshu",
            "value_score": 80,
            "scoped_pending_count": 1,
        }]
        with patch.object(
            metrics.database,
            "fetch_all",
            new=AsyncMock(return_value=rows),
        ), patch.object(
            metrics,
            "evaluate_exact_real_candidate_observation",
            new=slow_exact,
        ), patch.object(
            metrics,
            "load_account_scoped_real_run_readiness_batch",
            new=cancel_plan,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await metrics._autopilot_target_readiness({
                    "platform_allowlist_valid": True,
                    "platform_allowlist": ["xiaohongshu"],
                })

        self.assertTrue(exact_cancelled.is_set())


class GlobalCircuitBreakerProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_breaker_projection_is_fail_closed(self):
        with patch.object(
            metrics.database,
            "fetch_one",
            new=AsyncMock(return_value={
                "status": "half_open",
                "reason": "recovery probe",
                "opened_at": None,
                "updated_at": None,
            }),
        ):
            status = await metrics._global_circuit_breaker_runtime_status()

        self.assertTrue(status["available"])
        self.assertEqual(status["status"], "half_open")
        self.assertFalse(status["allows_real_run"])


if __name__ == "__main__":
    unittest.main()
