import base64
import os
import unittest
from unittest.mock import patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.account_calibration_streams import (  # noqa: E402
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME,
    LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
    account_calibration_stream_bindings,
)
from app.adapter_probe_streams import (  # noqa: E402
    LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
    LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
    adapter_probe_stream_bindings,
)
from app.api import metrics  # noqa: E402
from shared.discovery_scan_streams import (  # noqa: E402
    DISCOVERY_SCAN_STREAM_BINDINGS,
)


class FakeControlRedis:
    def __init__(self):
        self.groups = {}
        self.consumers = {}
        self.fail_consumers_for = set()
        self.group_calls = []
        for binding in (
            *adapter_probe_stream_bindings(include_legacy=True),
            *account_calibration_stream_bindings(include_legacy=True),
            *DISCOVERY_SCAN_STREAM_BINDINGS,
        ):
            group_name = binding.group_name
            consumers = [
                {"name": "worker-live", "idle": 10, "pending": 0}
            ]
            if getattr(binding, "legacy", False):
                if binding.stream_key == "adapter_probe_requests":
                    group_name = LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME
                    consumers = [
                        {
                            "name": (
                                LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME
                            ),
                            "idle": 10,
                            "pending": 0,
                        }
                    ]
                else:
                    group_name = (
                        LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME
                    )
                    consumers = [
                        {
                            "name": (
                                LEGACY_ACCOUNT_CALIBRATION_FANOUT_CONSUMER_NAME
                            ),
                            "idle": 10,
                            "pending": 0,
                        }
                    ]
            elif binding in DISCOVERY_SCAN_STREAM_BINDINGS:
                consumers = [
                    {
                        "name": (
                            f"core-platform-runner:{binding.platform}"
                        ),
                        "idle": 10,
                        "pending": 0,
                    }
                ]
            self.groups[binding.stream_key] = group_name
            self.consumers[binding.stream_key] = consumers

    async def xlen(self, _stream):
        return 0

    async def xpending(self, _stream, _group):
        return {"pending": 0}

    async def xinfo_consumers(self, stream, group):
        self.group_calls.append((stream, group))
        if stream in self.fail_consumers_for:
            raise RuntimeError("consumer metrics unavailable")
        return self.consumers[stream]

    async def xinfo_groups(self, stream):
        return [{"name": self.groups[stream], "lag": 0}]


class FakeControlDatabase:
    def __init__(self, *, rows=(), heartbeat_names=("worker-live",)):
        self.rows = tuple(rows)
        self.heartbeat_names = tuple(heartbeat_names)

    async def fetch_one(self, query, _values=None):
        if "FROM outbox_events" in query:
            return {"cnt": 0}
        raise AssertionError(f"unexpected fetch_one: {query}")

    async def fetch_all(self, query, values=None):
        if "FROM worker_heartbeats" in query:
            return [
                {"worker_id": name}
                for name in self.heartbeat_names
            ]
        if "FROM outbox_events" in query:
            streams = {
                str(value)
                for key, value in (values or {}).items()
                if key.startswith("task_outbox_stream_")
            }
            return [
                row
                for row in self.rows
                if row["stream_key"] in streams
            ]
        raise AssertionError(f"unexpected fetch_all: {query}")


class ControlStreamMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self, redis, database):
        with patch.object(metrics, "redis", redis), patch.object(
            metrics,
            "database",
            database,
        ), patch.object(
            metrics,
            "structured_log",
        ), patch.object(
            metrics,
            "get_platforms",
            return_value={
                "bilibili": {},
                "weibo": {},
                "xiaohongshu": {},
                "douyin": {},
            },
        ):
            return await metrics.collect_control_stream_metrics()

    async def test_live_workers_are_joined_to_exact_heartbeat_identity(self):
        observed = await self.observe(
            FakeControlRedis(),
            FakeControlDatabase(),
        )

        for platform in (
            "bilibili",
            "weibo",
            "xiaohongshu",
            "douyin",
        ):
            self.assertTrue(observed["by_platform"][platform]["ready"])
            self.assertEqual(
                observed["adapter_probe"]["by_platform"][platform][
                    "workers_online"
                ],
                1,
            )
            self.assertEqual(
                observed["account_calibration"]["by_platform"][platform][
                    "workers_online"
                ],
                1,
            )
            self.assertTrue(
                observed["discovery_scan"]["by_platform"][platform][
                    "ready"
                ]
            )
        self.assertTrue(observed["legacy_control_stream_drain_complete"])
        self.assertTrue(
            observed["adapter_probe"]["legacy"]["fanout_consumer_online"]
        )
        self.assertTrue(
            observed["account_calibration"]["legacy"][
                "fanout_consumer_online"
            ]
        )

    async def test_stale_redis_consumer_cannot_make_lane_ready(self):
        observed = await self.observe(
            FakeControlRedis(),
            FakeControlDatabase(heartbeat_names=("different-worker",)),
        )

        probe = observed["adapter_probe"]["by_platform"]["bilibili"]
        self.assertEqual(probe["redis_consumers_online"], 1)
        self.assertEqual(probe["workers_online"], 0)
        self.assertFalse(probe["ready"])
        self.assertIn(
            "adapter_probe_consumer_offline",
            probe["blocker_codes"],
        )

    async def test_one_failed_lane_and_one_stalled_outbox_are_local(self):
        fake_redis = FakeControlRedis()
        fake_redis.fail_consumers_for.add(
            "adapter_probe_requests:bilibili"
        )
        observed = await self.observe(
            fake_redis,
            FakeControlDatabase(
                rows=(
                    {
                        "stream_key": (
                            "account_calibration_requests:weibo"
                        ),
                        "undelivered": 2,
                        "stale_undelivered": 1,
                        "oldest_age_seconds": 180,
                    },
                )
            ),
        )

        self.assertFalse(observed["by_platform"]["bilibili"]["ready"])
        self.assertFalse(observed["by_platform"]["weibo"]["ready"])
        self.assertTrue(observed["by_platform"]["xiaohongshu"]["ready"])
        self.assertTrue(observed["by_platform"]["douyin"]["ready"])
        self.assertIn(
            "adapter_probe_redis_metrics_unavailable",
            observed["adapter_probe"]["by_platform"]["bilibili"][
                "blocker_codes"
            ],
        )
        self.assertIn(
            "account_calibration_outbox_stalled",
            observed["account_calibration"]["by_platform"]["weibo"][
                "blocker_codes"
            ],
        )

    async def test_legacy_metrics_observe_fanout_groups_not_old_groups(self):
        fake_redis = FakeControlRedis()
        await self.observe(fake_redis, FakeControlDatabase())

        self.assertIn(
            (
                "adapter_probe_requests",
                LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
            ),
            fake_redis.group_calls,
        )
        self.assertIn(
            (
                "account_calibration_requests",
                LEGACY_ACCOUNT_CALIBRATION_FANOUT_GROUP_NAME,
            ),
            fake_redis.group_calls,
        )
        self.assertNotIn(
            ("adapter_probe_requests", "adapter-probers"),
            fake_redis.group_calls,
        )
        self.assertNotIn(
            ("account_calibration_requests", "account-calibrators"),
            fake_redis.group_calls,
        )
