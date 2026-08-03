"""Deterministic scheduling tests for independent adapter-probe lanes."""

from __future__ import annotations

import asyncio
import sys
import unittest
from collections import defaultdict
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

from app import adapter_probe  # noqa: E402
from app.adapter_probe_streams import (  # noqa: E402
    adapter_probe_stream_binding_for_platform,
    adapter_probe_stream_bindings,
)


def probe(probe_id: str, platform: str) -> tuple[str, dict]:
    return (
        f"{probe_id}-0",
        {
            "probe_id": probe_id,
            "platform": platform,
            "account_id": "7",
            "lottery_id": "11",
            "target_url": f"https://example.invalid/{probe_id}",
            "canonical_url": f"{platform}://target/{probe_id}",
            "execution_path_id": f"{platform}_test_v1",
            "target_hash": "a" * 64,
            "rule_snapshot_id": "13",
            "rule_hash": "b" * 64,
            "action_plan_hash": "c" * 64,
            "config_hash": "d" * 64,
            "execution_revision": "2",
            "account_lease_id": f"lease-{probe_id}",
            "account_lease_generation": "1",
        },
    )


class FakeRedis:
    def __init__(
        self,
        entries_by_stream: dict[str, list[tuple[str, dict]]],
        shutdown_event: asyncio.Event,
        *,
        failing_group_streams: frozenset[str] = frozenset(),
    ):
        self.entries = {
            stream: list(entries)
            for stream, entries in entries_by_stream.items()
        }
        self.shutdown_event = shutdown_event
        self.failing_group_streams = failing_group_streams
        self.acks: list[tuple[str, str]] = []
        self.refreshes: list[tuple[str, tuple[str, ...]]] = []
        self.group_calls = defaultdict(int)

    async def xinfo_groups(self, stream):
        self.group_calls[str(stream)] += 1
        if str(stream) in self.failing_group_streams:
            raise RuntimeError("NOPERM isolated lane")
        binding = next(
            item
            for item in adapter_probe_stream_bindings(
                include_legacy=True
            )
            if item.stream_key == str(stream)
        )
        return [{"name": binding.group_name}]

    async def xpending_range(self, *_args, **_kwargs):
        return []

    async def xreadgroup(self, _group, _consumer, streams, **kwargs):
        stream = str(next(iter(streams)))
        entries = self.entries.setdefault(stream, [])
        if entries:
            count = int(kwargs.get("count") or 1)
            batch = entries[:count]
            del entries[:count]
            return [(stream, batch)]
        await asyncio.sleep(0.001)
        return []

    async def xack(self, stream, _group, message_id):
        self.acks.append((str(stream), str(message_id)))
        return 1

    async def eval(self, _script, _key_count, stream, _group, message_id):
        self.acks.append((str(stream), str(message_id)))
        return [1, 1]

    async def xclaim(self, stream, *_args, **kwargs):
        message_ids = tuple(
            str(value) for value in kwargs.get("message_ids", ())
        )
        if kwargs.get("justid"):
            self.refreshes.append((str(stream), message_ids))
            return list(message_ids)
        return []


class AdapterProbePlatformIsolationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_live_terminal_ack_uses_group_safe_stream_retirement(self):
        binding = adapter_probe_stream_binding_for_platform("weibo")
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=[1, 1])
        with patch.object(adapter_probe, "redis", fake_redis):
            await adapter_probe._ack_terminal_probe_message(binding, "9-0")

        args = fake_redis.eval.await_args.args
        self.assertIn("XDEL", args[0])
        self.assertEqual(
            args[1:],
            (1, binding.stream_key, binding.group_name, "9-0"),
        )
        fake_redis.xack.assert_not_awaited()

    async def run_loop(self, fake_redis, shutdown_event, **patches):
        stack = [
            patch.object(adapter_probe, "redis", fake_redis),
            patch.object(
                adapter_probe.settings,
                "legacy_control_stream_drain_enabled",
                False,
            ),
            patch.object(adapter_probe, "structured_log"),
        ]
        stack.extend(
            patch.object(adapter_probe, name, value)
            for name, value in patches.items()
        )
        for context in stack:
            context.start()
            self.addCleanup(context.stop)
        return asyncio.create_task(
            adapter_probe.probe_loop(object(), shutdown_event)
        )

    async def test_stuck_recovery_does_not_block_live_platform_ingestion(self):
        shutdown_event = asyncio.Event()
        weibo = adapter_probe_stream_binding_for_platform("weibo")
        fake_redis = FakeRedis(
            {weibo.stream_key: [probe("1", "weibo")]},
            shutdown_event,
        )
        recovery_started = asyncio.Event()
        never_finish = asyncio.Event()

        async def stuck_recovery(binding):
            if binding.platform == "weibo":
                recovery_started.set()
                await never_finish.wait()
            return 0

        loop_task = await self.run_loop(
            fake_redis,
            shutdown_event,
            reclaim_stale_probe_messages=stuck_recovery,
            handle_probe=AsyncMock(return_value=True),
        )
        await asyncio.wait_for(recovery_started.wait(), timeout=1)
        for _ in range(100):
            if (weibo.stream_key, "1-0") in fake_redis.acks:
                break
            await asyncio.sleep(0.01)
        self.assertIn((weibo.stream_key, "1-0"), fake_redis.acks)
        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

    async def test_first_32_bilibili_waiters_do_not_hide_33rd_weibo(self):
        shutdown_event = asyncio.Event()
        bilibili = adapter_probe_stream_binding_for_platform("bilibili")
        weibo = adapter_probe_stream_binding_for_platform("weibo")
        fake_redis = FakeRedis(
            {
                bilibili.stream_key: [
                    probe(str(index), "bilibili")
                    for index in range(1, 33)
                ],
                weibo.stream_key: [probe("33", "weibo")],
            },
            shutdown_event,
        )
        release_bilibili = asyncio.Event()
        weibo_finished = asyncio.Event()

        async def execute(_pool, message):
            if message["platform"] == "bilibili":
                await release_bilibili.wait()
            else:
                weibo_finished.set()
            return True

        loop_task = await self.run_loop(
            fake_redis,
            shutdown_event,
            handle_probe=execute,
        )
        # Python 3.10 asyncio debug mode can spend close to one second merely
        # scheduling the 32 intentionally blocked sibling tasks.
        await asyncio.wait_for(weibo_finished.wait(), timeout=3)
        self.assertIn((weibo.stream_key, "33-0"), fake_redis.acks)
        self.assertNotIn((bilibili.stream_key, "1-0"), fake_redis.acks)
        release_bilibili.set()
        for _ in range(200):
            if sum(
                stream == bilibili.stream_key
                for stream, _message_id in fake_redis.acks
            ) == 32:
                break
            await asyncio.sleep(0.01)
        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=3)
        self.assertEqual(
            sum(
                stream == bilibili.stream_key
                for stream, _message_id in fake_redis.acks
            ),
            32,
        )

    async def test_group_verify_failure_is_local_to_one_platform(self):
        shutdown_event = asyncio.Event()
        bilibili = adapter_probe_stream_binding_for_platform("bilibili")
        weibo = adapter_probe_stream_binding_for_platform("weibo")
        fake_redis = FakeRedis(
            {weibo.stream_key: [probe("1", "weibo")]},
            shutdown_event,
            failing_group_streams=frozenset({bilibili.stream_key}),
        )
        handled = asyncio.Event()

        async def execute(_pool, _message):
            handled.set()
            return True

        loop_task = await self.run_loop(
            fake_redis,
            shutdown_event,
            handle_probe=execute,
        )
        await asyncio.wait_for(handled.wait(), timeout=1)
        self.assertGreaterEqual(fake_redis.group_calls[bilibili.stream_key], 1)
        self.assertIn((weibo.stream_key, "1-0"), fake_redis.acks)
        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)

    async def test_same_platform_is_serial(self):
        shutdown_event = asyncio.Event()
        douyin = adapter_probe_stream_binding_for_platform("douyin")
        fake_redis = FakeRedis(
            {
                douyin.stream_key: [
                    probe("1", "douyin"),
                    probe("2", "douyin"),
                ]
            },
            shutdown_event,
        )
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        active = 0
        maximum_active = 0

        async def execute(_pool, message):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if message["probe_id"] == "1":
                    await release_first.wait()
                else:
                    second_finished.set()
                return True
            finally:
                active -= 1

        loop_task = await self.run_loop(
            fake_redis,
            shutdown_event,
            handle_probe=execute,
        )
        await asyncio.sleep(0.05)
        self.assertFalse(second_finished.is_set())
        release_first.set()
        await asyncio.wait_for(second_finished.wait(), timeout=1)
        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)
        self.assertEqual(maximum_active, 1)

    async def test_wrong_platform_envelope_never_reaches_handler(self):
        shutdown_event = asyncio.Event()
        bilibili = adapter_probe_stream_binding_for_platform("bilibili")
        fake_redis = FakeRedis(
            {
                bilibili.stream_key: [
                    probe("1", "weibo"),
                ]
            },
            shutdown_event,
        )
        rejected = asyncio.Event()

        async def reject(_probe, _exc, *, source_stream_key):
            self.assertEqual(source_stream_key, bilibili.stream_key)
            rejected.set()
            return True

        handler = AsyncMock(return_value=True)
        loop_task = await self.run_loop(
            fake_redis,
            shutdown_event,
            settle_rejected_probe_claim=reject,
            handle_probe=handler,
        )
        await asyncio.wait_for(rejected.wait(), timeout=1)
        shutdown_event.set()
        await asyncio.wait_for(loop_task, timeout=1)
        handler.assert_not_awaited()
        self.assertIn((bilibili.stream_key, "1-0"), fake_redis.acks)


if __name__ == "__main__":
    unittest.main()
