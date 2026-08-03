import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import security
from app.api import metrics
from app.models.schemas import RealRunSettingUpdate


def ready_technical_prerequisite_snapshot():
    return {
        "production_checks": [
            {"code": "worker_online", "priority": "P0", "passed": True},
            {
                "code": "all_platform_task_transports_ready",
                "priority": "P0",
                "passed": True,
            },
            {
                "code": "real_run_deployment_capability",
                "priority": "P0",
                "passed": True,
            },
            {
                "code": "autopilot_heartbeat_fresh",
                "priority": "P0",
                "passed": True,
            },
            {
                "code": "autopilot_dispatch_configured",
                "priority": "P0",
                "passed": True,
            },
            {"code": "notification_ready", "priority": "P0", "passed": True},
            {"code": "target_pool_ready", "priority": "P0", "passed": True},
            {
                "code": "autopilot_target_plan_ready",
                "priority": "P0",
                "passed": True,
            },
            {
                "code": "autopilot_exact_real_candidate_ready",
                "priority": "P0",
                "passed": True,
            },
            {
                "code": "all_platforms_dry_ready",
                "priority": "P0",
                "passed": True,
            },
            {"code": "real_run_available", "priority": "P0", "passed": True},
            {
                "code": "real_run_global_switch",
                "priority": "P0",
                "passed": False,
            },
            {
                "code": "global_circuit_breaker_closed",
                "priority": "P0",
                "passed": False,
            },
            {
                "code": "autopilot_real_run_authorized",
                "priority": "P0",
                "passed": False,
            },
        ]
    }


class RuntimeRealRunSettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_switch_is_a_hard_ceiling(self):
        with patch.object(
            security.settings, "real_run_enabled", False
        ), patch.object(
            security, "get_runtime_setting", new=AsyncMock(return_value="true")
        ) as get_runtime_setting:
            self.assertFalse(await security.is_real_run_enabled())
            get_runtime_setting.assert_not_awaited()

    async def test_process_and_runtime_switches_are_both_required(self):
        for persisted, expected in (("false", False), ("true", True)):
            with self.subTest(persisted=persisted), patch.object(
                security.settings, "real_run_enabled", True
            ), patch.object(
                security,
                "get_runtime_setting",
                new=AsyncMock(return_value=persisted),
            ) as get_runtime_setting:
                self.assertEqual(await security.is_real_run_enabled(), expected)
                get_runtime_setting.assert_awaited_once_with(
                    "real_run_enabled", "false"
                )

    async def test_toggle_never_resets_global_circuit_breaker(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch.object(
                metrics,
                "require_min_role",
                new=Mock(return_value={"actor_id": "owner-1"}),
            ) as require_role, patch.object(
                metrics, "require_confirmation", new=Mock()
            ) as require_confirmation, patch.object(
                metrics, "set_runtime_setting", new=AsyncMock()
            ) as set_runtime_setting, patch.object(
                metrics, "audit_event", new=AsyncMock()
            ) as audit_event, patch.object(
                metrics.database, "execute", new=AsyncMock()
            ) as database_execute, patch.object(
                metrics.settings, "real_run_enabled", True
            ), patch.object(
                metrics,
                "_real_run_inflight_counts",
                new=AsyncMock(return_value={"queued": 0, "running": 0}),
            ), patch.object(
                metrics, "record_event", new=AsyncMock()
            ), patch.object(
                metrics,
                "_readiness_snapshot",
                new=AsyncMock(
                    return_value=ready_technical_prerequisite_snapshot()
                ),
            ) as readiness_snapshot:
                request = SimpleNamespace()
                result = await metrics.update_real_run_setting(
                    RealRunSettingUpdate(enabled=enabled), request
                )

                require_role.assert_called_once_with(request, "owner")
                require_confirmation.assert_called_once_with(request)
                set_runtime_setting.assert_awaited_once_with(
                    "real_run_enabled", "true" if enabled else "false"
                )
                database_execute.assert_not_awaited()
                audit_event.assert_awaited_once()
                detail = audit_event.await_args.kwargs["detail"]
                self.assertEqual(detail["enabled"], enabled)
                self.assertEqual(detail["global_circuit_breaker"], "unchanged")
                self.assertEqual(result["status"], "updated")
                self.assertTrue(result["deployment_real_run_enabled"])
                self.assertEqual(result["runtime_real_run_enabled"], enabled)
                self.assertEqual(result["real_run_enabled"], enabled)
                self.assertTrue(
                    result["worker_gate_contract"]["process_capability_required"]
                )
                if enabled:
                    readiness_snapshot.assert_awaited_once_with()
                else:
                    readiness_snapshot.assert_not_awaited()

    async def test_runtime_enable_is_rejected_without_process_capability(self):
        with patch.object(
            metrics, "require_min_role", new=Mock(return_value={"actor_id": "owner"})
        ), patch.object(
            metrics, "require_confirmation", new=Mock()
        ), patch.object(
            metrics, "set_runtime_setting", new=AsyncMock()
        ) as set_runtime_setting, patch.object(
            metrics.settings, "real_run_enabled", False
        ), patch.object(
            metrics, "_readiness_snapshot", new=AsyncMock()
        ) as readiness_snapshot:
            with self.assertRaises(HTTPException) as raised:
                await metrics.update_real_run_setting(
                    RealRunSettingUpdate(enabled=True), SimpleNamespace()
                )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(
                raised.exception.detail,
                {"code": "real_run_deployment_capability_disabled"},
            )
            set_runtime_setting.assert_not_awaited()
            readiness_snapshot.assert_not_awaited()

    async def test_runtime_enable_reports_only_technical_p0_blocker_codes(self):
        snapshot = ready_technical_prerequisite_snapshot()
        for check in snapshot["production_checks"]:
            if check["code"] in {"worker_online", "notification_ready"}:
                check["passed"] = False
        snapshot["production_checks"].append({
            "code": "optional_proxy",
            "priority": "P1",
            "passed": False,
        })
        with patch.object(
            metrics, "require_min_role", new=Mock(return_value={"actor_id": "owner"})
        ), patch.object(
            metrics, "require_confirmation", new=Mock()
        ), patch.object(
            metrics.settings, "real_run_enabled", True
        ), patch.object(
            metrics, "_readiness_snapshot", new=AsyncMock(return_value=snapshot)
        ), patch.object(
            metrics, "set_runtime_setting", new=AsyncMock()
        ) as set_runtime_setting:
            with self.assertRaises(HTTPException) as raised:
                await metrics.update_real_run_setting(
                    RealRunSettingUpdate(enabled=True), SimpleNamespace()
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "real_run_prerequisites_not_ready",
                "blocker_codes": ["worker_online", "notification_ready"],
            },
        )
        set_runtime_setting.assert_not_awaited()

    async def test_runtime_enable_rejects_incomplete_p0_contract(self):
        incomplete_snapshot = {
            "production_checks": [
                {
                    "code": "worker_online",
                    "priority": "P0",
                    "passed": True,
                },
                {
                    "code": "real_run_global_switch",
                    "priority": "P0",
                    "passed": False,
                },
                {
                    "code": "global_circuit_breaker_closed",
                    "priority": "P0",
                    "passed": False,
                },
                {
                    "code": "autopilot_real_run_authorized",
                    "priority": "P0",
                    "passed": False,
                },
            ]
        }
        with patch.object(
            metrics, "require_min_role", new=Mock(return_value={"actor_id": "owner"})
        ), patch.object(
            metrics, "require_confirmation", new=Mock()
        ), patch.object(
            metrics.settings, "real_run_enabled", True
        ), patch.object(
            metrics,
            "_readiness_snapshot",
            new=AsyncMock(return_value=incomplete_snapshot),
        ), patch.object(
            metrics, "set_runtime_setting", new=AsyncMock()
        ) as set_runtime_setting:
            with self.assertRaises(HTTPException) as raised:
                await metrics.update_real_run_setting(
                    RealRunSettingUpdate(enabled=True), SimpleNamespace()
                )

        self.assertEqual(
            raised.exception.detail,
            {
                "code": "real_run_prerequisites_not_ready",
                "blocker_codes": ["production_readiness_contract_invalid"],
            },
        )
        set_runtime_setting.assert_not_awaited()

    def test_technical_p0_contract_is_fixed_and_rejects_drift(self):
        self.assertEqual(
            metrics.REAL_RUN_REQUIRED_TECHNICAL_P0_CHECK_CODES,
            {
                "worker_online",
                "all_platform_task_transports_ready",
                "real_run_deployment_capability",
                "autopilot_heartbeat_fresh",
                "autopilot_dispatch_configured",
                "notification_ready",
                "target_pool_ready",
                "autopilot_target_plan_ready",
                "autopilot_exact_real_candidate_ready",
                "all_platforms_dry_ready",
                "real_run_available",
            },
        )

        duplicate_snapshot = ready_technical_prerequisite_snapshot()
        duplicate_snapshot["production_checks"].append({
            "code": "worker_online",
            "priority": "P0",
            "passed": True,
        })
        unknown_snapshot = ready_technical_prerequisite_snapshot()
        unknown_snapshot["production_checks"].append({
            "code": "future_unreviewed_p0_gate",
            "priority": "P0",
            "passed": True,
        })

        for label, snapshot in (
            ("duplicate", duplicate_snapshot),
            ("unknown", unknown_snapshot),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    metrics._real_run_technical_prerequisite_blocker_codes(
                        snapshot
                    ),
                    ["production_readiness_contract_invalid"],
                )

    async def test_runtime_disable_skips_capability_and_readiness_gates(self):
        with patch.object(
            metrics, "require_min_role", new=Mock(return_value={"actor_id": "owner"})
        ), patch.object(
            metrics, "require_confirmation", new=Mock()
        ), patch.object(
            metrics.settings, "real_run_enabled", False
        ), patch.object(
            metrics,
            "_readiness_snapshot",
            new=AsyncMock(side_effect=AssertionError("must not run")),
        ) as readiness_snapshot, patch.object(
            metrics, "set_runtime_setting", new=AsyncMock()
        ) as set_runtime_setting, patch.object(
            metrics,
            "_real_run_inflight_counts",
            new=AsyncMock(return_value={"queued": 0, "running": 0}),
        ), patch.object(
            metrics, "audit_event", new=AsyncMock()
        ), patch.object(
            metrics, "record_event", new=AsyncMock()
        ):
            result = await metrics.update_real_run_setting(
                RealRunSettingUpdate(enabled=False), SimpleNamespace()
            )

        readiness_snapshot.assert_not_awaited()
        set_runtime_setting.assert_awaited_once_with("real_run_enabled", "false")
        self.assertFalse(result["deployment_real_run_enabled"])
        self.assertFalse(result["runtime_real_run_enabled"])
        self.assertFalse(result["real_run_enabled"])

    async def test_runtime_settings_exposes_deployment_and_runtime_split(self):
        projected_autopilot = metrics._empty_autopilot_runtime_status(
            available=True
        )
        fetch_one = AsyncMock(side_effect=[
            {"status": "open", "reason": "test"},
            {"setting_value": "false", "updated_at": "2026-08-01T00:00:00Z"},
        ])
        with patch.object(
            metrics.database, "fetch_one", new=fetch_one
        ), patch.object(
            metrics.settings, "real_run_enabled", True
        ), patch.object(
            metrics,
            "_real_run_inflight_counts",
            new=AsyncMock(return_value={"queued": 0, "running": 0}),
        ), patch.object(
            metrics,
            "_autopilot_runtime_status",
            new=AsyncMock(return_value=projected_autopilot),
        ):
            result = await metrics.runtime_settings()

        self.assertTrue(result["deployment_real_run_enabled"])
        self.assertFalse(result["runtime_real_run_enabled"])
        self.assertFalse(result["real_run_enabled"])
        self.assertTrue(
            result["real_run_control"][
                "technical_prerequisites_validated_on_enable"
            ]
        )
        self.assertTrue(
            result["real_run_control"]["global_circuit_breaker_independent"]
        )


if __name__ == "__main__":
    unittest.main()
