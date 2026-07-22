import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import security
from app.api import metrics
from app.models.schemas import RealRunSettingUpdate


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
            ):
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
                self.assertEqual(result["real_run_enabled"], enabled)
                self.assertTrue(
                    result["worker_gate_contract"]["process_capability_required"]
                )

    async def test_runtime_enable_is_rejected_without_process_capability(self):
        with patch.object(
            metrics, "require_min_role", new=Mock(return_value={"actor_id": "owner"})
        ), patch.object(
            metrics, "require_confirmation", new=Mock()
        ), patch.object(
            metrics, "set_runtime_setting", new=AsyncMock()
        ) as set_runtime_setting, patch.object(
            metrics.settings, "real_run_enabled", False
        ):
            with self.assertRaisesRegex(Exception, "Process REAL_RUN_ENABLED"):
                await metrics.update_real_run_setting(
                    RealRunSettingUpdate(enabled=True), SimpleNamespace()
                )

            set_runtime_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
