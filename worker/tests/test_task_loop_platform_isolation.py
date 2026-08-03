"""Deterministic concurrency checks for isolated durable task-stream lanes."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app import task_runner  # noqa: E402
from app.task_streams import (  # noqa: E402
    LEGACY_TASK_GROUP_NAME,
    LEGACY_TASK_STREAM_KEY,
    repair_task_stream_binding_for_platform,
    task_stream_binding_for_platform,
    task_stream_bindings,
)


def task_message(message_number: int, platform: str) -> tuple[str, dict[str, str]]:
    return (
        f"{message_number}-0",
        {
            "task_id": f"task-{message_number}",
            "account_id": str(message_number),
            "lottery_id": str(message_number + 100),
            "platform": platform,
            # Shadow messages exercise real platform routing without requiring
            # an executable dry-run plan; execution itself is stubbed below.
            "mode": "shadow_run",
            "selector_config": "{}",
        },
    )


class ScriptedRedis:
    def __init__(self, shutdown_event: asyncio.Event, entries_by_stream):
        self.shutdown_event = shutdown_event
        self.entries_by_stream = {
            stream: list(entries)
            for stream, entries in entries_by_stream.items()
        }
        self.acked: list[tuple[str, str, str]] = []
        self.ack_events = {
            (stream, str(message_id)): asyncio.Event()
            for stream, entries in self.entries_by_stream.items()
            for message_id, _data in entries
        }
        self.claim_calls = []
        self.groups_verified = []

    async def xinfo_groups(self, stream):
        binding = next(
            item
            for item in task_stream_bindings(include_legacy=True)
            if item.stream_key == stream
        )
        self.groups_verified.append((stream, binding.group_name))
        return [{"name": binding.group_name}]

    async def xreadgroup(self, group, _consumer, streams, **kwargs):
        stream = next(iter(streams))
        entries = self.entries_by_stream.get(stream, [])
        if entries:
            count = max(1, int(kwargs.get("count") or 1))
            batch = entries[:count]
            self.entries_by_stream[stream] = entries[count:]
            return [(stream, batch)]
        await self.shutdown_event.wait()
        return []

    async def xack(self, stream, group, message_id):
        key = (stream, str(message_id))
        self.acked.append((stream, group, str(message_id)))
        event = self.ack_events.get(key)
        if event is not None:
            event.set()
        return 1

    async def eval(self, _script, key_count, *args):
        if key_count == 1:
            stream, group, message_id = args
        elif key_count == 2:
            stream, _marker_key, group, message_id, _marker_member = args
        else:
            raise AssertionError(f"unexpected key count: {key_count}")
        acknowledged = await self.xack(stream, group, message_id)
        return [acknowledged, 1]

    async def xclaim(self, *args, **kwargs):
        self.claim_calls.append((args, kwargs))
        return list(kwargs.get("message_ids") or [])

    async def xdel(self, *_args, **_kwargs):
        return 1


class TaskLoopPlatformIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_runner._reset_task_lane_health_for_tests()

    def tearDown(self):
        task_runner._reset_task_lane_health_for_tests()

    async def run_loop(self, redis, execute, *, validate=None):
        patches = (
            patch.object(task_runner, "redis", redis),
            patch.object(
                task_runner,
                "execute_task_with_phases",
                side_effect=execute,
            ),
            patch.object(task_runner, "get_adapter", return_value=object()),
            patch.object(task_runner, "dead_letter_message", new=AsyncMock()),
            patch.object(task_runner, "structured_log"),
        )
        if validate is not None:
            patches = (
                *patches,
                patch.object(
                    task_runner,
                    "validate_task_message",
                    side_effect=validate,
                ),
            )
        for current in patches:
            current.start()
            self.addCleanup(current.stop)
        loop_task = asyncio.create_task(
            task_runner.task_loop(None, redis.shutdown_event)
        )
        self.addAsyncCleanup(self.stop_loop, loop_task, redis.shutdown_event)
        return loop_task

    async def stop_loop(self, loop_task, shutdown_event):
        shutdown_event.set()
        if not loop_task.done():
            await asyncio.wait_for(loop_task, timeout=2)

    async def wait_ack(self, redis, stream, message_id):
        await asyncio.wait_for(
            redis.ack_events[(stream, message_id)].wait(),
            timeout=1,
        )

    async def test_capacity_wait_publishes_live_saturation_for_32_inflight(
        self,
    ):
        binding = repair_task_stream_binding_for_platform("bilibili")
        shutdown = asyncio.Event()
        never_release = asyncio.Event()
        tasks = tuple(
            asyncio.create_task(never_release.wait())
            for _ in range(task_runner.TASK_DISPATCH_MAX_INFLIGHT)
        )
        inflight = {
            execution: task_runner._DispatchedTaskMessage(
                message_id=f"{index}-0",
                task={},
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )
            for index, execution in enumerate(tasks)
        }
        capacity_wait = asyncio.create_task(
            task_runner._wait_for_dispatch_capacity(
                binding,
                inflight,
                shutdown,
            )
        )
        try:
            for _ in range(20):
                lane = next(
                    item
                    for item in (
                        task_runner.task_lane_health_snapshot()["lanes"]
                    )
                    if item["stream"] == binding.stream_key
                )
                if (
                    lane["last_loop_progress_operation"]
                    == "capacity_wait"
                ):
                    break
                await asyncio.sleep(0)
            else:
                self.fail("capacity wait did not publish lane progress")
            self.assertEqual(lane["inflight_count"], 32)
            self.assertEqual(lane["inflight_limit"], 32)
            self.assertIs(lane["saturated"], True)

            shutdown.set()
            self.assertFalse(
                await asyncio.wait_for(capacity_wait, timeout=1)
            )
        finally:
            shutdown.set()
            if not capacity_wait.done():
                capacity_wait.cancel()
            for execution in tasks:
                execution.cancel()
            await asyncio.gather(
                capacity_wait,
                *tasks,
                return_exceptions=True,
            )

    async def test_saturated_bilibili_stream_does_not_block_weibo(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")
        weibo = task_stream_binding_for_platform("weibo")
        redis = ScriptedRedis(
            shutdown,
            {
                bilibili.stream_key: [
                    task_message(number, "bilibili")
                    for number in range(1, 65)
                ],
                weibo.stream_key: [task_message(100, "weibo")],
            },
        )
        bilibili_started = asyncio.Event()
        release_bilibili = asyncio.Event()
        weibo_started = asyncio.Event()

        async def execute(task, _adapter, _pool, _message_id):
            if task["platform"] == "bilibili":
                bilibili_started.set()
                await release_bilibili.wait()
            else:
                weibo_started.set()
            return True

        await self.run_loop(redis, execute)
        await asyncio.wait_for(bilibili_started.wait(), timeout=1)
        # Bilibili can fill its entire independent in-flight budget while the
        # Weibo stream still owns a separate read budget and execution lock.
        await asyncio.wait_for(weibo_started.wait(), timeout=1)
        await self.wait_ack(redis, weibo.stream_key, "100-0")
        self.assertFalse(
            any(item[0] == bilibili.stream_key for item in redis.acked)
        )

        release_bilibili.set()
        await self.wait_ack(redis, bilibili.stream_key, "1-0")

    async def test_same_platform_remains_serial(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")
        redis = ScriptedRedis(
            shutdown,
            {
                bilibili.stream_key: [
                    task_message(1, "bilibili"),
                    task_message(2, "bilibili"),
                ]
            },
        )
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        maximum_active = 0

        async def execute(task, _adapter, _pool, _message_id):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if task["task_id"] == "task-1":
                    first_started.set()
                    await release_first.wait()
                else:
                    second_started.set()
                return True
            finally:
                active -= 1

        await self.run_loop(redis, execute)
        await asyncio.wait_for(first_started.wait(), timeout=1)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.05)
        self.assertEqual(maximum_active, 1)

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        await self.wait_ack(redis, bilibili.stream_key, "2-0")
        self.assertEqual(maximum_active, 1)

    async def test_worker_never_executes_legacy_stream_directly(self):
        shutdown = asyncio.Event()
        redis = ScriptedRedis(
            shutdown,
            {
                LEGACY_TASK_STREAM_KEY: [task_message(1, "bilibili")],
            },
        )
        execute = AsyncMock(return_value=True)

        await self.run_loop(redis, execute)
        async def all_groups_verified():
            while len(redis.groups_verified) < 8:
                await asyncio.sleep(0)

        await asyncio.wait_for(all_groups_verified(), timeout=1)
        self.assertEqual(
            set(redis.groups_verified),
            {
                (binding.stream_key, binding.group_name)
                for binding in task_stream_bindings(include_legacy=False)
            },
        )
        self.assertNotIn(
            (LEGACY_TASK_STREAM_KEY, LEGACY_TASK_GROUP_NAME),
            redis.groups_verified,
        )
        execute.assert_not_awaited()
        self.assertEqual(redis.acked, [])

    async def test_platform_lane_revalidates_group_after_nogroup(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")

        class LostGroupRedis(ScriptedRedis):
            def __init__(self):
                super().__init__(
                    shutdown,
                    {
                        bilibili.stream_key: [
                            task_message(1, "bilibili")
                        ]
                    },
                )
                self.lost_once = False

            async def xreadgroup(
                self,
                group,
                consumer,
                streams,
                **kwargs,
            ):
                stream = next(iter(streams))
                if stream == bilibili.stream_key and not self.lost_once:
                    self.lost_once = True
                    raise RuntimeError(
                        "NOGROUP No such key or consumer group"
                    )
                return await super().xreadgroup(
                    group,
                    consumer,
                    streams,
                    **kwargs,
                )

        redis = LostGroupRedis()
        original_wait_for = asyncio.wait_for

        async def skip_retry_delay(awaitable, *, timeout):
            if timeout == 5:
                awaitable.close()
                raise asyncio.TimeoutError
            return await original_wait_for(awaitable, timeout=timeout)

        with patch.object(
            task_runner.asyncio,
            "wait_for",
            side_effect=skip_retry_delay,
        ):
            await self.run_loop(
                redis,
                AsyncMock(return_value=True),
            )
            await self.wait_ack(
                redis,
                bilibili.stream_key,
                "1-0",
            )

        self.assertGreaterEqual(
            redis.groups_verified.count(
                (bilibili.stream_key, bilibili.group_name)
            ),
            2,
        )

    async def test_sustained_group_failure_degrades_only_exact_lane(self):
        shutdown = asyncio.Event()
        bilibili = repair_task_stream_binding_for_platform("bilibili")
        weibo = repair_task_stream_binding_for_platform("weibo")
        bilibili_failed = asyncio.Event()
        weibo_polled = asyncio.Event()

        class LaneHealthRedis(ScriptedRedis):
            def __init__(self):
                super().__init__(shutdown, {})

            async def xinfo_groups(self, stream):
                if stream == bilibili.stream_key:
                    bilibili_failed.set()
                    raise RuntimeError("WRONGTYPE")
                return await super().xinfo_groups(stream)

            async def xreadgroup(
                self,
                group,
                consumer,
                streams,
                **kwargs,
            ):
                stream = next(iter(streams))
                if stream == weibo.stream_key:
                    weibo_polled.set()
                    await asyncio.sleep(0)
                    return []
                return await super().xreadgroup(
                    group,
                    consumer,
                    streams,
                    **kwargs,
                )

        redis = LaneHealthRedis()
        locks = {
            "bilibili": asyncio.Lock(),
            "weibo": asyncio.Lock(),
        }
        with patch.object(
            task_runner,
            "redis",
            redis,
        ), patch.object(
            task_runner,
            "structured_log",
        ):
            lane_tasks = (
                asyncio.create_task(
                    task_runner._task_stream_loop(
                        bilibili,
                        None,
                        shutdown,
                        locks,
                    )
                ),
                asyncio.create_task(
                    task_runner._task_stream_loop(
                        weibo,
                        None,
                        shutdown,
                        locks,
                    )
                ),
            )
            try:
                await asyncio.wait_for(
                    bilibili_failed.wait(),
                    timeout=1,
                )
                await asyncio.wait_for(
                    weibo_polled.wait(),
                    timeout=1,
                )
                by_stream = {
                    lane["stream"]: lane
                    for lane in (
                        task_runner.task_lane_health_snapshot()["lanes"]
                    )
                }
                self.assertEqual(
                    by_stream[bilibili.stream_key]["status"],
                    "degraded",
                )
                self.assertEqual(
                    by_stream[bilibili.stream_key][
                        "last_error_operation"
                    ],
                    "consumer_group_verify",
                )
                self.assertEqual(
                    by_stream[weibo.stream_key]["status"],
                    "healthy",
                )
            finally:
                shutdown.set()
                await asyncio.wait_for(
                    asyncio.gather(*lane_tasks),
                    timeout=1,
                )

    async def test_wrong_platform_entry_is_rejected_in_its_source_lane(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")
        redis = ScriptedRedis(
            shutdown,
            {bilibili.stream_key: [task_message(1, "weibo")]},
        )
        executed = AsyncMock(return_value=True)

        await self.run_loop(redis, executed)
        await self.wait_ack(redis, bilibili.stream_key, "1-0")

        executed.assert_not_awaited()
        self.assertIn(
            (bilibili.stream_key, bilibili.group_name, "1-0"),
            redis.acked,
        )

    async def test_standard_lane_rejects_repair_protocol_message(self):
        shutdown = asyncio.Event()
        standard = task_stream_binding_for_platform("bilibili")
        message_id, payload = task_message(1, "bilibili")
        payload.update(
            {
                "mode": "real_run",
                "execution_intent_kind": "repair",
            }
        )
        redis = ScriptedRedis(
            shutdown,
            {standard.stream_key: [(message_id, payload)]},
        )
        executed = AsyncMock(return_value=True)

        await self.run_loop(
            redis,
            executed,
            validate=lambda task: task,
        )
        await self.wait_ack(redis, standard.stream_key, message_id)

        executed.assert_not_awaited()

    async def test_repair_lane_accepts_only_exact_repair_protocol(self):
        shutdown = asyncio.Event()
        repair = repair_task_stream_binding_for_platform("bilibili")
        accepted_id, accepted = task_message(1, "bilibili")
        accepted.update(
            {
                "mode": "real_run",
                "execution_intent_kind": "repair",
            }
        )
        rejected_id, rejected = task_message(2, "bilibili")
        rejected.update(
            {
                "mode": "real_run",
                "execution_intent_kind": "full",
            }
        )
        redis = ScriptedRedis(
            shutdown,
            {
                repair.stream_key: [
                    (accepted_id, accepted),
                    (rejected_id, rejected),
                ]
            },
        )
        executed = AsyncMock(return_value=True)

        await self.run_loop(
            redis,
            executed,
            validate=lambda task: task,
        )
        await self.wait_ack(redis, repair.stream_key, accepted_id)
        await self.wait_ack(redis, repair.stream_key, rejected_id)

        executed.assert_awaited_once()
        self.assertEqual(
            executed.await_args.args[0]["task_id"],
            "task-1",
        )

    async def test_standard_and_repair_lanes_share_platform_lock(self):
        captured = []

        async def capture(binding, _pool, _shutdown, platform_locks):
            captured.append(
                (
                    binding,
                    platform_locks[binding.platform],
                )
            )

        with patch.object(
            task_runner,
            "_task_stream_loop",
            side_effect=capture,
        ):
            await task_runner.task_loop(None, asyncio.Event())

        self.assertEqual(len(captured), 8)
        for platform in ("bilibili", "weibo", "xiaohongshu", "douyin"):
            platform_lanes = [
                item for item in captured if item[0].platform == platform
            ]
            self.assertEqual(len(platform_lanes), 2)
            self.assertIs(platform_lanes[0][1], platform_lanes[1][1])

    async def test_failed_entry_does_not_kill_sibling_or_its_lane(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")
        weibo = task_stream_binding_for_platform("weibo")
        redis = ScriptedRedis(
            shutdown,
            {
                bilibili.stream_key: [
                    task_message(1, "bilibili"),
                    task_message(3, "bilibili"),
                ],
                weibo.stream_key: [task_message(2, "weibo")],
            },
        )
        weibo_ran = asyncio.Event()
        later_bilibili_ran = asyncio.Event()

        async def execute(task, _adapter, _pool, _message_id):
            if task["task_id"] == "task-1":
                raise RuntimeError("settlement is deliberately unconfirmed")
            if task["platform"] == "weibo":
                weibo_ran.set()
            else:
                later_bilibili_ran.set()
            return True

        await self.run_loop(redis, execute)
        await asyncio.wait_for(weibo_ran.wait(), timeout=1)
        await asyncio.wait_for(later_bilibili_ran.wait(), timeout=1)
        await self.wait_ack(redis, weibo.stream_key, "2-0")
        await self.wait_ack(redis, bilibili.stream_key, "3-0")
        self.assertNotIn(
            (bilibili.stream_key, bilibili.group_name, "1-0"),
            redis.acked,
        )

    async def test_fanout_marker_is_deleted_only_after_terminal_execution(
        self,
    ):
        binding = task_stream_binding_for_platform("bilibili")
        message_id, task = task_message(1, "bilibili")
        task.update(
            {
                task_runner.LEGACY_SOURCE_STREAM_FIELD: (
                    LEGACY_TASK_STREAM_KEY
                ),
                task_runner.LEGACY_SOURCE_MESSAGE_ID_FIELD: (
                    "1700000000000-0"
                ),
            }
        )
        dispatched = task_runner._DispatchedTaskMessage(
            message_id=message_id,
            task=task,
            stream_key=binding.stream_key,
            group_name=binding.group_name,
        )
        fake_redis = AsyncMock()
        execution_started = asyncio.Event()
        release_execution = asyncio.Event()

        async def execute(*_args):
            execution_started.set()
            await release_execution.wait()
            return True

        with patch.object(task_runner, "redis", fake_redis), patch.object(
            task_runner,
            "execute_task_with_phases",
            side_effect=execute,
        ), patch.object(
            task_runner,
            "get_adapter",
            return_value=object(),
        ), patch.object(task_runner, "structured_log"):
            execution = asyncio.create_task(
                task_runner._execute_dispatched_task(
                    dispatched,
                    asyncio.Lock(),
                    None,
                )
            )
            await asyncio.wait_for(execution_started.wait(), timeout=1)
            fake_redis.eval.assert_not_awaited()
            fake_redis.xack.assert_not_awaited()

            release_execution.set()
            await asyncio.wait_for(execution, timeout=1)

        fake_redis.xack.assert_not_awaited()
        fake_redis.eval.assert_awaited_once()
        args = fake_redis.eval.await_args.args
        self.assertIn("SISMEMBER", args[0])
        self.assertIn("XINFO", args[0])
        self.assertIn("XPENDING", args[0])
        self.assertIn("XDEL", args[0])
        self.assertIn("SREM", args[0])
        self.assertIn("SCARD", args[0])
        self.assertIn("DEL", args[0])
        self.assertEqual(
            args[1:],
            (
                2,
                binding.stream_key,
                task_runner._legacy_fanout_marker_key(
                    "1700000000000-0"
                ),
                binding.group_name,
                message_id,
                (
                    f"{binding.stream_key}|task-1|"
                    f"{message_id}"
                ),
            ),
        )

    async def test_shutdown_cancels_execution_without_ack(self):
        shutdown = asyncio.Event()
        bilibili = task_stream_binding_for_platform("bilibili")
        redis = ScriptedRedis(
            shutdown,
            {bilibili.stream_key: [task_message(1, "bilibili")]},
        )
        execution_started = asyncio.Event()
        never_release = asyncio.Event()

        async def execute(_task, _adapter, _pool, _message_id):
            execution_started.set()
            await never_release.wait()
            return True

        loop_task = await self.run_loop(redis, execute)
        await asyncio.wait_for(execution_started.wait(), timeout=1)
        loop_task.cancel()
        await asyncio.wait_for(loop_task, timeout=2)
        self.assertEqual(redis.acked, [])


if __name__ == "__main__":
    unittest.main()
