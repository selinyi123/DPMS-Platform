import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main, task_runner
from app.task_streams import repair_task_stream_binding_for_platform


class WorkerTaskSupervisionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _control_plan():
        return main.WorkerRuntimePlan(
            role="control",
            platforms=(),
            owns_platform_lanes=False,
            owns_control_loops=True,
            owns_legacy_fanout=True,
        )

    async def test_startup_redis_failure_runs_resource_cleanup(self):
        plan = self._control_plan()
        pool = SimpleNamespace()
        connect = AsyncMock(
            side_effect=(None, RuntimeError("redis init failed"))
        )
        cleanup = AsyncMock(return_value=())
        with (
            patch.object(main, "clear_stale_health_marker"),
            patch.object(main, "build_worker_runtime_plan", return_value=plan),
            patch.object(
                main,
                "validate_worker_deployment_plan",
                return_value=plan,
            ),
            patch.object(main, "preflight_worker_platform_modules"),
            patch.object(main, "ensure_evidence_storage_ready", AsyncMock()) as evidence,
            patch.object(main, "connect_with_retry", connect),
            patch.object(main, "BrowserPool", return_value=pool),
            patch.object(main, "shutdown_worker_resources", cleanup),
            patch.object(main, "structured_log"),
            self.assertRaisesRegex(RuntimeError, "redis init failed"),
        ):
            await main.main()

        evidence.assert_not_awaited()
        self.assertEqual(connect.await_count, 2)
        cleanup.assert_awaited_once_with(
            pool,
            plan,
            worker_started=False,
        )

    async def test_pool_init_failure_runs_resource_cleanup(self):
        plan = self._control_plan()
        pool = SimpleNamespace(
            init=AsyncMock(side_effect=RuntimeError("pool init failed"))
        )
        cleanup = AsyncMock(return_value=())
        with (
            patch.object(main, "clear_stale_health_marker"),
            patch.object(main, "build_worker_runtime_plan", return_value=plan),
            patch.object(
                main,
                "validate_worker_deployment_plan",
                return_value=plan,
            ),
            patch.object(main, "preflight_worker_platform_modules"),
            patch.object(main, "ensure_evidence_storage_ready", AsyncMock()) as evidence,
            patch.object(main, "connect_with_retry", AsyncMock()),
            patch.object(main, "verify_event_schema", AsyncMock()),
            patch.object(main, "record_event", AsyncMock()),
            patch.object(main, "BrowserPool", return_value=pool),
            patch.object(main, "shutdown_worker_resources", cleanup),
            patch.object(main, "structured_log"),
            patch.object(
                main.settings,
                "redis_acl_preflight_required",
                False,
            ),
            self.assertRaisesRegex(RuntimeError, "pool init failed"),
        ):
            await main.main()

        evidence.assert_not_awaited()
        cleanup.assert_awaited_once_with(
            pool,
            plan,
            worker_started=True,
        )

    async def test_shutdown_continues_after_browser_and_event_failures(self):
        pool = SimpleNamespace(
            close=AsyncMock(side_effect=RuntimeError("browser failed"))
        )
        disconnect = AsyncMock()
        heartbeat_update = AsyncMock()
        close_redis = AsyncMock()
        runtime_plan = main.WorkerRuntimePlan(
            role="control",
            platforms=(),
            owns_platform_lanes=False,
            owns_control_loops=True,
            owns_legacy_fanout=True,
        )
        with (
            patch.object(
                main,
                "record_event",
                new=AsyncMock(
                    side_effect=RuntimeError("event store failed")
                ),
            ),
            patch.object(
                main.database,
                "execute",
                new=heartbeat_update,
            ),
            patch.object(
                main.database,
                "disconnect",
                new=disconnect,
            ),
            patch.object(
                main.redis_client,
                "close",
                new=close_redis,
            ),
            patch.object(main, "structured_log"),
            patch.object(main, "_set_worker_health_marker"),
        ):
            failed = await main.shutdown_worker_resources(
                pool,
                runtime_plan,
            )

        self.assertEqual(
            failed,
            ("browser_pool", "worker_stopped_event"),
        )
        disconnect.assert_awaited_once_with()
        close_redis.assert_awaited_once_with()
        heartbeat_update.assert_awaited_once()

    async def test_shutdown_timeout_does_not_skip_db_or_redis_close(self):
        async def hang():
            await asyncio.Event().wait()

        pool = SimpleNamespace(
            close=AsyncMock(side_effect=hang)
        )
        heartbeat_update = AsyncMock()
        disconnect = AsyncMock()
        close_redis = AsyncMock()
        runtime_plan = main.WorkerRuntimePlan(
            role="control",
            platforms=(),
            owns_platform_lanes=False,
            owns_control_loops=True,
            owns_legacy_fanout=True,
        )
        with (
            patch.object(main, "record_event", new=AsyncMock()),
            patch.object(
                main.database,
                "execute",
                new=heartbeat_update,
            ),
            patch.object(
                main.database,
                "disconnect",
                new=disconnect,
            ),
            patch.object(
                main.redis_client,
                "close",
                new=close_redis,
            ),
            patch.object(
                main,
                "WORKER_SHUTDOWN_STEP_TIMEOUT_SECONDS",
                0.01,
            ),
            patch.object(main, "structured_log"),
            patch.object(main, "_set_worker_health_marker"),
        ):
            failed = await main.shutdown_worker_resources(
                pool,
                runtime_plan,
            )

        self.assertEqual(failed, ("browser_pool",))
        self.assertIn("status = 'stopped'", heartbeat_update.await_args.args[0])
        disconnect.assert_awaited_once_with()
        close_redis.assert_awaited_once_with()

    async def test_exception_exits_and_cancels_siblings(self):
        sibling_stopped = asyncio.Event()

        async def failing():
            raise RuntimeError("task consumer failed")

        async def sibling():
            try:
                await asyncio.Event().wait()
            finally:
                sibling_stopped.set()

        shutdown = asyncio.Event()
        failed = asyncio.create_task(
            failing(),
            name="worker:test-failing",
        )
        peer = asyncio.create_task(
            sibling(),
            name="worker:test-peer",
        )
        await asyncio.sleep(0)

        with (
            patch.object(main, "structured_log") as log,
            self.assertRaisesRegex(RuntimeError, "task consumer failed"),
        ):
            await main.supervise_worker_tasks((failed, peer), shutdown)

        self.assertTrue(shutdown.is_set())
        self.assertTrue(peer.done())
        self.assertTrue(sibling_stopped.is_set())
        log.assert_called_once()
        self.assertEqual(
            log.call_args.kwargs["outcome"],
            "exception",
        )

    async def test_normal_return_is_fatal_and_cancels_siblings(self):
        sibling_stopped = asyncio.Event()

        async def returned():
            return None

        async def sibling():
            try:
                await asyncio.Event().wait()
            finally:
                sibling_stopped.set()

        shutdown = asyncio.Event()
        completed = asyncio.create_task(
            returned(),
            name="worker:test-return",
        )
        peer = asyncio.create_task(
            sibling(),
            name="worker:test-peer",
        )
        await asyncio.sleep(0)

        with (
            patch.object(main, "structured_log") as log,
            self.assertRaisesRegex(
                RuntimeError,
                "worker_critical_task_returned:worker:test-return",
            ),
        ):
            await main.supervise_worker_tasks(
                (completed, peer),
                shutdown,
            )

        self.assertTrue(shutdown.is_set())
        self.assertTrue(peer.done())
        self.assertTrue(sibling_stopped.is_set())
        self.assertEqual(
            log.call_args.kwargs["outcome"],
            "unexpected_return",
        )

    async def test_requested_shutdown_cancels_all_without_failure(self):
        stopped = [asyncio.Event(), asyncio.Event()]

        async def sibling(index):
            try:
                await asyncio.Event().wait()
            finally:
                stopped[index].set()

        shutdown = asyncio.Event()
        tasks = tuple(
            asyncio.create_task(
                sibling(index),
                name=f"worker:test-peer-{index}",
            )
            for index in range(2)
        )
        await asyncio.sleep(0)
        shutdown.set()

        with patch.object(main, "structured_log") as log:
            await main.supervise_worker_tasks(tasks, shutdown)

        self.assertTrue(all(task.done() for task in tasks))
        self.assertTrue(all(event.is_set() for event in stopped))
        log.assert_not_called()


class WorkerTaskLaneHeartbeatTests(unittest.TestCase):
    def setUp(self):
        task_runner._reset_task_lane_health_for_tests()

    def tearDown(self):
        task_runner._reset_task_lane_health_for_tests()

    def test_snapshot_is_lane_local_bounded_and_does_not_store_error_text(
        self,
    ):
        bilibili = repair_task_stream_binding_for_platform("bilibili")
        weibo = repair_task_stream_binding_for_platform("weibo")
        task_runner._record_task_lane_success(
            bilibili,
            "xreadgroup",
        )
        task_runner._record_task_lane_success(
            weibo,
            "xreadgroup",
        )
        task_runner._record_task_lane_failure(
            bilibili,
            "xreadgroup",
            RuntimeError("secret-token-must-not-enter-heartbeat"),
        )

        encoded = main.build_worker_heartbeat_detail()
        self.assertLessEqual(
            len(encoded.encode("utf-8")),
            main.WORKER_HEARTBEAT_DETAIL_MAX_BYTES,
        )
        self.assertNotIn(
            "secret-token-must-not-enter-heartbeat",
            encoded,
        )
        detail = json.loads(encoded)
        self.assertEqual(
            detail["task_consumer_name"],
            task_runner.CONSUMER_NAME,
        )
        lane_health = detail["task_lane_health"]
        self.assertEqual(lane_health["contract_version"], 2)
        self.assertEqual(len(lane_health["lanes"]), 8)
        by_stream = {
            lane["stream"]: lane
            for lane in lane_health["lanes"]
        }
        self.assertEqual(
            by_stream[bilibili.stream_key]["status"],
            "degraded",
        )
        self.assertEqual(
            by_stream[bilibili.stream_key][
                "consecutive_failures"
            ],
            1,
        )
        self.assertEqual(
            by_stream[bilibili.stream_key]["last_error_type"],
            "RuntimeError",
        )
        self.assertEqual(
            by_stream[weibo.stream_key]["status"],
            "healthy",
        )

    def test_saturated_progress_keeps_old_read_healthy_without_rewriting_it(
        self,
    ):
        binding = repair_task_stream_binding_for_platform("bilibili")
        with patch.object(
            task_runner.time,
            "monotonic",
            side_effect=(100.0, 160.0, 161.0),
        ):
            task_runner._record_task_lane_success(
                binding,
                "xreadgroup",
            )
            task_runner._record_task_lane_loop_progress(
                binding,
                "capacity_wait",
                inflight_count=32,
            )
            lane = next(
                item
                for item in task_runner.task_lane_health_snapshot()["lanes"]
                if item["stream"] == binding.stream_key
            )

        self.assertEqual(lane["status"], "healthy")
        self.assertEqual(lane["last_success_operation"], "xreadgroup")
        self.assertEqual(lane["last_success_age_seconds"], 61)
        self.assertEqual(
            lane["last_loop_progress_operation"],
            "capacity_wait",
        )
        self.assertEqual(lane["last_loop_progress_age_seconds"], 1)
        self.assertEqual(lane["inflight_count"], 32)
        self.assertEqual(lane["inflight_limit"], 32)
        self.assertIs(lane["saturated"], True)

    def test_stale_or_not_full_capacity_progress_is_not_healthy(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        cases = (
            (160.0, 31, 161.0),
            (100.0, 32, 206.0),
        )
        for progress_at, inflight_count, snapshot_at in cases:
            with self.subTest(
                inflight_count=inflight_count,
                snapshot_at=snapshot_at,
            ):
                task_runner._reset_task_lane_health_for_tests()
                with patch.object(
                    task_runner.time,
                    "monotonic",
                    side_effect=(100.0, progress_at, snapshot_at),
                ):
                    task_runner._record_task_lane_success(
                        binding,
                        "xreadgroup",
                    )
                    task_runner._record_task_lane_loop_progress(
                        binding,
                        "capacity_wait",
                        inflight_count=inflight_count,
                    )
                    lane = next(
                        item
                        for item in (
                            task_runner.task_lane_health_snapshot()["lanes"]
                        )
                        if item["stream"] == binding.stream_key
                    )
                self.assertEqual(lane["status"], "degraded")

    def test_loop_progress_contract_rejects_unbounded_or_unknown_fields(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        for count in (-1, 33, True, "32"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                task_runner._record_task_lane_loop_progress(
                    binding,
                    "capacity_wait",
                    inflight_count=count,
                )
        with self.assertRaises(ValueError):
            task_runner._record_task_lane_loop_progress(
                binding,
                "task-secret",
                inflight_count=32,
            )

    def test_group_creation_alone_never_marks_lane_healthy(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        task_runner._record_task_lane_success(
            binding,
            "xgroup_create",
        )
        lane = next(
            item
            for item in task_runner.task_lane_health_snapshot()["lanes"]
            if item["stream"] == binding.stream_key
        )
        self.assertEqual(lane["status"], "starting")
        self.assertEqual(
            lane["last_success_operation"],
            "xgroup_create",
        )


if __name__ == "__main__":
    unittest.main()
