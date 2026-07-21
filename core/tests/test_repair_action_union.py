import unittest

from app.api import lotteries


class FakeRepairDatabase:
    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return [{"phase": "liked"}]
        if "FROM events" in query:
            return [{"phase": "followed"}, {"phase": "commented"}]
        if "FROM task_phases" in query:
            # The real schema keeps only the latest phase per task. This is a
            # legacy fallback, not a source of complete per-action history.
            return [{"phase": "reposted"}]
        raise AssertionError(f"Unexpected query: {query}")


class PartialRepairDatabase:
    async def fetch_all(self, query, values=None):
        if "FROM bilibili_action_ledger" in query:
            return [{"phase": "liked"}]
        if "FROM events" in query or "FROM task_phases" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")


class RepairActionUnionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_legacy_phases_and_action_ledger_are_unioned(self):
        original_database = lotteries.database
        lotteries.database = FakeRepairDatabase()
        try:
            completed = await lotteries.completed_real_run_actions(73)
        finally:
            lotteries.database = original_database

        self.assertEqual(completed, ["followed", "liked", "commented", "reposted"])

    async def test_completed_action_evidence_failure_is_fail_closed(self):
        class FailingDatabase:
            async def fetch_all(self, _query, _values=None):
                raise RuntimeError("ledger unavailable")

        original_database = lotteries.database
        lotteries.database = FailingDatabase()
        try:
            with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
                await lotteries.completed_real_run_actions(73)
        finally:
            lotteries.database = original_database

    async def test_active_execution_is_not_advertised_as_repair_eligible(self):
        original_database = lotteries.database
        lotteries.database = PartialRepairDatabase()
        try:
            plan = await lotteries.build_lottery_repair_plan(
                {
                    "id": 73,
                    "status": "running",
                    "execution_lock": "task-active",
                    "action_plan": {"required_actions": ["liked", "commented"]},
                }
            )
        finally:
            lotteries.database = original_database

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["reason"], "execution_in_flight_or_reconciliation_required")

    async def test_terminal_lottery_is_not_advertised_as_repair_eligible(self):
        original_database = lotteries.database
        lotteries.database = PartialRepairDatabase()
        try:
            plan = await lotteries.build_lottery_repair_plan(
                {
                    "id": 73,
                    "status": "participated",
                    "execution_lock": None,
                    "action_plan": {"required_actions": ["liked", "commented"]},
                }
            )
        finally:
            lotteries.database = original_database

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["reason"], "lottery_not_pending")


if __name__ == "__main__":
    unittest.main()
