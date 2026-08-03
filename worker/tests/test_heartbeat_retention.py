import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app import heartbeat_retention
from app import main as worker_main
from app.runtime_scope import build_worker_runtime_plan


class BlockingPool:
    async def context_reaper_loop(self, *_args, **_kwargs):
        await asyncio.Event().wait()


class HeartbeatRetentionTests(unittest.IsolatedAsyncioTestCase):
    def test_cleanup_pass_is_frequent_enough_to_clear_stale_metadata(self):
        self.assertLessEqual(
            heartbeat_retention.HEARTBEAT_RETENTION_INTERVAL_SECONDS,
            heartbeat_retention.REDIS_CONSUMER_RETIRE_IDLE_SECONDS,
        )

    async def test_cleanup_is_worker_scoped_ordered_and_bounded(self):
        execute = AsyncMock(return_value=3)
        with patch.object(
            heartbeat_retention,
            "execute_affected_rows",
            new=execute,
        ):
            observed = (
                await heartbeat_retention.retire_stale_worker_heartbeats_once()
            )

        self.assertEqual(observed, {"heartbeats": 3})
        query, values = execute.await_args.args
        normalized_query = " ".join(str(query).split())
        self.assertIn(
            "WHERE service_name = 'worker'",
            normalized_query,
        )
        self.assertIn(
            "ORDER BY last_seen_at ASC, worker_id ASC",
            normalized_query,
        )
        self.assertIn("LIMIT :limit", normalized_query)
        self.assertEqual(
            values["limit"],
            heartbeat_retention.HEARTBEAT_RETIREMENT_MAX_PER_PASS,
        )

    async def test_only_control_role_owns_heartbeat_retention(self):
        async def block(*_args, **_kwargs):
            await asyncio.Event().wait()

        patched_loops = (
            "heartbeat_loop",
            "reload_signal_loop",
            "start_task_outbox_dispatcher",
            "login_loop",
            "heartbeat_retention_loop",
            "legacy_probe_fanout_loop",
            "legacy_calibration_fanout_loop",
            "calibration_loop",
            "probe_loop",
            "task_loop",
        )
        with ExitStack() as stack:
            for name in patched_loops:
                stack.enter_context(
                    patch.object(worker_main, name, new=block)
                )

            control_tasks = worker_main.start_worker_runtime_tasks(
                BlockingPool(),
                asyncio.Event(),
                build_worker_runtime_plan(
                    role="control",
                    platform_scope="all",
                ),
            )
            platform_tasks = worker_main.start_worker_runtime_tasks(
                BlockingPool(),
                asyncio.Event(),
                build_worker_runtime_plan(
                    role="platform",
                    platform_scope="bilibili",
                ),
            )
            await asyncio.sleep(0)

            control_names = {
                task.get_name() for task in control_tasks
            }
            platform_names = {
                task.get_name() for task in platform_tasks
            }
            self.assertIn(
                "worker:heartbeat-retention",
                control_names,
            )
            self.assertNotIn(
                "worker:heartbeat-retention",
                platform_names,
            )
            self.assertIn("worker:task-outbox", control_names)
            self.assertNotIn("worker:task-outbox", platform_names)
            self.assertIn("worker:task-consumer", platform_names)
            self.assertNotIn("worker:task-consumer", control_names)
            self.assertIn(
                "worker:legacy-probe-fanout",
                control_names,
            )
            self.assertIn(
                "worker:legacy-calibration-fanout",
                control_names,
            )
            self.assertNotIn(
                "worker:legacy-probe-fanout",
                platform_names,
            )
            self.assertNotIn(
                "worker:legacy-calibration-fanout",
                platform_names,
            )

            for task in (*control_tasks, *platform_tasks):
                task.cancel()
            await asyncio.gather(
                *control_tasks,
                *platform_tasks,
                return_exceptions=True,
            )


if __name__ == "__main__":
    unittest.main()
