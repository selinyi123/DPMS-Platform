import unittest
from pathlib import Path

from shared.redis_consumer_groups import (
    GOVERNED_CONSUMER_GROUP_STREAM_KEYS,
    MAX_OBSERVED_CONSUMER_GROUPS,
    MAX_OBSERVED_CONSUMERS_PER_GROUP,
    REDIS_CONSUMER_GROUP_SPECS,
    RedisConsumerGroupTopologyError,
    RedisConsumerRetentionError,
    evaluate_consumer_group_governance,
    expected_consumer_group_names,
    retire_stale_consumer_metadata,
    runtime_consumer_group_specs,
    verify_redis_consumer_groups,
)


class RedisConsumerGroupGovernanceTests(unittest.TestCase):
    def test_governed_stream_catalog_outlives_current_group_specs(self):
        current_streams = {
            spec.stream_key for spec in REDIS_CONSUMER_GROUP_SPECS
        }
        self.assertTrue(
            current_streams.issubset(
                GOVERNED_CONSUMER_GROUP_STREAM_KEYS
            )
        )

    def test_topology_includes_primary_and_legacy_fanout_groups(self):
        self.assertEqual(
            expected_consumer_group_names("lottery_tasks:bilibili"),
            frozenset({"workers:bilibili"}),
        )
        self.assertEqual(
            expected_consumer_group_names("adapter_probe_requests"),
            frozenset(
                {
                    "adapter-probers",
                    "adapter-probers:legacy-fanout",
                }
            ),
        )
        self.assertEqual(
            expected_consumer_group_names("account_calibration_requests"),
            frozenset(
                {
                    "account-calibrators",
                    "account-calibrators:legacy-fanout",
                }
            ),
        )
        self.assertEqual(
            expected_consumer_group_names(
                "discovery_scan_requests:v1:bilibili"
            ),
            frozenset(
                {"discovery-platform-runners:v1:bilibili"}
            ),
        )
        self.assertEqual(
            expected_consumer_group_names("notify_events"),
            frozenset({"notify-dispatchers"}),
        )
        self.assertEqual(
            expected_consumer_group_names("login_requests"),
            frozenset({"login-workers"}),
        )

    def test_bootstrap_manifest_exactly_matches_shared_topology(self):
        manifest = (
            Path(__file__).resolve().parents[2]
            / "docker"
            / "redis"
            / "consumer-groups.tsv"
        )
        manifest_pairs = {
            tuple(line.split("\t"))
            for raw_line in manifest.read_text(encoding="utf-8").splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        }
        self.assertTrue(
            all(len(pair) == 2 for pair in manifest_pairs)
        )
        self.assertEqual(
            manifest_pairs,
            {
                (spec.stream_key, spec.group_name)
                for spec in REDIS_CONSUMER_GROUP_SPECS
            },
        )

    def test_runtime_scope_selects_only_role_owned_groups(self):
        core = runtime_consumer_group_specs(
            "core",
            platforms=("weibo",),
            include_shared=False,
        )
        worker = runtime_consumer_group_specs(
            "worker",
            platforms=(),
            include_shared=True,
        )
        self.assertEqual(
            {spec.subsystem for spec in core},
            {"lottery_task", "discovery_scan"},
        )
        self.assertTrue(all(spec.platform == "weibo" for spec in core))
        self.assertEqual(
            {spec.subsystem for spec in worker},
            {"adapter_probe", "account_calibration", "login"},
        )
        self.assertTrue(all(spec.platform is None for spec in worker))

    def test_stale_unexpected_group_raises_retention_alert(self):
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:bilibili",
            groups=[
                {
                    "name": "workers:bilibili",
                    "pending": 0,
                    "lag": 0,
                },
                {
                    "name": "abandoned-audit",
                    "pending": 0,
                    "lag": 7,
                },
            ],
            consumers_by_group={
                "workers:bilibili": [{"name": "worker-1", "idle": 10}],
                "abandoned-audit": [
                    {"name": "old-auditor", "idle": 901_000}
                ],
            },
            stale_after_milliseconds=900_000,
            stream_length=7,
        )

        self.assertTrue(result["available"])
        self.assertTrue(result["retention_alert"])
        self.assertEqual(result["unexpected_groups"], ["abandoned-audit"])
        self.assertEqual(result["stale_groups"], ["abandoned-audit"])
        self.assertEqual(
            result["retention_blocked_groups"],
            ["abandoned-audit"],
        )
        self.assertIn(
            "consumer_group_retention_blocked",
            result["warning_codes"],
        )
        self.assertEqual(result["stale_consumer_entries"], 1)
        self.assertTrue(result["consumer_inventory_alert"])
        self.assertEqual(
            result["groups"][1]["stale_consumers"],
            1,
        )

    def test_active_expected_group_backlog_is_observable_not_stale(self):
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:weibo",
            groups=[
                {
                    "name": "workers:weibo",
                    "pending": 1,
                    "lag": 3,
                }
            ],
            consumers_by_group={
                "workers:weibo": [{"name": "worker-1", "idle": 50}]
            },
            stale_after_milliseconds=900_000,
            stream_length=4,
        )

        self.assertFalse(result["retention_alert"])
        self.assertEqual(result["stale_groups"], [])
        self.assertEqual(result["xdel_blocked_groups"], ["workers:weibo"])
        self.assertEqual(result["retention_blocked_groups"], [])

    def test_clean_unexpected_group_still_raises_governance_alert(self):
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:weibo",
            groups=[
                {
                    "name": "workers:weibo",
                    "pending": 0,
                    "lag": 0,
                },
                {
                    "name": "orphan-clean",
                    "pending": 0,
                    "lag": 0,
                },
            ],
            consumers_by_group={
                "workers:weibo": [{"name": "worker-1", "idle": 10}],
                "orphan-clean": [],
            },
            stale_after_milliseconds=900_000,
            stream_length=0,
        )

        self.assertTrue(result["retention_alert"])
        self.assertEqual(result["unexpected_groups"], ["orphan-clean"])
        self.assertEqual(result["stale_groups"], ["orphan-clean"])
        self.assertEqual(result["retention_blocked_groups"], [])

    def test_oversized_consumer_inventory_is_alerted_without_scanning(self):
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:douyin",
            groups=[
                {
                    "name": "workers:douyin",
                    "pending": 0,
                    "lag": 0,
                }
            ],
            consumers_by_group={
                "workers:douyin": [
                    {"name": f"old-{index}", "idle": 1_000_000}
                    for index in range(
                        MAX_OBSERVED_CONSUMERS_PER_GROUP + 1
                    )
                ]
            },
            stale_after_milliseconds=900_000,
            stream_length=0,
        )

        self.assertFalse(result["available"])
        self.assertTrue(result["consumer_inventory_alert"])
        self.assertIn(
            "consumer_group_consumer_inventory_too_large",
            result["warning_codes"],
        )

    def test_missing_consumer_observation_fails_retention_health_closed(self):
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:douyin",
            groups=[
                {
                    "name": "workers:douyin",
                    "pending": 1,
                    "lag": 0,
                }
            ],
            consumers_by_group={},
            stale_after_milliseconds=900_000,
            stream_length=1,
        )

        self.assertFalse(result["available"])
        self.assertTrue(result["retention_alert"])
        self.assertEqual(
            result["retention_blocked_groups"],
            ["workers:douyin"],
        )

    def test_oversized_group_inventory_is_bounded_and_alerted(self):
        groups = [
            {
                "name": f"unexpected-{index}",
                "pending": 0,
                "lag": 0,
            }
            for index in range(MAX_OBSERVED_CONSUMER_GROUPS + 1)
        ]
        result = evaluate_consumer_group_governance(
            stream_key="lottery_tasks:xiaohongshu",
            groups=groups,
            consumers_by_group={},
            stale_after_milliseconds=900_000,
            stream_length=0,
        )

        self.assertFalse(result["available"])
        self.assertTrue(result["retention_alert"])
        self.assertEqual(
            result["groups_inspected"],
            MAX_OBSERVED_CONSUMER_GROUPS,
        )
        self.assertIn(
            "consumer_group_inventory_too_large",
            result["warning_codes"],
        )


class RedisConsumerGroupTopologyPreflightTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_exact_fixed_groups_are_accepted(self):
        class FakeRedis:
            async def xinfo_groups(self, stream_key):
                return [
                    {"name": name}
                    for name in expected_consumer_group_names(stream_key)
                ]

        await verify_redis_consumer_groups(
            FakeRedis(),
            REDIS_CONSUMER_GROUP_SPECS,
        )

    async def test_oversized_inventory_fails_closed(self):
        class FakeRedis:
            async def xinfo_groups(self, _stream_key):
                return [
                    {"name": f"group-{index}"}
                    for index in range(
                        MAX_OBSERVED_CONSUMER_GROUPS + 1
                    )
                ]

        with self.assertRaisesRegex(
            RedisConsumerGroupTopologyError,
            "redis_consumer_group_inventory_too_large",
        ):
            await verify_redis_consumer_groups(
                FakeRedis(),
                REDIS_CONSUMER_GROUP_SPECS[:1],
            )


class RedisConsumerMetadataRetirementTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_only_stale_zero_pending_managed_consumer_is_deleted(self):
        class FakeRedis:
            def __init__(self):
                self.eval_calls = []

            async def xinfo_groups(self, _stream_key):
                return [{"name": "notify-dispatchers"}]

            async def xinfo_consumers(self, _stream_key, _group_name):
                return [
                    {
                        "name": "core-notify:current",
                        "pending": 0,
                        "idle": 900_000,
                    },
                    {
                        "name": "core-notify:stale",
                        "pending": 0,
                        "idle": 900_000,
                    },
                    {
                        "name": "core-notify:pending",
                        "pending": 1,
                        "idle": 900_000,
                    },
                    {
                        "name": "core-notify:active",
                        "pending": 0,
                        "idle": 1,
                    },
                    {
                        "name": "foreign-consumer",
                        "pending": 0,
                        "idle": 900_000,
                    },
                ]

            async def eval(self, *args):
                self.eval_calls.append(args)
                return ["deleted", "0"]

        redis = FakeRedis()
        summary = await retire_stale_consumer_metadata(
            redis,
            stream_key="notify_events",
            group_name="notify-dispatchers",
            current_consumer_name="core-notify:current",
            managed_consumer_prefix="core-notify:",
            minimum_idle_milliseconds=300_000,
        )

        self.assertEqual(summary["inventory"], 5)
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["retired"], 1)
        self.assertEqual(len(redis.eval_calls), 1)
        self.assertEqual(
            redis.eval_calls[0][3:],
            (
                "notify-dispatchers",
                "core-notify:stale",
                "300000",
            ),
        )

    async def test_oversized_consumer_inventory_fails_closed(self):
        class FakeRedis:
            async def xinfo_groups(self, _stream_key):
                return [{"name": "notify-dispatchers"}]

            async def xinfo_consumers(self, _stream_key, _group_name):
                return [
                    {
                        "name": f"core-notify:{index}",
                        "pending": 0,
                        "idle": 900_000,
                    }
                    for index in range(3)
                ]

        with self.assertRaisesRegex(
            RedisConsumerRetentionError,
            "redis_consumer_retention_inventory_too_large",
        ):
            await retire_stale_consumer_metadata(
                FakeRedis(),
                stream_key="notify_events",
                group_name="notify-dispatchers",
                current_consumer_name="core-notify:current",
                managed_consumer_prefix="core-notify:",
                minimum_idle_milliseconds=300_000,
                max_inventory=2,
            )


if __name__ == "__main__":
    unittest.main()
