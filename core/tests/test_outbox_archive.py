import unittest
from unittest.mock import AsyncMock, patch

from app.services import outbox


class OutboxArchiveContractTests(unittest.IsolatedAsyncioTestCase):
    def test_cutoff_requires_a_bounded_retention_window(self):
        with self.assertRaises(ValueError):
            outbox._archive_cutoff_datetime(3599)
        self.assertIsNotNone(outbox._archive_cutoff_datetime(3600))

    async def test_archive_requires_a_matching_continuity_watermark(self):
        fake_database = AsyncMock()
        fake_database.transaction.return_value.__aenter__ = AsyncMock()
        fake_database.transaction.return_value.__aexit__ = AsyncMock()
        fake_database.fetch_one = AsyncMock(
            return_value={
                "continuity_epoch": "old-epoch",
                "safe_outbox_id": 10,
            }
        )
        with (
            patch.object(outbox, "database", fake_database),
            patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                AsyncMock(return_value="new-epoch"),
            ),
        ):
            result = await outbox.archive_sent_outbox_once(
                "lottery_tasks:bilibili",
                retention_seconds=3600,
            )
        self.assertEqual(result["archived"], 0)
        fake_database.fetch_all.assert_not_awaited()

    async def test_archive_rejects_a_non_contiguous_watermark(self):
        fake_database = AsyncMock()
        fake_database.transaction.return_value.__aenter__ = AsyncMock()
        fake_database.transaction.return_value.__aexit__ = AsyncMock()
        fake_database.fetch_one = AsyncMock(
            side_effect=[
                {
                    "continuity_epoch": "redis:v1:global",
                    "safe_outbox_id": 10,
                },
                {"cnt": 1},
            ]
        )
        with (
            patch.object(outbox, "database", fake_database),
            patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                AsyncMock(return_value="redis:v1:global"),
            ),
        ):
            result = await outbox.archive_sent_outbox_once(
                "lottery_tasks:bilibili",
                retention_seconds=3600,
            )
        self.assertEqual(result["archived"], 0)
        fake_database.fetch_all.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
