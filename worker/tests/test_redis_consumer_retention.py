import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import redis_consumer_retention as retention
from shared.login_streams import (
    LOGIN_REQUEST_GROUP_NAME,
    LOGIN_REQUEST_STREAM_KEY,
)


def group_spec():
    return SimpleNamespace(
        stream_key="lottery_tasks:bilibili",
        group_name="workers:bilibili",
    )


def login_group_spec():
    return SimpleNamespace(
        stream_key=LOGIN_REQUEST_STREAM_KEY,
        group_name=LOGIN_REQUEST_GROUP_NAME,
    )


class RedisConsumerRetentionTests(unittest.IsolatedAsyncioTestCase):
    def test_retirement_window_matches_core_governance_window(self):
        self.assertEqual(
            retention.REDIS_CONSUMER_RETIRE_IDLE_SECONDS,
            retention.settings.redis_consumer_group_stale_seconds,
        )

    def test_login_control_group_is_in_retention_scope(self):
        specs = retention._worker_group_specs()
        self.assertTrue(
            any(
                spec.stream_key == LOGIN_REQUEST_STREAM_KEY
                and spec.group_name == LOGIN_REQUEST_GROUP_NAME
                and spec.subsystem == "login"
                for spec in specs
            )
        )

    async def test_live_heartbeat_is_never_deleted(self):
        name = "dpms-worker-bilibili"
        db = SimpleNamespace(
            fetch_all=AsyncMock(return_value=[{"worker_id": name}])
        )
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": 10**12,
                    }
                ]
            ),
            eval=AsyncMock(),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["skipped_live"], 1)
        redis.eval.assert_not_awaited()

    async def test_stale_zero_pending_identity_uses_atomic_recheck(self):
        name = "dpms-worker-weibo"
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": 10**12,
                    }
                ]
            ),
            eval=AsyncMock(return_value=["deleted", "0"]),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["retired"], 1)
        args = redis.eval.await_args.args
        self.assertIn("blocked_pending", args[0])
        self.assertIn("blocked_active", args[0])
        self.assertEqual(
            args[1:5],
            (
                1,
                "lottery_tasks:bilibili",
                "workers:bilibili",
                name,
            ),
        )

    async def test_consumer_reactivated_after_observation_is_not_counted(self):
        name = "dpms-worker-douyin"
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": 10**12,
                    }
                ]
            ),
            eval=AsyncMock(return_value=["blocked_active", "1"]),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["retired"], 0)

    async def test_prior_restart_login_consumer_is_retired(self):
        name = f"control-host:4321:{'a' * 32}"
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": 10**12,
                    }
                ]
            ),
            eval=AsyncMock(return_value=["deleted", "0"]),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(login_group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["retired"], 1)
        self.assertEqual(
            redis.eval.await_args.args[1:5],
            (
                1,
                LOGIN_REQUEST_STREAM_KEY,
                LOGIN_REQUEST_GROUP_NAME,
                name,
            ),
        )

    async def test_active_login_consumer_is_not_deleted(self):
        name = f"control-host:4321:{'b' * 32}"
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": (
                            retention.REDIS_CONSUMER_RETIRE_IDLE_SECONDS
                            * 1000
                        ),
                    }
                ]
            ),
            eval=AsyncMock(),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(login_group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(summary["retired"], 0)
        redis.eval.assert_not_awaited()

    async def test_login_consumer_reactivated_during_atomic_recheck_survives(
        self,
    ):
        name = f"control-host:4321:{'c' * 32}"
        db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
        redis = SimpleNamespace(
            xinfo_consumers=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "pending": 0,
                        "idle": 10**12,
                    }
                ]
            ),
            eval=AsyncMock(return_value=["blocked_active", "1"]),
        )
        with patch.object(
            retention,
            "_worker_group_specs",
            return_value=(login_group_spec(),),
        ):
            summary = await retention.retire_stale_redis_consumers_once(
                redis_client=redis,
                db=db,
            )

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["retired"], 0)


if __name__ == "__main__":
    unittest.main()
