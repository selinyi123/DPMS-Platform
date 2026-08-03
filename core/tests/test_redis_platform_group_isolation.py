import unittest
from unittest.mock import patch

import redis.asyncio as aioredis

from app.db import RedisClient
from shared.redis_consumer_groups import (
    RedisConsumerGroupTopologyError,
    expected_consumer_group_names,
    runtime_consumer_group_specs,
)


class FakeRedis:
    def __init__(self, *, failing_stream: str | None = None, error: str = ""):
        self.failing_stream = failing_stream
        self.error = error
        self.calls = []
        self.closed = False

    async def xinfo_groups(self, stream):
        self.calls.append(str(stream))
        if stream == self.failing_stream:
            raise aioredis.ResponseError(self.error)
        return [
            {"name": group_name}
            for group_name in expected_consumer_group_names(str(stream))
        ]

    async def close(self):
        self.closed = True


class RedisPlatformGroupIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_deployment_verifies_only_its_fixed_topology(self):
        fake = FakeRedis()
        client = RedisClient()

        with patch("app.db.aioredis.from_url", return_value=fake):
            await client.initialize(
                platforms=("weibo",),
                include_shared=False,
            )

        expected_streams = {
            spec.stream_key
            for spec in runtime_consumer_group_specs(
                "core",
                platforms=("weibo",),
                include_shared=False,
            )
        }
        self.assertEqual(set(fake.calls), expected_streams)
        self.assertTrue(all("bilibili" not in key for key in fake.calls))
        self.assertTrue(all("douyin" not in key for key in fake.calls))
        self.assertTrue(all("xiaohongshu" not in key for key in fake.calls))
        await client.close()
        self.assertTrue(fake.closed)

    async def test_owned_lane_topology_failure_aborts_deployment(self):
        owned = runtime_consumer_group_specs(
            "core",
            platforms=("weibo",),
            include_shared=False,
        )
        fake = FakeRedis(
            failing_stream=owned[0].stream_key,
            error="WRONGTYPE Operation against a key holding the wrong kind",
        )
        client = RedisClient()

        with patch("app.db.aioredis.from_url", return_value=fake):
            with self.assertRaisesRegex(
                RedisConsumerGroupTopologyError,
                "redis_consumer_group_topology_unavailable",
            ):
                await client.initialize(
                    platforms=("weibo",),
                    include_shared=False,
                )


if __name__ == "__main__":
    unittest.main()
