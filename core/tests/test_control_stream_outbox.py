"""Redis continuity and replay contracts for control-stream Outbox lanes."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.account_calibration_streams import (
    account_calibration_stream_binding_for_platform,
)
from app.adapter_probe_streams import (
    adapter_probe_stream_binding_for_platform,
)
from app.services import outbox


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeDatabase:
    def __init__(self, affected: int = 1):
        self.affected = affected
        self.executions: list[tuple[str, dict]] = []

    def transaction(self):
        return Transaction()

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))

    async def fetch_one(self, query, values=None):
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        raise AssertionError(query)


class ControlStreamOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_epoch_replay_is_scoped_to_queued_probe_rows(self):
        binding = adapter_probe_stream_binding_for_platform("weibo")
        fake = FakeDatabase()
        with patch.object(outbox, "database", fake), patch.object(
            outbox,
            "_read_stream_lane_epochs",
            AsyncMock(return_value={binding.stream_key: "epoch-probe"}),
        ), patch.object(outbox, "structured_log"):
            affected = (
                await outbox.reconcile_redis_adapter_probe_stream_epochs()
            )

        self.assertEqual(affected, 1)
        query, values = fake.executions[0]
        self.assertIn("UPDATE adapter_calibrations", query)
        self.assertIn("ac.status = 'queued'", query)
        self.assertIn("o.status = 'sent'", query)
        self.assertEqual(values["probe_stream_0"], binding.stream_key)
        self.assertEqual(values["probe_epoch_0"], "epoch-probe")

    async def test_calibration_epoch_replay_is_scoped_to_queued_rows(self):
        binding = account_calibration_stream_binding_for_platform(
            "douyin"
        )
        fake = FakeDatabase()
        with patch.object(outbox, "database", fake), patch.object(
            outbox,
            "_read_stream_lane_epochs",
            AsyncMock(
                return_value={binding.stream_key: "epoch-calibration"}
            ),
        ), patch.object(outbox, "structured_log"):
            affected = await (
                outbox.reconcile_redis_account_calibration_stream_epochs()
            )

        self.assertEqual(affected, 1)
        query, values = fake.executions[0]
        self.assertIn("UPDATE account_calibrations", query)
        self.assertIn("c.status = 'queued'", query)
        self.assertIn("o.status = 'sent'", query)
        self.assertEqual(
            values["calibration_stream_0"],
            binding.stream_key,
        )
        self.assertEqual(
            values["calibration_epoch_0"],
            "epoch-calibration",
        )

    async def test_control_delivery_uses_lane_continuity_before_xadd(self):
        binding = adapter_probe_stream_binding_for_platform("bilibili")
        fake_db = AsyncMock()
        fake_db.execute = AsyncMock()
        fake_redis = AsyncMock()
        fake_redis.xadd = AsyncMock(return_value="1-0")
        epoch = AsyncMock(side_effect=("epoch-1", "epoch-1"))
        payload = {
            "probe_id": "probe-1",
            "platform": "bilibili",
            "account_id": "7",
            "lottery_id": "11",
            "target_url": "https://t.bilibili.com/123",
            "canonical_url": "bilibili://dynamic/123",
            "execution_path_id": "bilibili_api_v2",
            "target_hash": "a" * 64,
            "rule_snapshot_id": "13",
            "rule_hash": "b" * 64,
            "action_plan_hash": "c" * 64,
            "config_hash": "d" * 64,
            "execution_revision": "2",
            "account_lease_id": "lease-1",
            "account_lease_generation": "1",
        }
        with patch.object(outbox, "database", fake_db), patch.object(
            outbox, "redis", fake_redis
        ), patch.object(
            outbox, "execute_affected_rows", AsyncMock(return_value=1)
        ), patch.object(
            outbox, "read_redis_task_stream_epoch", epoch
        ):
            delivered = await outbox._deliver_claimed(
                {
                    "id": 1,
                    "stream_key": binding.stream_key,
                    "payload": json.dumps(payload),
                    "attempts": 1,
                    "dedup_key": "adapter-probe:probe-1",
                }
            )

        self.assertTrue(delivered)
        self.assertEqual(epoch.await_count, 2)
        self.assertEqual(
            epoch.await_args_list[0].kwargs,
            {"prepare_delivery": True},
        )
        fake_redis.xadd.assert_awaited_once()

    async def test_incomplete_platform_probe_envelope_never_reaches_redis(self):
        binding = adapter_probe_stream_binding_for_platform("bilibili")
        fake_db = AsyncMock()
        fake_db.execute = AsyncMock()
        fake_redis = AsyncMock()
        with patch.object(outbox, "database", fake_db), patch.object(
            outbox,
            "redis",
            fake_redis,
        ):
            delivered = await outbox._deliver_claimed(
                {
                    "id": 1,
                    "stream_key": binding.stream_key,
                    "payload": (
                        '{"probe_id":"probe-1",'
                        '"platform":"bilibili"}'
                    ),
                    "attempts": 1,
                    "dedup_key": "adapter-probe:probe-1",
                }
            )

        self.assertFalse(delivered)
        fake_redis.xadd.assert_not_awaited()
        fake_db.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
