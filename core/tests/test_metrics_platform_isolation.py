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


def platform_config(label, *, module_available=True):
    return {
        "label": label,
        "module_available": module_available,
        "execution_mode": (
            "browser" if module_available else "unavailable"
        ),
        "adapter_status": (
            "planned" if module_available else "module_unavailable"
        ),
        "action_adapter": False,
        "qr_login": False,
        "cookie_login": True,
    }


def transport(platform):
    return {
        "platform": platform,
        "available": True,
        "ready": True,
        "standard_ready": True,
        "repair_ready": True,
        "workers_online": 1,
        "redis_consumers_online": 1,
        "lanes_total": 2,
        "lanes_ready": 2,
        "consumers_available": True,
        "worker_heartbeats_available": True,
        "outbox_available": True,
        "outbox_undelivered": 0,
        "outbox_stale_undelivered": 0,
        "outbox_oldest_age_seconds": 0,
        "blocker_codes": [],
        "lanes": [],
    }


def task_metrics(platforms):
    transports = {
        platform: transport(platform)
        for platform in platforms
    }
    return {
        "workers_online": 1,
        "worker_heartbeats_online": 1,
        "worker_heartbeats_available": True,
        "workers_online_by_platform": {
            platform: 1
            for platform in platforms
        },
        "transport_by_platform": transports,
        "task_streams": [],
        "pending_by_platform": {
            platform: 0
            for platform in platforms
        },
        "pending_available": True,
        "legacy_pending": 0,
        "legacy_lag": 0,
        "legacy_outbox_undelivered": 0,
        "legacy_drain_complete": True,
        "stale_running": {
            "available": True,
            "total": 0,
            "by_platform": {},
        },
        "outbox_undelivered": 0,
        "outbox_undelivered_available": True,
        "outbox_undelivered_by_platform": {
            platform: 0
            for platform in platforms
        },
        "outbox_stale_by_platform": {
            platform: 0
            for platform in platforms
        },
        "outbox_stale_after_seconds": 120,
    }


def control_metrics(platforms):
    by_platform = {
        platform: {
            "adapter_probe": {"ready": True},
            "account_calibration": {"ready": True},
            "available": True,
            "ready": True,
        }
        for platform in platforms
    }
    return {
        "by_platform": by_platform,
        "adapter_probe": {
            "by_platform": {
                platform: {"ready": True}
                for platform in platforms
            },
            "legacy": {"drain_complete": True},
            "streams": [],
        },
        "account_calibration": {
            "by_platform": {
                platform: {"ready": True}
                for platform in platforms
            },
            "legacy": {"drain_complete": True},
            "streams": [],
        },
        "legacy_control_stream_drain_complete": True,
    }


class RedisRetentionNextActionTests(unittest.TestCase):
    @staticmethod
    def summary(alerts):
        return {
            "redis_consumer_group_retention_alerts": alerts,
            "notification_channels_configured": 1,
            "recent_risk_events_24h": 0,
            "proxy_exits_total": 1,
            "dry_run_ready": 0,
            "real_run_ready": 0,
        }

    def test_stale_metadata_has_nonblocking_atomic_cleanup_action(self):
        actions = metrics.build_next_actions(
            [],
            self.summary(
                [
                    {
                        "retention_alert": False,
                        "consumer_inventory_alert": True,
                        "stale_consumer_entries": 12,
                    }
                ]
            ),
        )

        self.assertEqual(
            [item["code"] for item in actions],
            ["retire_stale_redis_consumer_metadata"],
        )
        self.assertEqual(actions[0]["priority"], "P1")
        self.assertIn("12 zero-pending", actions[0]["detail"])

    def test_true_group_retention_blocker_remains_p0(self):
        actions = metrics.build_next_actions(
            [],
            self.summary(
                [
                    {
                        "retention_alert": True,
                        "consumer_inventory_alert": True,
                        "stale_consumer_entries": 1,
                    }
                ]
            ),
        )

        self.assertEqual(
            [item["code"] for item in actions],
            ["resolve_redis_consumer_group_retention"],
        )
        self.assertEqual(actions[0]["priority"], "P0")

    def test_prometheus_platform_scalar_mapping_uses_platform_labels(self):
        lines = metrics._prometheus_lines(
            {
                "task_outbox_undelivered_by_platform": {
                    "bilibili": 3,
                    "weibo": 0,
                },
            }
        )

        self.assertIn(
            'dpms_task_outbox_undelivered_by_platform{platform="bilibili"} 3',
            lines,
        )
        self.assertIn(
            'dpms_task_outbox_undelivered_by_platform{platform="weibo"} 0',
            lines,
        )


class ReadinessDatabase:
    def __init__(
        self,
        *,
        failed_account_platform=None,
        global_breaker_status="closed",
    ):
        self.failed_account_platform = failed_account_platform
        self.global_breaker_status = global_breaker_status
        self.fetch_one_queries = []

    async def fetch_one(self, query, values=None):
        self.fetch_one_queries.append(query)
        platform = (values or {}).get("platform")
        if "FROM accounts a" in query:
            if (
                self.failed_account_platform is not None
                and platform == self.failed_account_platform
            ):
                raise RuntimeError("platform account read failed")
            return {"cnt": 1 if platform else 0}
        if "FROM adapter_calibrations" in query:
            return None
        if "FROM risk_events" in query:
            return {"cnt": 0}
        if "FROM proxies" in query:
            return {"cnt": 0}
        if "FROM lotteries" in query:
            return {"cnt": 0}
        if "FROM circuit_breakers" in query:
            return {
                "status": self.global_breaker_status,
                "reason": None,
                "opened_at": None,
                "updated_at": None,
            }
        if "FROM worker_heartbeats" in query:
            return None
        raise AssertionError(f"unexpected fetch_one: {query}")

    async def fetch_all(self, query, _values=None):
        if "FROM accounts a" in query and "account_calibrations" in query:
            return []
        raise AssertionError(f"unexpected fetch_all: {query}")


class MetricsPlatformIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def call_readiness(
        self,
        platform_configs,
        database,
        *,
        observed_task_metrics=None,
        real_run_enabled=False,
        adapter_kind="manual_assisted",
        runtime_adapter_enabled=False,
        probe_ready=False,
    ):
        platform_names = tuple(platform_configs)
        observed_task_metrics = (
            observed_task_metrics
            if observed_task_metrics is not None
            else task_metrics(platform_names)
        )
        with patch.object(
            metrics,
            "collect_task_stream_metrics",
            AsyncMock(return_value=observed_task_metrics),
        ), patch.object(
            metrics,
            "collect_control_stream_metrics",
            AsyncMock(return_value=control_metrics(platform_names)),
        ), patch.object(
            metrics,
            "is_real_run_enabled",
            AsyncMock(return_value=real_run_enabled),
        ), patch.object(
            metrics,
            "load_runtime_selector_config",
            AsyncMock(return_value={}),
        ), patch.object(
            metrics,
            "get_platforms",
            return_value=platform_configs,
        ), patch.object(
            metrics,
            "database",
            database,
        ), patch.object(
            metrics,
            "configured_channels",
            AsyncMock(return_value=[]),
        ), patch.object(
            metrics,
            "platform_selectors_complete",
            return_value=False,
        ), patch.object(
            metrics,
            "platform_real_adapter_kind",
            return_value=adapter_kind,
        ), patch.object(
            metrics,
            "platform_has_runtime_real_adapter",
            return_value=runtime_adapter_enabled,
        ), patch.object(
            metrics,
            "platform_probe_ready_for_real_actions",
            return_value=probe_ready,
        ), patch.object(
            metrics,
            "build_next_actions",
            return_value=[],
        ), patch.object(
            metrics,
            "build_production_checks",
            return_value=[],
        ), patch.object(
            metrics,
            "build_strategy_advice",
            AsyncMock(return_value={"advice": []}),
        ), patch.object(
            metrics,
            "structured_log",
        ):
            return await metrics.readiness()

    async def test_failed_module_is_reported_without_reloading_or_poisoning_peer(
        self,
    ):
        configs = {
            "weibo": platform_config(
                "Weibo",
                module_available=False,
            ),
            "bilibili": platform_config("Bilibili"),
        }
        with patch.object(
            metrics,
            "action_order_for_platform",
            side_effect=AssertionError("failed module was reloaded"),
        ):
            result = await self.call_readiness(
                configs,
                ReadinessDatabase(),
            )

        by_platform = {
            item["platform"]: item
            for item in result["platforms"]
        }
        self.assertFalse(by_platform["weibo"]["module_available"])
        self.assertEqual(
            by_platform["weibo"]["blocker_codes"],
            ["platform_module_unavailable"],
        )
        self.assertTrue(by_platform["bilibili"]["module_available"])
        self.assertEqual(by_platform["bilibili"]["safe_accounts"], 1)

    async def test_platform_database_failure_does_not_poison_peer(self):
        configs = {
            "bilibili": platform_config("Bilibili"),
            "douyin": platform_config("Douyin"),
        }
        result = await self.call_readiness(
            configs,
            ReadinessDatabase(failed_account_platform="bilibili"),
        )

        by_platform = {
            item["platform"]: item
            for item in result["platforms"]
        }
        self.assertEqual(
            by_platform["bilibili"]["blocker_codes"],
            ["platform_readiness_database_unavailable"],
        )
        self.assertFalse(by_platform["bilibili"]["ready_for_dry_run"])
        self.assertEqual(by_platform["douyin"]["safe_accounts"], 1)
        self.assertTrue(
            by_platform["douyin"]["capability_ready_for_dry_run"]
        )

    async def test_run_readiness_requires_own_standard_transport(self):
        configs = {
            "bilibili": platform_config("Bilibili"),
            "douyin": platform_config("Douyin"),
        }
        observed = task_metrics(tuple(configs))
        observed["transport_by_platform"]["bilibili"].update(
            {
                "ready": False,
                "standard_ready": False,
                "blocker_codes": [
                    "standard_task_consumer_heartbeat_missing"
                ],
            }
        )
        result = await self.call_readiness(
            configs,
            ReadinessDatabase(),
            observed_task_metrics=observed,
        )

        by_platform = {
            item["platform"]: item
            for item in result["platforms"]
        }
        self.assertTrue(
            by_platform["bilibili"]["capability_ready_for_dry_run"]
        )
        self.assertFalse(by_platform["bilibili"]["ready_for_dry_run"])
        self.assertTrue(by_platform["douyin"]["ready_for_dry_run"])

    async def test_platform_capability_failure_does_not_poison_peer(self):
        configs = {
            "weibo": platform_config("Weibo"),
            "bilibili": platform_config("Bilibili"),
        }
        with patch.object(
            metrics,
            "action_order_for_platform",
            side_effect=RuntimeError("weibo module unavailable"),
        ):
            result = await self.call_readiness(
                configs,
                ReadinessDatabase(),
            )

        by_platform = {
            item["platform"]: item
            for item in result["platforms"]
        }
        self.assertEqual(
            by_platform["weibo"]["blocker_codes"],
            ["platform_readiness_capability_unavailable"],
        )
        self.assertEqual(by_platform["bilibili"]["safe_accounts"], 1)

    async def test_open_global_breaker_blocks_platform_real_capability(self):
        config = platform_config("Bilibili")
        config["action_adapter"] = True
        result = await self.call_readiness(
            {"bilibili": config},
            ReadinessDatabase(global_breaker_status="open"),
            real_run_enabled=True,
            adapter_kind="selector",
            probe_ready=True,
        )

        platform = result["platforms"][0]
        self.assertTrue(platform["real_actions_ready"])
        self.assertFalse(platform["capability_ready_for_real_run"])
        self.assertFalse(platform["ready_for_real_run"])
        self.assertIn(
            "global_circuit_breaker_not_closed",
            platform["blocker_codes"],
        )
        self.assertEqual(
            result["summary"]["global_circuit_breaker"]["status"],
            "open",
        )

    async def test_pending_target_count_excludes_expired_lotteries(self):
        observed_database = ReadinessDatabase()
        result = await self.call_readiness(
            {"bilibili": platform_config("Bilibili")},
            observed_database,
        )

        lottery_queries = [
            query
            for query in observed_database.fetch_one_queries
            if "FROM lotteries" in query
        ]
        self.assertEqual(len(lottery_queries), 1)
        self.assertIn("expires_at IS NULL", lottery_queries[0])
        self.assertIn("expires_at > UTC_TIMESTAMP()", lottery_queries[0])
        self.assertIn("autopilot", result["summary"])


if __name__ == "__main__":
    unittest.main()
