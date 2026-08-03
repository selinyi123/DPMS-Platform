import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.services import risk_engine


class _HealthDatabase:
    def __init__(self):
        self.fetch_one_calls = []
        self.fetch_all_calls = []
        self.execute_calls = []

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls.append((query, values or {}))
        return {"cnt": 0}

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls.append((query, values or {}))
        return []

    async def execute(self, query, values=None):
        self.execute_calls.append((query, values or {}))
        return None


class _FrozenDateTime:
    current = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


class AccountHealthQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_queries_use_one_bounded_sargable_cutoff(self):
        fake_database = _HealthDatabase()
        with (
            patch.object(risk_engine, "database", fake_database),
            patch.object(risk_engine, "datetime", _FrozenDateTime),
            patch.object(
                risk_engine,
                "record_event",
                new=AsyncMock(),
            ),
        ):
            result = await risk_engine.check_all_accounts_health(
                cooldown_minutes=0,
                stale_execution_minutes=999,
            )

        all_calls = (
            fake_database.fetch_one_calls
            + fake_database.fetch_all_calls
            + fake_database.execute_calls
        )
        all_sql = "\n".join(query for query, _ in all_calls)
        self.assertNotIn("TIMESTAMPDIFF", all_sql)

        expected_cooldown = _FrozenDateTime.current.replace(
            tzinfo=None
        ) - timedelta(minutes=1)
        expected_stale = _FrozenDateTime.current.replace(
            tzinfo=None
        ) - timedelta(minutes=120)

        cooling_calls = [
            (query, values)
            for query, values in all_calls
            if "a.status = 'cooling'" in query
        ]
        self.assertEqual(len(cooling_calls), 3)
        for query, values in cooling_calls:
            self.assertIn("a.updated_at <= :cooldown_cutoff", query)
            self.assertIn("r.created_at > :cooldown_cutoff", query)
            self.assertEqual(values["cooldown_cutoff"], expected_cooldown)

        stale_calls = [
            (query, values)
            for query, values in all_calls
            if "status = 'executing'" in query
        ]
        self.assertEqual(len(stale_calls), 4)
        for query, values in stale_calls:
            self.assertIn("updated_at <= :stale_execution_cutoff", query)
            self.assertEqual(
                values["stale_execution_cutoff"],
                expected_stale,
            )

        bounded_lists = [
            query
            for query, _ in fake_database.fetch_all_calls
            if "LIMIT 500" in query
        ]
        self.assertEqual(len(bounded_lists), 3)
        self.assertEqual(result["cooldown_minutes"], 1)
        self.assertEqual(result["stale_execution_minutes"], 120)


if __name__ == "__main__":
    unittest.main()
