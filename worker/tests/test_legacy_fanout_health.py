import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import account_calibrator, adapter_probe


class LegacyFanoutHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_failure_is_published_before_retry(self):
        shutdown = asyncio.Event()
        fake_redis = AsyncMock()
        fake_redis.xgroup_create.side_effect = RuntimeError("redis down")

        def record_failure(*_args):
            shutdown.set()

        with (
            patch.object(adapter_probe, "redis", fake_redis),
            patch.object(
                adapter_probe,
                "record_runtime_lane_failure",
                side_effect=record_failure,
            ) as failure,
            patch.object(adapter_probe, "structured_log"),
        ):
            await adapter_probe._legacy_probe_fanout_loop(shutdown)

        failure.assert_called_once()
        self.assertEqual(
            failure.call_args.args[:2],
            ("legacy_probe_fanout", None),
        )

    async def test_calibration_failure_is_published_before_retry(self):
        shutdown = asyncio.Event()
        fake_redis = AsyncMock()
        fake_redis.xgroup_create.side_effect = RuntimeError("redis down")

        def record_failure(*_args):
            shutdown.set()

        with (
            patch.object(account_calibrator, "redis", fake_redis),
            patch.object(
                account_calibrator,
                "record_runtime_lane_failure",
                side_effect=record_failure,
            ) as failure,
            patch.object(account_calibrator, "structured_log"),
        ):
            await account_calibrator._legacy_calibration_fanout_loop(
                shutdown
            )

        failure.assert_called_once()
        self.assertEqual(
            failure.call_args.args[:2],
            ("legacy_calibration_fanout", None),
        )


if __name__ == "__main__":
    unittest.main()
