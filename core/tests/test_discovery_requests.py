import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.services import discovery_requests
from shared.discovery_scan_streams import (
    DISCOVERY_SCAN_STREAM_BINDINGS,
    discovery_scan_binding_for_platform,
)
from shared.redis_consumer_groups import expected_consumer_group_names


def discovery_result(platform: str) -> dict:
    return {
        "sources": 1,
        "scanned": 1,
        "found": 1,
        "inserted": 1,
        "expanded_sources": 0,
        "expired": 0,
        "failed": 0,
        "by_platform": {
            platform: {
                "scanned": 1,
                "found": 1,
                "inserted": 1,
                "expanded_sources": 0,
                "failed": 0,
            }
        },
    }


class DispatchRedis:
    def __init__(self, *, failing_platform: str | None = None):
        self.failing_platform = failing_platform
        self.xadd_calls = []

    async def xadd(self, stream, fields, **kwargs):
        self.xadd_calls.append((stream, dict(fields), dict(kwargs)))
        if str(fields["platform"]) == self.failing_platform:
            raise RuntimeError("WRONGTYPE")
        return "1-0"


class RequestRedis:
    def __init__(self):
        self.retire_calls = []
        self.sets = []

    async def get(self, _key):
        return None

    async def set(self, *args, **kwargs):
        self.sets.append((args, kwargs))
        return True

    async def eval(self, *args):
        self.retire_calls.append(args)
        return [1, 1]


class DiscoveryRequestDispatchTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self):
        discovery_requests._manual_discovery_scan_task = None

    def tearDown(self):
        task = discovery_requests._manual_discovery_scan_task
        if task is not None and not task.done():
            task.cancel()
        discovery_requests._manual_discovery_scan_task = None

    async def test_one_xadd_failure_is_lane_local_and_never_trims(self):
        fake_redis = DispatchRedis(failing_platform="weibo")

        async def wait_result(_request_id, platform):
            return discovery_result(platform)

        with patch.object(
            discovery_requests,
            "redis",
            fake_redis,
        ), patch.object(
            discovery_requests,
            "_wait_for_discovery_result",
            side_effect=wait_result,
        ) as wait_mock, patch.object(
            discovery_requests,
            "structured_log",
        ):
            observed = (
                await discovery_requests._dispatch_manual_discovery_scan_once()
            )

        self.assertEqual(len(fake_redis.xadd_calls), 4)
        self.assertTrue(
            all(not kwargs for _stream, _fields, kwargs in fake_redis.xadd_calls)
        )
        self.assertEqual(wait_mock.await_count, 3)
        self.assertEqual(observed["sources"], 3)
        self.assertEqual(observed["inserted"], 3)
        self.assertEqual(observed["failed"], 1)
        self.assertEqual(
            observed["by_platform"]["weibo"]["dispatch_error"],
            "discovery_scan_dispatch_failed",
        )
        for platform in ("bilibili", "xiaohongshu", "douyin"):
            self.assertEqual(
                observed["by_platform"][platform]["inserted"],
                1,
            )

    async def test_concurrent_callers_share_shielded_fanout(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        expected = {"failed": 0, "by_platform": {}}

        async def one_scan():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return expected

        with patch.object(
            discovery_requests,
            "_dispatch_manual_discovery_scan_once",
            side_effect=one_scan,
        ):
            first = asyncio.create_task(
                discovery_requests.dispatch_manual_discovery_scan()
            )
            await started.wait()
            second = asyncio.create_task(
                discovery_requests.dispatch_manual_discovery_scan()
            )
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertIs(await second, expected)

        self.assertEqual(calls, 1)


class DiscoveryRequestConsumerTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_consumer_names_are_instance_unique(self):
        first = discovery_requests._new_discovery_scan_consumer_name(
            "weibo"
        )
        second = discovery_requests._new_discovery_scan_consumer_name(
            "weibo"
        )

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("core-platform-runner:weibo:"))
        self.assertEqual(len(first.rsplit(":", 1)[-1]), 32)

    def test_configured_consumer_name_is_stable_across_restarts(self):
        first = discovery_requests._new_discovery_scan_consumer_name(
            "weibo",
            configured_instance_id="dpms-core-weibo-runner",
        )
        second = discovery_requests._new_discovery_scan_consumer_name(
            "weibo",
            configured_instance_id="dpms-core-weibo-runner",
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            "core-platform-runner:weibo:dpms-core-weibo-runner",
        )

    def test_configured_consumer_name_rejects_ambiguous_values(self):
        with self.assertRaisesRegex(
            ValueError,
            "core_runner_instance_id_invalid",
        ):
            discovery_requests._new_discovery_scan_consumer_name(
                "weibo",
                configured_instance_id="runner with spaces",
            )

    async def test_stale_request_is_retired_without_scanning(self):
        binding = discovery_scan_binding_for_platform("bilibili")
        fake_redis = RequestRedis()
        stale_requested_at = int(time.time() * 1000) - (
            discovery_requests.DISCOVERY_SCAN_REQUEST_MAX_AGE_MILLISECONDS
            + 1
        )
        fields = {
            "protocol_version": str(
                discovery_requests.DISCOVERY_SCAN_PROTOCOL_VERSION
            ),
            "request_id": "52fb0bd8-2d91-4df0-9ae4-061540d78944",
            "platform": binding.platform,
            "requested_at_ms": str(stale_requested_at),
        }

        with patch.object(
            discovery_requests,
            "redis",
            fake_redis,
        ), patch.object(
            discovery_requests,
            "run_discovery",
            new=AsyncMock(),
        ) as run_mock, patch.object(
            discovery_requests,
            "structured_log",
        ):
            await discovery_requests._handle_discovery_scan_request(
                binding,
                "8-0",
                fields,
            )

        run_mock.assert_not_awaited()
        self.assertEqual(
            fake_redis.retire_calls[0][1:],
            (1, binding.stream_key, binding.group_name, "8-0"),
        )
        self.assertEqual(fake_redis.sets, [])

    async def test_retirement_accepts_delete_after_prior_ack(self):
        binding = discovery_scan_binding_for_platform("weibo")
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=[0, 1])

        with patch.object(discovery_requests, "redis", fake_redis):
            await discovery_requests._retire_discovery_scan_request(
                binding,
                "8-1",
            )

        fake_redis.eval.assert_awaited_once()
        self.assertIn("XINFO", fake_redis.eval.await_args.args[0])
        self.assertIn("XPENDING", fake_redis.eval.await_args.args[0])
        self.assertIn("XDEL", fake_redis.eval.await_args.args[0])
        fake_redis.xack.assert_not_awaited()
        fake_redis.xdel.assert_not_awaited()

    async def test_stale_consumer_cleanup_is_exactly_platform_scoped(self):
        binding = discovery_scan_binding_for_platform("xiaohongshu")
        consumer = (
            "core-platform-runner:xiaohongshu:"
            "0f6fd31be4634ab1a330c18a7f374f20"
        )
        with patch.object(
            discovery_requests,
            "retire_stale_consumer_metadata",
            new=AsyncMock(return_value={"retired": 1}),
        ) as retire:
            result = await (
                discovery_requests
                ._retire_stale_discovery_scan_consumers(
                    binding,
                    consumer,
                )
            )

        self.assertEqual(result, {"retired": 1})
        retire.assert_awaited_once_with(
            discovery_requests.redis,
            stream_key=binding.stream_key,
            group_name=binding.group_name,
            current_consumer_name=consumer,
            managed_consumer_prefix=(
                "core-platform-runner:xiaohongshu:"
            ),
            minimum_idle_milliseconds=(
                discovery_requests
                .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS
            ),
        )

    async def test_restart_drains_own_pending_before_new_messages(self):
        binding = discovery_scan_binding_for_platform("douyin")
        request_id = "b0bcb8e9-b148-48cb-987a-989ae1b8372c"
        fields = {
            "protocol_version": str(
                discovery_requests.DISCOVERY_SCAN_PROTOCOL_VERSION
            ),
            "request_id": request_id,
            "platform": binding.platform,
            "requested_at_ms": str(int(time.time() * 1000)),
        }

        class StopLoop(RuntimeError):
            pass

        class PendingRedis(RequestRedis):
            def __init__(self):
                super().__init__()
                self.read_ids = []
                self.consumers = []
                self.read_count = 0
                self.pending_calls = 0

            async def xinfo_groups(self, _stream):
                return [{"name": binding.group_name}]

            async def xinfo_consumers(self, *_args):
                return []

            async def xpending_range(self, *_args, **_kwargs):
                self.pending_calls += 1
                return []

            async def xreadgroup(
                self,
                _group,
                consumer,
                streams,
                **_kwargs,
            ):
                read_id = streams[binding.stream_key]
                self.read_ids.append(read_id)
                self.consumers.append(consumer)
                self.read_count += 1
                if self.read_count == 1:
                    return [
                        (
                            binding.stream_key,
                            [("9-0", dict(fields))],
                        )
                    ]
                if self.read_count == 2:
                    # redis-py can return a truthy stream envelope with no
                    # pending entries. The loop must still advance to ">".
                    return [(binding.stream_key, [])]
                raise StopLoop("observed new-message phase")

        fake_redis = PendingRedis()
        with patch.object(
            discovery_requests,
            "redis",
            fake_redis,
        ), patch.object(
            discovery_requests,
            "run_discovery",
            new=AsyncMock(return_value=discovery_result("douyin")),
        ) as run_mock:
            with self.assertRaises(StopLoop):
                await discovery_requests.discovery_scan_request_loop(
                    "douyin"
                )

        run_mock.assert_awaited_once_with(platforms=("douyin",))
        self.assertEqual(fake_redis.read_ids, ["0", "0", ">"])
        self.assertEqual(fake_redis.pending_calls, 1)
        self.assertEqual(len(set(fake_redis.consumers)), 1)
        self.assertTrue(
            fake_redis.consumers[0].startswith(
                "core-platform-runner:douyin:"
            )
        )
        self.assertEqual(
            fake_redis.retire_calls[0][1:],
            (1, binding.stream_key, binding.group_name, "9-0"),
        )

    async def test_periodic_reclaim_claims_only_guaranteed_stale_work(self):
        binding = discovery_scan_binding_for_platform("xiaohongshu")
        message_id = "13-0"
        fields = {
            "protocol_version": str(
                discovery_requests.DISCOVERY_SCAN_PROTOCOL_VERSION
            ),
            "request_id": "23928d24-3c76-45f2-8ac7-50e945ad40d7",
            "platform": binding.platform,
            "requested_at_ms": str(
                int(time.time() * 1000)
                - discovery_requests
                .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS
                - 1
            ),
        }

        class ReclaimRedis(RequestRedis):
            def __init__(self):
                super().__init__()
                self.claims = []

            async def xpending_range(self, *args, **kwargs):
                self.pending_args = (args, kwargs)
                return [
                    {
                        "message_id": message_id,
                        "time_since_delivered": (
                            discovery_requests
                            .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS
                        ),
                    }
                ]

            async def xclaim(self, *args, **kwargs):
                self.claims.append((args, kwargs))
                return [(message_id, dict(fields))]

        fake_redis = ReclaimRedis()
        consumer = (
            discovery_requests._new_discovery_scan_consumer_name(
                binding.platform
            )
        )
        with (
            patch.object(discovery_requests, "redis", fake_redis),
            patch.object(
                discovery_requests,
                "run_discovery",
                new=AsyncMock(),
            ) as run_mock,
        ):
            reclaimed = await (
                discovery_requests
                ._reclaim_stale_discovery_scan_requests(
                    binding,
                    consumer,
                )
            )

        self.assertEqual(reclaimed, 1)
        run_mock.assert_not_awaited()
        self.assertEqual(
            fake_redis.pending_args[1]["idle"],
            discovery_requests
            .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS,
        )
        self.assertEqual(fake_redis.claims[0][0][2], consumer)
        self.assertEqual(
            fake_redis.claims[0][1]["min_idle_time"],
            discovery_requests
            .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS,
        )
        self.assertEqual(
            fake_redis.retire_calls[0][1:],
            (1, binding.stream_key, binding.group_name, message_id),
        )
        self.assertGreaterEqual(
            discovery_requests
            .DISCOVERY_SCAN_RECLAIM_IDLE_MILLISECONDS,
            discovery_requests
            .DISCOVERY_SCAN_REQUEST_MAX_AGE_MILLISECONDS
            + discovery_requests
            .DISCOVERY_SCAN_REQUEST_MAX_FUTURE_SKEW_MILLISECONDS,
        )

    def test_each_discovery_lane_is_in_consumer_group_topology(self):
        for binding in DISCOVERY_SCAN_STREAM_BINDINGS:
            self.assertEqual(
                expected_consumer_group_names(binding.stream_key),
                frozenset({binding.group_name}),
            )


if __name__ == "__main__":
    unittest.main()
