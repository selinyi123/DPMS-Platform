import asyncio
import base64
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import metrics  # noqa: E402
from app.task_streams import task_stream_bindings  # noqa: E402


def repair_heartbeat_row(
    worker_id,
    *,
    lane_overrides=None,
    heartbeat_age_seconds=1,
):
    lane_overrides = dict(lane_overrides or {})
    lanes = []
    for binding in task_stream_bindings(include_legacy=False):
        override = dict(
            lane_overrides.get(binding.stream_key)
            or lane_overrides.get(binding.platform)
            or {}
        )
        lane = {
            "stream": binding.stream_key,
            "group": binding.group_name,
            "platform": binding.platform,
            "repair": bool(binding.repair),
            "protocol_version": binding.protocol_version,
            "status": "healthy",
            "consecutive_failures": 0,
            "last_success_operation": "xreadgroup",
            "last_success_age_seconds": 1,
            "last_loop_progress_operation": "xreadgroup",
            "last_loop_progress_age_seconds": 1,
            "inflight_count": 0,
            "inflight_limit": 32,
            "saturated": False,
        }
        lane.update(override)
        lanes.append(lane)
    return {
        "worker_id": worker_id,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "detail": {
            "capabilities": ["repair_execution_intent_v1"],
            "execution_intent_contract_version": 1,
            "task_consumer_name": worker_id,
            "task_lane_health": {
                "contract_version": 2,
                "lanes": lanes,
            },
        },
    }


class FakeTaskStreamRedis:
    def __init__(self):
        bindings = task_stream_bindings(include_legacy=True)
        self.pending = {
            binding.stream_key: index
            for index, binding in enumerate(bindings)
        }
        self.lag = {
            binding.stream_key: index + 10
            for index, binding in enumerate(bindings)
        }
        self.length = {
            binding.stream_key: index + 100
            for index, binding in enumerate(bindings)
        }
        self.consumers = {
            binding.stream_key: [
                {"name": "worker-shared", "idle": 10, "pending": 0}
            ]
            for binding in bindings
        }
        self.consumers["lottery_tasks:weibo"].append(
            {"name": "worker-weibo", "idle": 10, "pending": 0}
        )
        self.consumers["lottery_tasks"].append(
            {"name": "core-legacy-fanout", "idle": 10, "pending": 1}
        )
        self.fail_lag_for = set()
        self.fail_pending_for = set()
        self.fail_length_for = set()
        self.fail_consumers_for = set()

    async def xlen(self, stream):
        if stream in self.fail_length_for:
            raise RuntimeError("length unavailable")
        return self.length[stream]

    async def xpending(self, stream, _group):
        if stream in self.fail_pending_for:
            raise RuntimeError("pending unavailable")
        return {"pending": self.pending[stream]}

    async def xinfo_consumers(self, stream, _group):
        if stream in self.fail_consumers_for:
            raise RuntimeError("consumers unavailable")
        return self.consumers[stream]

    async def xinfo_groups(self, stream):
        if stream in self.fail_lag_for:
            raise RuntimeError("lag unavailable")
        binding = next(
            item
            for item in task_stream_bindings(include_legacy=True)
            if item.stream_key == stream
        )
        return [{"name": binding.group_name, "lag": self.lag[stream]}]


class FakeMetricsDatabase:
    def __init__(
        self,
        *,
        legacy_outbox=0,
        heartbeat_names=("worker-shared",),
        heartbeat_rows=None,
        platform_outbox_rows=(),
        fail_outbox_platform=None,
        hang_outbox_platform=None,
    ):
        self.legacy_outbox = legacy_outbox
        self.heartbeat_names = tuple(heartbeat_names)
        self.heartbeat_rows = (
            tuple(heartbeat_rows)
            if heartbeat_rows is not None
            else tuple(
                repair_heartbeat_row(name)
                for name in self.heartbeat_names
            )
        )
        self.platform_outbox_rows = tuple(platform_outbox_rows)
        self.fail_outbox_platform = fail_outbox_platform
        self.hang_outbox_platform = hang_outbox_platform

    async def fetch_one(self, query, _values=None):
        if "FROM outbox_events" in query:
            return {"cnt": self.legacy_outbox}
        raise AssertionError(f"unexpected fetch_one: {query}")

    async def fetch_all(self, query, values=None):
        if "FROM worker_heartbeats" in query:
            return list(self.heartbeat_rows)
        if "FROM outbox_events" in query:
            scoped_streams = {
                str(value)
                for key, value in (values or {}).items()
                if key.startswith("task_outbox_stream_")
            }
            if (
                self.fail_outbox_platform
                and any(
                    stream.endswith(f":{self.fail_outbox_platform}")
                    for stream in scoped_streams
                )
            ):
                raise RuntimeError("scoped outbox query failed")
            if (
                self.hang_outbox_platform
                and any(
                    stream.endswith(f":{self.hang_outbox_platform}")
                    for stream in scoped_streams
                )
            ):
                await asyncio.Event().wait()
            return [
                row
                for row in self.platform_outbox_rows
                if row["stream_key"] in scoped_streams
            ]
        if "FROM task_runs" in query:
            return []
        raise AssertionError(f"unexpected fetch_all: {query}")


class TaskStreamMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_health_does_not_use_global_worker_or_peer_outbox(
        self,
    ):
        fake = FakeTaskStreamRedis()
        dead_consumer = [
            {
                "name": "worker-dead",
                "idle": 600_000,
                # Preserve the historical Redis aggregate edge case: a PEL
                # owner looks active even after its process heartbeat died.
                "pending": 1,
            }
        ]
        fake.consumers["lottery_tasks:bilibili"] = dead_consumer
        fake.consumers[
            "lottery_repair_tasks:v1:bilibili"
        ] = dead_consumer
        fake_database = FakeMetricsDatabase(
            heartbeat_names=("worker-shared",),
            platform_outbox_rows=(
                {
                    "stream_key": "lottery_tasks:weibo",
                    "undelivered": 2,
                    "stale_undelivered": 1,
                    "oldest_age_seconds": 180,
                },
            ),
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        # The compatibility aggregate still sees both Redis identities.
        self.assertEqual(observed["workers_online"], 3)
        self.assertEqual(observed["worker_heartbeats_online"], 1)
        # Platform readiness never borrows that healthy peer identity.
        self.assertFalse(
            observed["transport_by_platform"]["bilibili"]["ready"]
        )
        self.assertEqual(
            observed["workers_online_by_platform"]["bilibili"],
            0,
        )
        self.assertIn(
            "standard_task_consumer_heartbeat_missing",
            observed["transport_by_platform"]["bilibili"][
                "blocker_codes"
            ],
        )
        # A stalled Weibo Outbox blocks only Weibo.
        self.assertFalse(
            observed["transport_by_platform"]["weibo"]["ready"]
        )
        self.assertIn(
            "standard_task_outbox_stalled",
            observed["transport_by_platform"]["weibo"]["blocker_codes"],
        )
        self.assertTrue(
            observed["transport_by_platform"]["xiaohongshu"]["ready"]
        )
        self.assertTrue(
            observed["transport_by_platform"]["douyin"]["ready"]
        )
        self.assertEqual(observed["outbox_undelivered"], 2)
        self.assertEqual(
            observed["outbox_undelivered_by_platform"]["weibo"],
            2,
        )
        self.assertEqual(
            observed["outbox_undelivered_by_platform"]["bilibili"],
            0,
        )
        self.assertEqual(observed["outbox_stale_by_platform"]["weibo"], 1)

    async def test_stale_extra_group_blocks_only_its_exact_lane(self):
        fake = FakeTaskStreamRedis()
        original_groups = fake.xinfo_groups
        original_consumers = fake.xinfo_consumers

        async def groups(stream):
            observed = list(await original_groups(stream))
            if stream == "lottery_tasks:bilibili":
                observed.append(
                    {
                        "name": "abandoned-audit",
                        "pending": 0,
                        "lag": 5,
                    }
                )
            return observed

        async def consumers(stream, group):
            if (
                stream == "lottery_tasks:bilibili"
                and group == "abandoned-audit"
            ):
                return [{"name": "old-auditor", "idle": 901_000}]
            return await original_consumers(stream, group)

        fake.xinfo_groups = groups
        fake.xinfo_consumers = consumers
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            FakeMetricsDatabase(),
        ):
            observed = await metrics.collect_task_stream_metrics()

        bilibili = observed["transport_by_platform"]["bilibili"]
        self.assertFalse(bilibili["standard_ready"])
        self.assertIn(
            "standard_task_consumer_group_retention_blocked",
            bilibili["blocker_codes"],
        )
        standard_lane = next(
            lane for lane in bilibili["lanes"] if not lane["repair"]
        )
        governance = standard_lane["consumer_group_governance"]
        self.assertTrue(governance["retention_alert"])
        self.assertEqual(
            governance["retention_blocked_groups"],
            ["abandoned-audit"],
        )
        self.assertTrue(
            observed["transport_by_platform"]["weibo"]["ready"]
        )
        self.assertEqual(
            [
                alert["stream"]
                for alert in observed[
                    "consumer_group_retention_alerts"
                ]
            ],
            ["lottery_tasks:bilibili"],
        )

    async def test_extra_group_observations_share_one_hard_call_budget(self):
        self.assertEqual(
            metrics.CONSUMER_GROUP_OBSERVATION_MAX_CALLS,
            64,
        )
        self.assertEqual(
            metrics.CONSUMER_GROUP_OBSERVATION_MAX_CONCURRENCY,
            8,
        )

        class ExtraGroupRedis:
            def __init__(self):
                self.calls = []

            async def xinfo_consumers(self, stream, group):
                self.calls.append((stream, group))
                await asyncio.sleep(0)
                return []

        fake = ExtraGroupRedis()
        budget = metrics._ConsumerGroupObservationBudget(
            max_calls=5,
            max_concurrency=2,
            timeout_seconds=1,
        )
        stream_groups = (
            ("lottery_tasks:bilibili", "workers:bilibili"),
            (
                "adapter_probe_requests:bilibili",
                "adapter-probers:bilibili",
            ),
            (
                "account_calibration_requests:bilibili",
                "account-calibrators:bilibili",
            ),
            (
                "discovery_scan_requests:v1:bilibili",
                "discovery-platform-runners:v1:bilibili",
            ),
        )

        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ):
            observations = await asyncio.gather(
                *(
                    metrics._consumer_group_governance_observation(
                        stream_key=stream,
                        primary_group_name=primary_group,
                        primary_consumers=[],
                        primary_consumers_available=True,
                        groups=[
                            {
                                "name": primary_group,
                                "pending": 0,
                                "lag": 0,
                            },
                            *(
                                {
                                    "name": f"orphan-{index}",
                                    "pending": 0,
                                    "lag": 0,
                                }
                                for index in range(4)
                            ),
                        ],
                        groups_available=True,
                        stream_length=0,
                        consumer_group_budget=budget,
                    )
                    for stream, primary_group in stream_groups
                )
            )

        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(budget.snapshot()["calls_started"], 5)
        self.assertTrue(budget.snapshot()["exhausted"])
        self.assertTrue(
            any(
                not observation["available"]
                and observation["retention_alert"]
                and (
                    "consumer_group_observation_budget_exhausted"
                    in observation["warning_codes"]
                )
                for observation in observations
            )
        )

    async def test_top_level_metrics_collection_shares_one_budget(self):
        observed_budgets = []

        async def collect_task(*, consumer_group_budget):
            observed_budgets.append(consumer_group_budget)
            return {"kind": "task"}

        async def collect_control(*, consumer_group_budget):
            observed_budgets.append(consumer_group_budget)
            return {"kind": "control"}

        with patch.object(
            metrics,
            "collect_task_stream_metrics",
            side_effect=collect_task,
        ), patch.object(
            metrics,
            "collect_control_stream_metrics",
            side_effect=collect_control,
        ):
            task, control, budget = (
                await metrics._collect_transport_metrics_for_request()
            )

        self.assertEqual(task, {"kind": "task"})
        self.assertEqual(control, {"kind": "control"})
        self.assertEqual(len(observed_budgets), 2)
        self.assertIs(observed_budgets[0], budget)
        self.assertIs(observed_budgets[1], budget)
        self.assertEqual(budget.snapshot()["max_calls"], 64)
        self.assertEqual(budget.snapshot()["max_concurrency"], 8)

    async def test_extra_group_slot_wait_does_not_serialize_timeouts(self):
        class HangingRedis:
            def __init__(self):
                self.calls = 0
                self.inflight = 0
                self.max_inflight = 0

            async def xinfo_consumers(self, _stream, _group):
                self.calls += 1
                self.inflight += 1
                self.max_inflight = max(
                    self.max_inflight,
                    self.inflight,
                )
                try:
                    await asyncio.Event().wait()
                finally:
                    self.inflight -= 1

        fake = HangingRedis()
        budget = metrics._ConsumerGroupObservationBudget(
            max_calls=10,
            max_concurrency=1,
            timeout_seconds=0.02,
        )
        started_at = asyncio.get_running_loop().time()
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ):
            results = await asyncio.gather(
                *(
                    budget.observe(
                        stream_key="lottery_tasks:bilibili",
                        group_name=f"orphan-{index}",
                    )
                    for index in range(10)
                )
            )
        elapsed = asyncio.get_running_loop().time() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertLessEqual(fake.calls, 10)
        self.assertEqual(fake.max_inflight, 1)
        self.assertTrue(all(not result["available"] for result in results))

    async def test_reported_oversized_consumer_inventory_skips_xinfo(self):
        binding = next(
            item
            for item in task_stream_bindings(include_legacy=False)
            if item.stream_key == "lottery_tasks:bilibili"
        )

        class OversizedInventoryRedis:
            def __init__(self):
                self.consumer_calls = 0

            async def xlen(self, _stream):
                return 0

            async def xpending(self, _stream, _group):
                return {"pending": 0}

            async def xinfo_groups(self, _stream):
                return [
                    {
                        "name": binding.group_name,
                        "pending": 0,
                        "lag": 0,
                        "consumers": 257,
                    }
                ]

            async def xinfo_consumers(self, _stream, _group):
                self.consumer_calls += 1
                raise AssertionError("oversized inventory must be skipped")

        fake = OversizedInventoryRedis()
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ):
            observation, _active_names = (
                await metrics._task_stream_observation(binding)
            )

        self.assertEqual(fake.consumer_calls, 0)
        self.assertFalse(observation["consumers_available"])
        governance = observation["consumer_group_governance"]
        self.assertFalse(governance["available"])
        self.assertTrue(governance["consumer_inventory_alert"])
        self.assertIn(
            "consumer_group_consumer_inventory_too_large",
            governance["warning_codes"],
        )

    async def test_one_lane_metrics_failure_blocks_only_its_platform(self):
        fake = FakeTaskStreamRedis()
        fake.fail_consumers_for.add(
            "lottery_repair_tasks:v1:douyin"
        )
        fake_database = FakeMetricsDatabase()
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertFalse(
            observed["transport_by_platform"]["douyin"]["ready"]
        )
        self.assertIn(
            "repair_task_consumer_metrics_unavailable",
            observed["transport_by_platform"]["douyin"]["blocker_codes"],
        )
        for platform in ("bilibili", "weibo", "xiaohongshu"):
            self.assertTrue(
                observed["transport_by_platform"][platform]["ready"]
            )

    async def test_pending_consumer_failed_repair_lane_is_not_ready(self):
        fake = FakeTaskStreamRedis()
        failed_stream = "lottery_repair_tasks:v1:bilibili"
        fake.consumers[failed_stream] = [
            {
                "name": "worker-shared",
                "idle": 600_000,
                "pending": 1,
            }
        ]
        fake_database = FakeMetricsDatabase(
            heartbeat_rows=(
                repair_heartbeat_row(
                    "worker-shared",
                    lane_overrides={
                        failed_stream: {
                            "status": "failed",
                            "consecutive_failures": 3,
                        }
                    },
                ),
            )
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        bilibili = observed["transport_by_platform"]["bilibili"]
        self.assertTrue(
            next(
                lane
                for lane in bilibili["lanes"]
                if lane["repair"]
            )["consumers_online"]
        )
        self.assertFalse(bilibili["repair_ready"])
        self.assertIn(
            "repair_task_lane_health_unready",
            bilibili["blocker_codes"],
        )
        for platform in ("weibo", "xiaohongshu", "douyin"):
            self.assertTrue(
                observed["transport_by_platform"][platform][
                    "repair_ready"
                ]
            )

    async def test_stale_pel_cannot_hide_failed_standard_lane(self):
        fake = FakeTaskStreamRedis()
        failed_stream = "lottery_tasks:bilibili"
        fake.consumers[failed_stream] = [
            {
                "name": "worker-shared",
                "idle": 600_000,
                "pending": 1,
            }
        ]
        fake_database = FakeMetricsDatabase(
            heartbeat_rows=(
                repair_heartbeat_row(
                    "worker-shared",
                    lane_overrides={
                        failed_stream: {
                            "status": "degraded",
                            "consecutive_failures": 2,
                        }
                    },
                ),
            )
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        bilibili = observed["transport_by_platform"]["bilibili"]
        self.assertFalse(bilibili["standard_ready"])
        self.assertIn(
            "standard_task_lane_health_unready",
            bilibili["blocker_codes"],
        )
        for platform in ("weibo", "xiaohongshu", "douyin"):
            self.assertTrue(
                observed["transport_by_platform"][platform][
                    "standard_ready"
                ]
            )

    async def test_saturated_repair_lane_uses_fresh_capacity_progress(self):
        fake = FakeTaskStreamRedis()
        repair_stream = "lottery_repair_tasks:v1:bilibili"
        fake.consumers[repair_stream] = [
            {
                "name": "worker-shared",
                "idle": 600_000,
                "pending": 0,
            }
        ]
        fake_database = FakeMetricsDatabase(
            heartbeat_rows=(
                repair_heartbeat_row(
                    "worker-shared",
                    lane_overrides={
                        repair_stream: {
                            "last_success_age_seconds": 120,
                            "last_loop_progress_operation": (
                                "capacity_wait"
                            ),
                            "last_loop_progress_age_seconds": 1,
                            "inflight_count": 32,
                            "inflight_limit": 32,
                            "saturated": True,
                        }
                    },
                ),
            )
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        bilibili = observed["transport_by_platform"]["bilibili"]
        self.assertTrue(bilibili["repair_ready"])
        repair_lane = next(
            lane for lane in bilibili["lanes"] if lane["repair"]
        )
        self.assertTrue(repair_lane["task_lane_health_ready"])

    async def test_platform_outbox_query_defaults_empty_lanes_to_zero(self):
        binding = next(
            item
            for item in task_stream_bindings(include_legacy=False)
            if item.stream_key == "lottery_tasks:weibo"
        )
        fake_database = FakeMetricsDatabase(
            platform_outbox_rows=(
                {
                    "stream_key": binding.stream_key,
                    "undelivered": 3,
                    "stale_undelivered": 2,
                    "oldest_age_seconds": 300,
                },
            )
        )
        with patch.object(metrics, "database", fake_database):
            observed = await metrics._platform_outbox_observation(
                task_stream_bindings(include_legacy=True)
            )

        self.assertTrue(observed["available"])
        self.assertEqual(
            observed["by_stream"][binding.stream_key]["undelivered"],
            3,
        )
        self.assertEqual(
            observed["by_stream"]["lottery_tasks:bilibili"][
                "undelivered"
            ],
            0,
        )

    async def test_one_platform_outbox_query_failure_preserves_peers(self):
        fake = FakeTaskStreamRedis()
        fake_database = FakeMetricsDatabase(
            fail_outbox_platform="weibo",
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertFalse(observed["outbox_undelivered_available"])
        self.assertIsNone(
            observed["outbox_undelivered_by_platform"]["weibo"]
        )
        self.assertFalse(
            observed["transport_by_platform"]["weibo"]["ready"]
        )
        self.assertIn(
            "standard_task_outbox_metrics_unavailable",
            observed["transport_by_platform"]["weibo"]["blocker_codes"],
        )
        for platform in ("bilibili", "xiaohongshu", "douyin"):
            self.assertEqual(
                observed["outbox_undelivered_by_platform"][platform],
                0,
            )
            self.assertTrue(
                observed["transport_by_platform"][platform]["ready"]
            )

    async def test_one_platform_outbox_timeout_preserves_peers(self):
        fake = FakeTaskStreamRedis()
        fake_database = FakeMetricsDatabase(
            hang_outbox_platform="weibo",
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ), patch.object(
            metrics,
            "TASK_METRICS_OPERATION_TIMEOUT_SECONDS",
            0.01,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertFalse(
            observed["transport_by_platform"]["weibo"]["ready"]
        )
        for platform in ("bilibili", "xiaohongshu", "douyin"):
            self.assertTrue(
                observed["transport_by_platform"][platform]["ready"]
            )

    async def test_heartbeat_timeout_returns_unavailable(self):
        class HangingDatabase:
            async def fetch_all(self, _query, _values=None):
                await asyncio.Event().wait()

        with patch.object(
            metrics,
            "database",
            HangingDatabase(),
        ), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "TASK_METRICS_OPERATION_TIMEOUT_SECONDS",
            0.01,
        ):
            observed = await metrics._worker_heartbeat_observation()

        self.assertFalse(observed["available"])
        self.assertEqual(observed["names"], set())

    async def test_metrics_cover_all_lanes_and_deduplicate_worker_names(self):
        fake = FakeTaskStreamRedis()
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 2}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertEqual(len(observed["task_streams"]), 9)
        self.assertEqual(observed["workers_online"], 2)
        self.assertEqual(
            observed["pending"],
            sum(fake.pending.values()),
        )
        self.assertEqual(
            observed["pending_by_platform"],
            {
                "bilibili": 4,
                "weibo": 6,
                "xiaohongshu": 8,
                "douyin": 10,
            },
        )
        self.assertEqual(
            observed["lag_by_platform"],
            {
                "bilibili": 24,
                "weibo": 26,
                "xiaohongshu": 28,
                "douyin": 30,
            },
        )
        self.assertEqual(observed["length"], sum(fake.length.values()))
        self.assertTrue(observed["length_available"])
        self.assertEqual(
            observed["length_by_platform"],
            {
                "bilibili": 204,
                "weibo": 206,
                "xiaohongshu": 208,
                "douyin": 210,
            },
        )
        self.assertTrue(
            all(
                item["length"] == fake.length[item["stream"]]
                for item in observed["task_streams"]
            )
        )
        repair_lanes = [
            item for item in observed["task_streams"] if item["repair"]
        ]
        self.assertEqual(len(repair_lanes), 4)
        self.assertTrue(
            all(item["protocol_version"] == 1 for item in repair_lanes)
        )
        self.assertEqual(observed["legacy_pending"], 8)
        self.assertEqual(observed["legacy_lag"], 18)
        self.assertEqual(observed["legacy_outbox_undelivered"], 2)
        self.assertFalse(observed["legacy_drain_complete"])

    async def test_legacy_drain_needs_observed_zero_pending_and_lag(self):
        fake = FakeTaskStreamRedis()
        fake.pending["lottery_tasks"] = 0
        fake.lag["lottery_tasks"] = 0
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertTrue(observed["legacy_drain_complete"])

    async def test_missing_lag_never_reports_legacy_drain_complete(self):
        fake = FakeTaskStreamRedis()
        fake.pending["lottery_tasks"] = 0
        fake.fail_lag_for.add("lottery_tasks")
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["legacy_lag"])
        self.assertFalse(observed["legacy_drain_complete"])

    async def test_missing_one_repair_lane_lag_fails_platform_aggregate_closed(
        self,
    ):
        fake = FakeTaskStreamRedis()
        fake.fail_lag_for.add("lottery_repair_tasks:v1:weibo")
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["lag_by_platform"]["weibo"])
        self.assertEqual(observed["lag_by_platform"]["bilibili"], 24)

    async def test_missing_one_lane_pending_is_not_reported_as_zero(self):
        fake = FakeTaskStreamRedis()
        failed_stream = "lottery_repair_tasks:v1:weibo"
        fake.fail_pending_for.add(failed_stream)
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["pending"])
        self.assertFalse(observed["pending_available"])
        self.assertIsNone(observed["pending_by_platform"]["weibo"])
        self.assertEqual(observed["pending_by_platform"]["bilibili"], 4)
        failed_lane = next(
            item
            for item in observed["task_streams"]
            if item["stream"] == failed_stream
        )
        self.assertIsNone(failed_lane["pending"])
        self.assertFalse(failed_lane["pending_available"])

    async def test_missing_one_lane_length_fails_aggregate_closed(self):
        fake = FakeTaskStreamRedis()
        failed_stream = "lottery_repair_tasks:v1:weibo"
        fake.fail_length_for.add(failed_stream)
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["length"])
        self.assertFalse(observed["length_available"])
        self.assertIsNone(observed["length_by_platform"]["weibo"])
        self.assertEqual(observed["length_by_platform"]["bilibili"], 204)
        failed_lane = next(
            item
            for item in observed["task_streams"]
            if item["stream"] == failed_stream
        )
        self.assertIsNone(failed_lane["length"])
        self.assertFalse(failed_lane["length_available"])

    async def test_missing_legacy_pending_never_reports_drain_complete(self):
        fake = FakeTaskStreamRedis()
        fake.fail_pending_for.add("lottery_tasks")
        fake.lag["lottery_tasks"] = 0
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 0}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["legacy_pending"])
        self.assertFalse(observed["legacy_drain_complete"])

    async def test_undelivered_legacy_outbox_keeps_drain_incomplete(self):
        fake = FakeTaskStreamRedis()
        fake.pending["lottery_tasks"] = 0
        fake.lag["lottery_tasks"] = 0
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"cnt": 1}
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertEqual(observed["legacy_outbox_undelivered"], 1)
        self.assertFalse(observed["legacy_drain_complete"])

    async def test_unavailable_legacy_outbox_count_fails_closed(self):
        fake = FakeTaskStreamRedis()
        fake.pending["lottery_tasks"] = 0
        fake.lag["lottery_tasks"] = 0
        fake_database = AsyncMock()
        fake_database.fetch_one.side_effect = RuntimeError(
            "database unavailable"
        )
        with patch.object(metrics, "redis", fake), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "database",
            fake_database,
        ):
            observed = await metrics.collect_task_stream_metrics()

        self.assertIsNone(observed["legacy_outbox_undelivered"])
        self.assertFalse(observed["legacy_drain_complete"])


if __name__ == "__main__":
    unittest.main()
