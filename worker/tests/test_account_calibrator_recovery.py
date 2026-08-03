"""Recovery and scheduling contracts for account calibration deliveries."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import account_calibrator
from shared.redis_consumer_groups import expected_consumer_group_names


def calibration(calibration_id: str, platform: str) -> tuple[str, dict]:
    return (
        f"{calibration_id}-0",
        {
            "calibration_id": calibration_id,
            "account_id": "7",
            "platform": platform,
            "check_url": f"https://{platform}.example.invalid/",
        },
    )


class ScriptedRedis:
    def __init__(self, entries, shutdown_event: asyncio.Event):
        self.entries_by_stream: dict[str, list[tuple[str, dict]]] = {}
        for entry in entries:
            platform = str(entry[1].get("platform") or "").strip()
            stream = (
                account_calibrator.account_calibration_stream_binding_for_platform(
                    platform
                ).stream_key
                if platform
                else account_calibrator.STREAM_KEY
            )
            self.entries_by_stream.setdefault(stream, []).append(entry)
        self.shutdown_event = shutdown_event
        self.acks: list[str] = []
        self.refreshes: list[list[str]] = []

    async def xinfo_groups(self, stream):
        return [
            {"name": group_name}
            for group_name in expected_consumer_group_names(str(stream))
        ]

    async def xreadgroup(self, *_args, **kwargs):
        stream = next(iter(kwargs.get("streams") or _args[2]))
        entries = self.entries_by_stream.get(stream, [])
        if entries:
            count = int(kwargs.get("count") or 1)
            batch = entries[:count]
            del entries[:count]
            return [(stream, batch)]
        await self.shutdown_event.wait()
        return []

    async def xpending_range(self, *_args, **_kwargs):
        return []

    async def xclaim(self, *_args, **kwargs):
        message_ids = [str(value) for value in kwargs.get("message_ids", ())]
        if kwargs.get("justid"):
            self.refreshes.append(message_ids)
            return message_ids
        return []

    async def xack(self, _stream, _group, message_id):
        self.acks.append(str(message_id))
        return 1

    async def eval(self, _script, _key_count, _stream, _group, message_id):
        self.acks.append(str(message_id))
        return [1, 1]


class CalibrationPlatformIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_terminal_ack_uses_group_safe_stream_retirement(self):
        binding = (
            account_calibrator.account_calibration_stream_binding_for_platform(
                "douyin"
            )
        )
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=[1, 1])
        with patch.object(account_calibrator, "redis", fake_redis):
            await account_calibrator._ack_terminal_calibration_message(
                binding,
                "8-0",
            )

        args = fake_redis.eval.await_args.args
        self.assertIn("XDEL", args[0])
        self.assertEqual(
            args[1:],
            (1, binding.stream_key, binding.group_name, "8-0"),
        )
        fake_redis.xack.assert_not_awaited()

    def test_platform_consumers_match_worker_heartbeat_identity(self):
        from app.worker_identity import WORKER_ID

        expected = WORKER_ID
        for binding in account_calibrator.account_calibration_stream_bindings(
            include_legacy=False
        ):
            with self.subTest(platform=binding.platform):
                self.assertEqual(
                    account_calibrator.calibration_consumer_name(binding),
                    expected,
                )

    async def test_broken_group_verify_is_local_to_one_platform(self):
        shutdown = asyncio.Event()
        weibo_id = "00000000-0000-0000-0000-000000000041"
        fake_redis = ScriptedRedis(
            [calibration(weibo_id, "weibo")],
            shutdown,
        )
        original_verify = fake_redis.xinfo_groups

        async def group_verify(stream):
            if stream == "account_calibration_requests:bilibili":
                raise RuntimeError("bilibili lane wrongtype")
            return await original_verify(stream)

        fake_redis.xinfo_groups = group_verify
        weibo_finished = asyncio.Event()

        async def process(_pool, _message_id, data, **_kwargs):
            if data["platform"] == "weibo":
                weibo_finished.set()

        async def idle_recovery(
            _pool,
            event,
            _locks=None,
            **_kwargs,
        ):
            await event.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(account_calibrator, "redis", fake_redis),
                patch.object(
                    account_calibrator,
                    "process_calibration_message",
                    side_effect=process,
                ),
                patch.object(
                    account_calibrator,
                    "_calibration_recovery_loop",
                    side_effect=idle_recovery,
                ),
                patch.object(
                    account_calibrator,
                    "PROFILES_ROOT",
                    Path(temp_dir),
                ),
                patch.object(account_calibrator, "structured_log"),
            ):
                loop_task = asyncio.create_task(
                    account_calibrator.calibration_loop(object(), shutdown)
                )
                await asyncio.wait_for(weibo_finished.wait(), timeout=1)
                shutdown.set()
                await asyncio.wait_for(loop_task, timeout=1)

    async def test_wrong_lane_envelope_is_acked_without_execution(self):
        shutdown = asyncio.Event()
        binding = (
            account_calibrator.account_calibration_stream_binding_for_platform(
                "bilibili"
            )
        )
        wrong = calibration(
            "00000000-0000-0000-0000-000000000042",
            "weibo",
        )

        class WrongLaneRedis:
            def __init__(self):
                self.delivered = False
                self.acks = []

            async def xinfo_groups(self, _stream):
                return [{"name": binding.group_name}]

            async def xreadgroup(self, *_args, **_kwargs):
                if not self.delivered:
                    self.delivered = True
                    return [(binding.stream_key, [wrong])]
                await shutdown.wait()
                return []

            async def xack(self, stream, group, message_id):
                self.acks.append((stream, group, str(message_id)))
                shutdown.set()
                return 1

            async def eval(
                self,
                _script,
                _key_count,
                stream,
                group,
                message_id,
            ):
                self.acks.append((stream, group, str(message_id)))
                shutdown.set()
                return [1, 1]

        fake_redis = WrongLaneRedis()
        process = AsyncMock()
        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "process_calibration_message",
                process,
            ),
            patch.object(account_calibrator, "structured_log"),
        ):
            await asyncio.wait_for(
                account_calibrator._calibration_platform_loop(
                    object(),
                    shutdown,
                    binding,
                    asyncio.Lock(),
                ),
                timeout=1,
            )

        process.assert_not_awaited()
        self.assertEqual(
            [
                (
                    binding.stream_key,
                    binding.group_name,
                    wrong[0],
                )
            ],
            fake_redis.acks,
        )

    async def test_first_32_blocked_bilibili_do_not_hide_33rd_weibo(self):
        shutdown = asyncio.Event()
        entries = [
            calibration(f"00000000-0000-0000-0000-{index:012d}", "bilibili")
            for index in range(1, 33)
        ]
        weibo_id = "00000000-0000-0000-0000-000000000033"
        entries.append(calibration(weibo_id, "weibo"))
        fake_redis = ScriptedRedis(entries, shutdown)
        release_bilibili = asyncio.Event()
        weibo_finished = asyncio.Event()
        completed: list[str] = []
        active_by_platform: dict[str, int] = {}
        maximum_by_platform: dict[str, int] = {}

        async def process(_pool, message_id, data, **_kwargs):
            platform = data["platform"]
            active_by_platform[platform] = active_by_platform.get(platform, 0) + 1
            maximum_by_platform[platform] = max(
                maximum_by_platform.get(platform, 0),
                active_by_platform[platform],
            )
            try:
                if platform == "bilibili":
                    await release_bilibili.wait()
                else:
                    weibo_finished.set()
                completed.append(str(message_id))
            finally:
                active_by_platform[platform] -= 1

        async def idle_recovery(
            _pool,
            event,
            _locks=None,
            **_kwargs,
        ):
            await event.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(account_calibrator, "redis", fake_redis),
                patch.object(
                    account_calibrator,
                    "process_calibration_message",
                    side_effect=process,
                ),
                patch.object(
                    account_calibrator,
                    "_calibration_recovery_loop",
                    side_effect=idle_recovery,
                ),
                patch.object(
                    account_calibrator,
                    "PROFILES_ROOT",
                    Path(temp_dir),
                ),
                patch.object(account_calibrator, "structured_log"),
            ):
                loop_task = asyncio.create_task(
                    account_calibrator.calibration_loop(object(), shutdown)
                )
                await asyncio.wait_for(weibo_finished.wait(), timeout=1)
                self.assertIn(f"{weibo_id}-0", completed)
                self.assertEqual(1, maximum_by_platform["bilibili"])
                release_bilibili.set()
                while len(completed) < len(entries):
                    await asyncio.sleep(0)
                shutdown.set()
                await asyncio.wait_for(loop_task, timeout=1)

    async def test_legacy_message_fans_out_from_database_authority(self):
        legacy_id = "00000000-0000-0000-0000-000000000001"
        legacy_message = calibration(legacy_id, "bilibili")
        legacy_message[1].pop("platform")
        fake_redis = SimpleNamespace(
            eval=AsyncMock(return_value="2-0"),
        )
        async def authority(query, values=None):
            self.assertIn("FROM account_calibrations", query)
            return {
                "calibration_id": legacy_id,
                "account_id": 7,
                "platform": "bilibili",
                "check_url": "https://bilibili.example.invalid/",
                "status": "queued",
                "account_platform": "bilibili",
                "account_deleted_at": None,
                "account_execution_revision": 3,
            }

        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "database",
                SimpleNamespace(fetch_one=authority),
            ),
            patch.object(account_calibrator, "structured_log"),
        ):
            moved = await account_calibrator._fanout_legacy_calibration_entry(
                legacy_message[0],
                legacy_message[1],
            )

        self.assertTrue(moved)
        args = fake_redis.eval.await_args.args
        self.assertEqual(
            args[2],
            account_calibrator.STREAM_KEY,
        )
        self.assertEqual(
            args[3],
            "account_calibration_requests:bilibili",
        )
        self.assertIn("XADD", args[0])
        self.assertIn("SET", args[0])
        self.assertIn("XACK", args[0])
        self.assertNotIn("XINFO", args[0])
        self.assertNotIn("XPENDING", args[0])
        self.assertNotIn("XDEL", args[0])
        self.assertEqual(
            args[4],
            account_calibrator._legacy_calibration_fanout_marker_key(
                legacy_message[0]
            ),
        )

    async def test_invalid_legacy_calibration_is_ack_only(self):
        fake_redis = SimpleNamespace(
            xack=AsyncMock(return_value=1),
            eval=AsyncMock(),
        )
        with patch.object(account_calibrator, "redis", fake_redis):
            moved = (
                await account_calibrator
                ._fanout_legacy_calibration_entry(
                    "11-0",
                    {
                        "calibration_id": "not-a-calibration-id",
                        "account_id": "7",
                    },
                )
            )

        self.assertFalse(moved)
        fake_redis.xack.assert_awaited_once_with(
            account_calibrator
            .LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
            account_calibrator
            .LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
            "11-0",
        )
        fake_redis.eval.assert_not_awaited()

    async def test_terminal_legacy_calibration_is_ack_only(self):
        calibration_id = "00000000-0000-0000-0000-000000000019"
        fake_redis = SimpleNamespace(
            xack=AsyncMock(return_value=1),
            eval=AsyncMock(),
        )
        database = SimpleNamespace(
            fetch_one=AsyncMock(
                return_value={
                    "calibration_id": calibration_id,
                    "account_id": 7,
                    "platform": "weibo",
                    "check_url": "https://weibo.example.invalid/",
                    "status": "succeeded",
                    "account_platform": "weibo",
                    "account_deleted_at": None,
                    "account_execution_revision": 4,
                }
            )
        )
        with patch.object(
            account_calibrator,
            "redis",
            fake_redis,
        ), patch.object(
            account_calibrator,
            "database",
            database,
        ):
            moved = (
                await account_calibrator
                ._fanout_legacy_calibration_entry(
                    "12-0",
                    {
                        "calibration_id": calibration_id,
                        "account_id": "7",
                    },
                )
            )

        self.assertFalse(moved)
        fake_redis.xack.assert_awaited_once_with(
            account_calibrator
            .LEGACY_ACCOUNT_CALIBRATION_STREAM_KEY,
            account_calibrator
            .LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
            "12-0",
        )
        fake_redis.eval.assert_not_awaited()

    async def test_waiting_entry_is_refreshed_and_shutdown_leaves_it_pending(self):
        shutdown = asyncio.Event()
        first_id = "00000000-0000-0000-0000-000000000001"
        second_id = "00000000-0000-0000-0000-000000000002"
        fake_redis = ScriptedRedis(
            [calibration(first_id, "douyin"), calibration(second_id, "douyin")],
            shutdown,
        )
        first_started = asyncio.Event()
        never_release = asyncio.Event()

        async def process(_pool, _message_id, data, **_kwargs):
            if data["calibration_id"] == first_id:
                first_started.set()
            await never_release.wait()

        async def idle_recovery(
            _pool,
            event,
            _locks=None,
            **_kwargs,
        ):
            await event.wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(account_calibrator, "redis", fake_redis),
                patch.object(
                    account_calibrator,
                    "process_calibration_message",
                    side_effect=process,
                ),
                patch.object(
                    account_calibrator,
                    "_calibration_recovery_loop",
                    side_effect=idle_recovery,
                ),
                patch.object(
                    account_calibrator,
                    "CALIBRATION_PENDING_REFRESH_SECONDS",
                    0.01,
                ),
                patch.object(
                    account_calibrator,
                    "PROFILES_ROOT",
                    Path(temp_dir),
                ),
                patch.object(account_calibrator, "structured_log"),
            ):
                loop_task = asyncio.create_task(
                    account_calibrator.calibration_loop(object(), shutdown)
                )
                await asyncio.wait_for(first_started.wait(), timeout=1)
                # A refresh snapshot taken just before the first entry acquires
                # its lane is harmless and may complete in the same loop turn.
                # Observe only refreshes made after the active/waiting state has
                # become stable.
                await asyncio.sleep(0)
                fake_redis.refreshes.clear()
                for _ in range(100):
                    if fake_redis.refreshes:
                        break
                    await asyncio.sleep(0.01)
                shutdown.set()
                await asyncio.wait_for(loop_task, timeout=1)

        self.assertTrue(fake_redis.refreshes)
        self.assertTrue(
            any(f"{second_id}-0" in refresh for refresh in fake_redis.refreshes)
        )
        self.assertTrue(
            all(f"{first_id}-0" not in refresh for refresh in fake_redis.refreshes)
        )
        self.assertEqual([], fake_redis.acks)


class CalibrationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_queued_recovery_forces_platform_first_index(self):
        fetch_all = AsyncMock(return_value=[])
        with patch.object(
            account_calibrator.database,
            "fetch_all",
            fetch_all,
        ):
            recovered = (
                await account_calibrator.requeue_stale_queued_calibrations(
                    "weibo"
                )
            )

        self.assertEqual(recovered, 0)
        query, values = fetch_all.await_args.args
        compact = " ".join(query.lower().split())
        self.assertIn(
            "from account_calibrations c force index "
            "(idx_account_calibration_platform_queued)",
            compact,
        )
        self.assertIn("and c.platform = :platform", compact)
        self.assertIn("order by c.created_at, c.id", compact)
        self.assertEqual(values["platform"], "weibo")

    async def test_platform_running_recovery_forces_indexable_order(self):
        fetch_all = AsyncMock(return_value=[])
        with patch.object(
            account_calibrator.database,
            "fetch_all",
            fetch_all,
        ):
            expired = (
                await account_calibrator.expire_stale_running_calibrations(
                    "bilibili"
                )
            )

        self.assertEqual(expired, 0)
        query, values = fetch_all.await_args.args
        compact = " ".join(query.lower().split())
        self.assertIn(
            "from account_calibrations c force index "
            "(idx_account_calibration_platform_running)",
            compact,
        )
        self.assertIn("and c.platform = :platform", compact)
        self.assertIn(
            "order by c.started_at, c.created_at, c.id",
            compact,
        )
        self.assertNotIn("coalesce(", compact)
        self.assertEqual(values["platform"], "bilibili")

    async def test_recovery_replay_shares_live_platform_lock(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000001", "bilibili"
        )[1]
        fake_redis = SimpleNamespace(
            xpending_range=AsyncMock(return_value=[{"message_id": "1-0"}]),
            xclaim=AsyncMock(return_value=[("1-0", data)]),
            xack=AsyncMock(return_value=1),
        )
        reconcile = AsyncMock(side_effect=["queued", "terminal"])
        handle = AsyncMock()
        platform_lock = asyncio.Lock()
        await platform_lock.acquire()

        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                reconcile,
            ),
            patch.object(account_calibrator, "handle_calibration", handle),
            patch.object(account_calibrator, "structured_log"),
        ):
            replay = asyncio.create_task(
                account_calibrator.reclaim_stale_calibration_messages(
                    object(),
                    {"bilibili": platform_lock},
                )
            )
            for _ in range(100):
                if reconcile.await_count:
                    break
                await asyncio.sleep(0.01)
            self.assertGreater(reconcile.await_count, 0)
            self.assertEqual(handle.await_count, 0)
            platform_lock.release()
            settled = await asyncio.wait_for(replay, timeout=1)

        self.assertEqual(settled, 1)
        handle.assert_awaited_once()

    async def test_reclaimed_queued_delivery_replays_then_acks_terminal(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000001", "bilibili"
        )[1]
        pending = AsyncMock(return_value=[{"message_id": "1-0"}])
        claim = AsyncMock(return_value=[("1-0", data)])
        ack = AsyncMock(return_value=1)
        fake_redis = SimpleNamespace(
            xpending_range=pending,
            xclaim=claim,
            xack=ack,
        )
        reconcile = AsyncMock(side_effect=["queued", "terminal"])
        handle = AsyncMock()
        pool = object()
        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                reconcile,
            ),
            patch.object(account_calibrator, "handle_calibration", handle),
            patch.object(account_calibrator, "structured_log"),
        ):
            settled = await account_calibrator.reclaim_stale_calibration_messages(
                pool
            )

        self.assertEqual(1, settled)
        handle.assert_awaited_once_with(pool, data)
        ack.assert_awaited_once_with(
            account_calibrator.STREAM_KEY,
            account_calibrator.GROUP_NAME,
            "1-0",
        )

    async def test_active_reclaimed_delivery_is_not_acked_or_replayed(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000001", "weibo"
        )[1]
        ack = AsyncMock()
        handle = AsyncMock()
        fake_redis = SimpleNamespace(
            xpending_range=AsyncMock(
                return_value=[{"message_id": "1-0"}]
            ),
            xclaim=AsyncMock(return_value=[("1-0", data)]),
            xack=ack,
        )
        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                new=AsyncMock(return_value="active"),
            ),
            patch.object(account_calibrator, "handle_calibration", handle),
            patch.object(account_calibrator, "structured_log"),
        ):
            settled = await account_calibrator.reclaim_stale_calibration_messages(
                object()
            )

        self.assertEqual(0, settled)
        handle.assert_not_awaited()
        ack.assert_not_awaited()

    async def test_orphaned_queued_row_is_republished_once(self):
        row = {
            "calibration_id": "00000000-0000-0000-0000-000000000001",
            "account_id": 7,
            "platform": "xiaohongshu",
            "check_url": "https://www.xiaohongshu.com/explore",
            "account_platform": "xiaohongshu",
            "account_deleted_at": None,
            "account_execution_revision": 2,
        }
        marker = AsyncMock(return_value=True)
        xadd = AsyncMock(return_value="2-0")
        fake_redis = SimpleNamespace(
            set=marker,
            xadd=xadd,
            delete=AsyncMock(),
        )
        with (
            patch.object(
                account_calibrator.database,
                "fetch_all",
                new=AsyncMock(return_value=[row]),
            ),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                new=AsyncMock(return_value="queued"),
            ),
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(account_calibrator, "structured_log"),
        ):
            count = await account_calibrator.requeue_stale_queued_calibrations()

        self.assertEqual(1, count)
        marker.assert_awaited_once()
        xadd.assert_awaited_once_with(
            "account_calibration_requests:xiaohongshu",
            account_calibrator._calibration_task_from_row(row),
        )

    async def test_running_message_cancel_is_terminally_reconciled_before_ack(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000001", "douyin"
        )[1]
        started = asyncio.Event()
        never_release = asyncio.Event()

        async def handle(_pool, _task):
            started.set()
            await never_release.wait()

        ack = AsyncMock(return_value=1)
        fake_redis = SimpleNamespace(xack=ack)
        with (
            patch.object(account_calibrator, "handle_calibration", side_effect=handle),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                new=AsyncMock(return_value="failed"),
            ) as reconcile,
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(account_calibrator, "structured_log"),
        ):
            task = asyncio.create_task(
                account_calibrator.process_calibration_message(
                    object(), "1-0", data
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        reconcile.assert_awaited_once()
        ack.assert_awaited_once_with(
            account_calibrator.STREAM_KEY,
            account_calibrator.GROUP_NAME,
            "1-0",
        )

    async def test_claim_race_keeps_fresh_running_delivery_pending(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000002", "weibo"
        )[1]
        ack = AsyncMock()
        reconcile = AsyncMock(return_value="active")
        with (
            patch.object(
                account_calibrator,
                "claim_calibration_message",
                new=AsyncMock(return_value=None),
            ) as claim,
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                reconcile,
            ),
            patch.object(
                account_calibrator,
                "redis",
                SimpleNamespace(xack=ack),
            ),
            patch.object(account_calibrator, "structured_log") as log,
        ):
            await account_calibrator.process_calibration_message(
                object(),
                "2-0",
                data,
            )

        claim.assert_awaited_once()
        ack.assert_not_awaited()
        reconcile.assert_awaited_once_with(
            data,
            stale_running_only=True,
            failure_reason=(
                "account calibration handler returned without terminal "
                "settlement"
            ),
        )
        self.assertEqual(
            log.call_args.args[:2],
            ("warning", "account_calibration_delivery_remains_pending"),
        )

    async def test_deleted_account_queued_delivery_is_failed_then_acked(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000003", "xiaohongshu"
        )[1]
        transaction_active = False

        @asynccontextmanager
        async def transaction():
            nonlocal transaction_active
            transaction_active = True
            try:
                yield
            finally:
                transaction_active = False

        async def fetch_one(_query, _values):
            self.assertTrue(transaction_active)
            return {
                "calibration_id": data["calibration_id"],
                "account_id": 7,
                "platform": "xiaohongshu",
                "check_url": data["check_url"],
                "calibration_status": "queued",
                "account_platform": "xiaohongshu",
                "account_deleted_at": "2026-07-23 00:00:00",
                "account_execution_revision": 2,
                "stale_running": 0,
            }

        fake_database = SimpleNamespace(
            transaction=transaction,
            fetch_one=AsyncMock(side_effect=fetch_one),
        )

        async def update_failed(_query, values, *, db):
            self.assertIs(db, fake_database)
            self.assertTrue(transaction_active)
            self.assertEqual(values["status"], "queued")
            self.assertEqual(
                values["error"],
                "account calibration authority became invalid",
            )
            return 1

        ack = AsyncMock(return_value=1)
        with (
            patch.object(
                account_calibrator,
                "claim_calibration_message",
                new=AsyncMock(return_value=None),
            ),
            patch.object(account_calibrator, "database", fake_database),
            patch.object(
                account_calibrator,
                "execute_affected_rows",
                new=AsyncMock(side_effect=update_failed),
            ) as update,
            patch.object(
                account_calibrator,
                "emit_calibration_terminal_observability",
                new=AsyncMock(),
            ) as observability,
            patch.object(
                account_calibrator,
                "redis",
                SimpleNamespace(xack=ack),
            ),
            patch.object(account_calibrator, "structured_log"),
        ):
            await account_calibrator.process_calibration_message(
                object(),
                "3-0",
                data,
            )

        update.assert_awaited_once()
        observability.assert_awaited_once()
        ack.assert_awaited_once_with(
            account_calibrator.STREAM_KEY,
            account_calibrator.GROUP_NAME,
            "3-0",
        )

    async def test_terminal_success_is_reconciled_then_acked(self):
        data = calibration(
            "00000000-0000-0000-0000-000000000004", "douyin"
        )[1]
        ack = AsyncMock(return_value=1)
        reconcile = AsyncMock(return_value="terminal")
        with (
            patch.object(
                account_calibrator,
                "handle_calibration",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                account_calibrator,
                "reconcile_calibration_message_state",
                reconcile,
            ),
            patch.object(
                account_calibrator,
                "redis",
                SimpleNamespace(xack=ack),
            ),
        ):
            await account_calibrator.process_calibration_message(
                object(),
                "4-0",
                data,
            )

        reconcile.assert_awaited_once()
        ack.assert_awaited_once_with(
            account_calibrator.STREAM_KEY,
            account_calibrator.GROUP_NAME,
            "4-0",
        )


if __name__ == "__main__":
    unittest.main()
