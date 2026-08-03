import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main, runtime_lane_health, task_runner
from app.runtime_scope import build_worker_runtime_plan
from app.services import task_outbox
from app.task_streams import task_stream_bindings


class WorkerRuntimeLaneHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_runner._reset_task_lane_health_for_tests()
        runtime_lane_health._reset_runtime_lane_health_for_tests()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.marker = Path(self.temp_dir.name) / "worker-health"

    def tearDown(self):
        task_runner._reset_task_lane_health_for_tests()
        runtime_lane_health._reset_runtime_lane_health_for_tests()

    @staticmethod
    def _platform_plan():
        return build_worker_runtime_plan(
            role="platform",
            platform_scope="bilibili",
        )

    @staticmethod
    def _record_fresh_platform_lanes():
        for binding in task_stream_bindings(include_legacy=False):
            if binding.platform == "bilibili":
                task_runner._record_task_lane_success(
                    binding,
                    "xreadgroup",
                )
        runtime_lane_health.record_runtime_lane_success(
            "probe",
            "bilibili",
        )
        runtime_lane_health.record_runtime_lane_success(
            "calibration",
            "bilibili",
        )

    async def test_marker_is_written_only_after_all_lanes_and_db_succeed(
        self,
    ):
        plan = self._platform_plan()
        self._record_fresh_platform_lanes()
        redis_ping = AsyncMock(return_value=True)
        db_execute = AsyncMock(return_value=1)

        with (
            patch.object(main, "HEALTH_FILE", self.marker),
            patch.object(
                main,
                "redis_client",
                SimpleNamespace(ping=redis_ping),
            ),
            patch.object(main.database, "execute", new=db_execute),
        ):
            healthy = await main.publish_worker_heartbeat_once(plan)

        self.assertTrue(healthy)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "ok")
        values = db_execute.await_args.args[1]
        self.assertEqual(values["status"], "ok")

    async def test_stale_owned_lane_removes_marker_and_publishes_degraded(
        self,
    ):
        plan = self._platform_plan()
        for binding in task_stream_bindings(include_legacy=False):
            if binding.platform == "bilibili":
                task_runner._record_task_lane_success(
                    binding,
                    "xreadgroup",
                )
        self.marker.write_text("old", encoding="utf-8")
        db_execute = AsyncMock(return_value=1)

        runtime_lane_health.record_runtime_lane_success(
            "probe",
            "bilibili",
        )
        runtime_lane_health.record_runtime_lane_success(
            "calibration",
            "bilibili",
        )
        for state in runtime_lane_health._RUNTIME_LANE_HEALTH.values():
            state.last_success_monotonic -= (
                runtime_lane_health.RUNTIME_LANE_HEALTH_RECENT_SECONDS
                + 1
            )
        with (
            patch.object(main, "HEALTH_FILE", self.marker),
            patch.object(
                main,
                "redis_client",
                SimpleNamespace(
                    ping=AsyncMock(return_value=True)
                ),
            ),
            patch.object(main.database, "execute", new=db_execute),
        ):
            healthy = await main.publish_worker_heartbeat_once(plan)

        self.assertFalse(healthy)
        self.assertFalse(self.marker.exists())
        values = db_execute.await_args.args[1]
        self.assertEqual(values["status"], "degraded")

    async def test_db_or_redis_failure_removes_preexisting_marker(self):
        plan = self._platform_plan()
        failure_cases = ("redis", "database")
        for failure in failure_cases:
            with self.subTest(failure=failure):
                self._record_fresh_platform_lanes()
                self.marker.write_text("old", encoding="utf-8")
                redis_ping = AsyncMock(return_value=True)
                db_execute = AsyncMock(return_value=1)
                if failure == "redis":
                    redis_ping.side_effect = RuntimeError(
                        "redis unavailable"
                    )
                else:
                    db_execute.side_effect = RuntimeError(
                        "database unavailable"
                    )
                with (
                    patch.object(main, "HEALTH_FILE", self.marker),
                    patch.object(
                        main,
                        "redis_client",
                        SimpleNamespace(ping=redis_ping),
                    ),
                    patch.object(
                        main.database,
                        "execute",
                        new=db_execute,
                    ),
                    patch.object(main, "structured_log"),
                ):
                    healthy = await main.publish_worker_heartbeat_once(
                        plan
                    )
                self.assertFalse(healthy)
                self.assertFalse(self.marker.exists())

    def test_failure_metadata_is_bounded_and_failure_is_lane_local(self):
        plan = self._platform_plan()
        runtime_lane_health.record_runtime_lane_success(
            "probe",
            "bilibili",
        )
        runtime_lane_health.record_runtime_lane_success(
            "calibration",
            "bilibili",
        )
        runtime_lane_health.record_runtime_lane_failure(
            "probe",
            "bilibili",
            RuntimeError("secret-value-must-not-be-retained"),
        )

        snapshot = runtime_lane_health.runtime_lane_health_snapshot(plan)
        by_lane = {lane["lane"]: lane for lane in snapshot["lanes"]}
        self.assertEqual(
            by_lane["probe:bilibili"]["status"],
            "degraded",
        )
        self.assertEqual(
            by_lane["probe:bilibili"]["last_error_type"],
            "RuntimeError",
        )
        self.assertEqual(
            by_lane["calibration:bilibili"]["status"],
            "healthy",
        )
        self.assertNotIn(
            "secret-value-must-not-be-retained",
            str(snapshot),
        )

    def test_fresh_saturated_progress_keeps_lane_healthy_but_not_errors(
        self,
    ):
        plan = self._platform_plan()
        runtime_lane_health.record_runtime_lane_success(
            "probe",
            "bilibili",
        )
        runtime_lane_health.record_runtime_lane_success(
            "calibration",
            "bilibili",
        )
        probe = runtime_lane_health._RUNTIME_LANE_HEALTH[
            "probe:bilibili"
        ]
        probe.last_success_monotonic -= (
            runtime_lane_health.RUNTIME_LANE_HEALTH_RECENT_SECONDS
            + 1
        )
        runtime_lane_health.record_runtime_lane_progress(
            "probe",
            "bilibili",
            saturated=True,
        )

        by_lane = {
            lane["lane"]: lane
            for lane in runtime_lane_health.runtime_lane_health_snapshot(
                plan
            )["lanes"]
        }
        self.assertEqual(
            by_lane["probe:bilibili"]["status"],
            "healthy",
        )
        self.assertTrue(by_lane["probe:bilibili"]["saturated"])

        runtime_lane_health.record_runtime_lane_failure(
            "probe",
            "bilibili",
            PermissionError("NOPERM"),
        )
        by_lane = {
            lane["lane"]: lane
            for lane in runtime_lane_health.runtime_lane_health_snapshot(
                plan
            )["lanes"]
        }
        self.assertEqual(
            by_lane["probe:bilibili"]["status"],
            "degraded",
        )

    async def test_outbox_incomplete_cycle_is_not_a_success_heartbeat(self):
        shutdown = task_outbox.asyncio.Event()

        async def stop_after_cycle(_seconds):
            shutdown.set()

        with (
            patch.object(
                task_outbox,
                "reclaim_stale_task_outbox",
                new=AsyncMock(return_value=0),
            ),
            patch.object(
                task_outbox,
                "flush_pending_task_outbox",
                new=AsyncMock(
                    return_value={
                        "scanned": 1,
                        "sent": 0,
                        "delivery_failures": 1,
                    }
                ),
            ),
            patch.object(
                task_outbox.asyncio,
                "sleep",
                side_effect=stop_after_cycle,
            ),
            patch.object(task_outbox, "structured_log"),
        ):
            await task_outbox.start_task_outbox_dispatcher(shutdown)

        plan = build_worker_runtime_plan(
            role="control",
            platform_scope="all",
        )
        snapshot = runtime_lane_health.runtime_lane_health_snapshot(plan)
        self.assertEqual(
            snapshot["lanes"][0]["lane"],
            "outbox:shared",
        )
        self.assertEqual(
            snapshot["lanes"][0]["status"],
            "degraded",
        )

    async def test_outbox_lost_claim_race_is_a_healthy_cycle(self):
        shutdown = task_outbox.asyncio.Event()

        async def stop_after_cycle(_seconds):
            shutdown.set()

        with (
            patch.object(
                task_outbox,
                "reclaim_stale_task_outbox",
                new=AsyncMock(return_value=0),
            ),
            patch.object(
                task_outbox,
                "flush_pending_task_outbox",
                new=AsyncMock(
                    return_value={
                        "scanned": 1,
                        "sent": 0,
                        "delivery_failures": 0,
                    }
                ),
            ),
            patch.object(
                task_outbox.asyncio,
                "sleep",
                side_effect=stop_after_cycle,
            ),
        ):
            await task_outbox.start_task_outbox_dispatcher(shutdown)

        plan = build_worker_runtime_plan(
            role="control",
            platform_scope="all",
        )
        snapshot = runtime_lane_health.runtime_lane_health_snapshot(plan)
        self.assertEqual(
            snapshot["lanes"][0]["status"],
            "healthy",
        )

    async def test_control_health_requires_both_enabled_legacy_fanouts(self):
        plan = build_worker_runtime_plan(
            role="control",
            platform_scope="all",
        )
        runtime_lane_health.record_runtime_lane_success("outbox")
        with patch.object(
            main.settings,
            "legacy_control_stream_drain_enabled",
            True,
        ):
            self.assertFalse(main.worker_owned_lanes_ready(plan))
            runtime_lane_health.record_runtime_lane_success(
                "legacy_probe_fanout"
            )
            runtime_lane_health.record_runtime_lane_success(
                "legacy_calibration_fanout"
            )
            self.assertTrue(main.worker_owned_lanes_ready(plan))
            runtime_lane_health.record_runtime_lane_failure(
                "legacy_probe_fanout",
                None,
                RuntimeError("redis unavailable"),
            )
            self.assertFalse(main.worker_owned_lanes_ready(plan))


if __name__ == "__main__":
    unittest.main()
