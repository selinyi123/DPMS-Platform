import base64
import json
import os
import unittest
from datetime import datetime

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import lotteries  # noqa: E402
from app.services import real_run_readiness  # noqa: E402


class EvidenceBatchDatabase:
    def __init__(self):
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if "FROM adapter_calibrations" in query:
            return [
                {
                    "id": 9,
                    "lottery_id": 2,
                    "platform": "weibo",
                    "account_id": 5,
                    "result": json.dumps({"_summary": {"ready_for_real_actions": True}}),
                }
            ]
        if "FROM task_runs" in query:
            return [
                {
                    "id": 20,
                    "lottery_id": 1,
                    "task_id": "shadow-1",
                    "account_id": 4,
                    "screenshot_path": "/profiles/shadow-runs/shared.png",
                },
                {
                    "id": 21,
                    "lottery_id": 2,
                    "task_id": "shadow-2",
                    "account_id": 5,
                    "screenshot_path": "/profiles/shadow-runs/shared.png",
                },
            ]
        if "FROM events" in query:
            return [
                {"aggregate_id": "shadow-1", "payload": "{}"},
                {"aggregate_id": "shadow-2", "payload": "{}"},
            ]
        if "FROM evidence_files" in query:
            return [
                {
                    "id": 31,
                    "task_id": "shadow-1",
                    "account_id": 4,
                    "lottery_id": 1,
                    "file_path": "/profiles/shadow-runs/shared.png",
                    "sha256": "a" * 64,
                },
                {
                    "id": 32,
                    "task_id": "shadow-2",
                    "account_id": 5,
                    "lottery_id": 2,
                    "file_path": "/profiles/shadow-runs/shared.png",
                    "sha256": "a" * 64,
                },
            ]
        raise AssertionError(f"Unexpected query: {query}")


class AccountSummaryDatabase:
    def __init__(self):
        self.fetch_all_calls = 0
        self.fetch_one_calls = 0

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls += 1
        if "FROM accounts" in query:
            return [
                {"id": 4, "platform": "bilibili"},
                {"id": 5, "platform": "bilibili"},
                {"id": 6, "platform": "weibo"},
            ]
        if "FROM risk_events" in query:
            return [
                {
                    "id": 10,
                    "account_id": 5,
                    "event_type": "action_window",
                    "detail": json.dumps({"reason": "action_window"}),
                    "created_at": "2026-07-14 10:00:00",
                }
            ]
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "SELECT NOW() AS db_now" in query:
            return {"db_now": datetime(2026, 7, 14, 11, 0, 0)}
        raise AssertionError(f"Unexpected query: {query}")


class EndpointQueryDatabase:
    def __init__(self, lottery_count):
        self.lottery_count = lottery_count
        self.fetch_all_calls = 0
        self.fetch_one_calls = 0

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls += 1
        if "SELECT * FROM lotteries" in query:
            return [
                {
                    "id": index,
                    "platform": "bilibili",
                    "status": "pending",
                    "raw_url": f"https://www.bilibili.com/opus/{1220306071196794800 + index}",
                    "canonical_url": None,
                    "rule_text": "抽奖：关注并点赞本条动态",
                    "action_plan": json.dumps(
                        {
                            "required_actions": ["followed", "liked"],
                            "review_required": False,
                        }
                    ),
                    "execution_lock": None,
                }
                for index in range(1, self.lottery_count + 1)
            ]
        if "FROM accounts" in query:
            return []
        if "FROM task_runs" in query:
            return []
        if "FROM bilibili_action_ledger" in query:
            return []
        if "FROM events" in query:
            return []
        if "FROM task_phases" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "SELECT NOW() AS db_now" in query:
            return {"db_now": datetime(2026, 7, 14, 11, 0, 0)}
        raise AssertionError(f"Unexpected query: {query}")


class RealRunEvidenceBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_shadow_event_and_file_rows_use_four_queries_for_many_lotteries(self):
        fake = EvidenceBatchDatabase()
        original_database = real_run_readiness.database
        real_run_readiness.database = fake
        try:
            batch = await real_run_readiness.load_real_run_evidence_batch(
                [
                    {"id": 1, "platform": "bilibili"},
                    {"id": 2, "platform": "weibo"},
                ]
            )
        finally:
            real_run_readiness.database = original_database

        self.assertEqual(len(fake.queries), 4)
        self.assertTrue(all("ROW_NUMBER() OVER" in query for query in fake.queries))
        self.assertIn((2, "weibo"), batch.probes)
        self.assertEqual(batch.shadows[1]["task_id"], "shadow-1")
        self.assertEqual(batch.observations["shadow-2"]["payload"], "{}")
        self.assertEqual(batch.evidence_files[("shadow-1", "4", 1)]["sha256"], "a" * 64)

    async def test_account_risk_summaries_are_batched_across_platforms_and_accounts(self):
        fake = AccountSummaryDatabase()
        original_database = real_run_readiness.database
        real_run_readiness.database = fake
        try:
            summaries = await real_run_readiness.real_run_account_risk_summaries(
                ["bilibili", "weibo", "bilibili"]
            )
        finally:
            real_run_readiness.database = original_database

        self.assertEqual(fake.fetch_all_calls, 2)
        self.assertEqual(fake.fetch_one_calls, 1)
        self.assertEqual(summaries["bilibili"]["ready_accounts"], 2)
        self.assertEqual(summaries["bilibili"]["runnable_accounts"], 1)
        self.assertEqual(summaries["weibo"]["ready_accounts"], 1)
        self.assertEqual(summaries["weibo"]["runnable_accounts"], 1)

    async def test_evidence_endpoint_query_count_does_not_scale_with_lottery_count(self):
        async def selector_config():
            return {}

        async def real_run_enabled():
            return False

        original_api_database = lotteries.database
        original_readiness_database = real_run_readiness.database
        original_selector_loader = lotteries.load_runtime_selector_config
        original_setting_loader = lotteries.is_real_run_enabled
        counts = []
        try:
            lotteries.load_runtime_selector_config = selector_config
            lotteries.is_real_run_enabled = real_run_enabled
            for lottery_count in (1, 50):
                fake = EndpointQueryDatabase(lottery_count)
                lotteries.database = fake
                real_run_readiness.database = fake
                result = await lotteries.list_real_run_evidence(limit=lottery_count)
                self.assertEqual(len(result["items"]), lottery_count)
                counts.append((fake.fetch_all_calls, fake.fetch_one_calls))
        finally:
            lotteries.database = original_api_database
            real_run_readiness.database = original_readiness_database
            lotteries.load_runtime_selector_config = original_selector_loader
            lotteries.is_real_run_enabled = original_setting_loader

        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[0], (7, 1))

    async def test_batch_query_failure_propagates_instead_of_returning_ready_context(self):
        class FailingDatabase:
            async def fetch_all(self, query, values=None):
                raise RuntimeError("evidence storage unavailable")

        original_database = real_run_readiness.database
        real_run_readiness.database = FailingDatabase()
        try:
            with self.assertRaisesRegex(RuntimeError, "evidence storage unavailable"):
                await real_run_readiness.load_real_run_evidence_batch(
                    [{"id": 1, "platform": "bilibili"}]
                )
        finally:
            real_run_readiness.database = original_database

    async def test_batch_ledger_query_failure_is_not_rendered_as_empty_history(self):
        class FailingLedgerDatabase:
            async def fetch_all(self, query, values=None):
                raise RuntimeError("ledger storage unavailable")

        original_database = lotteries.database
        lotteries.database = FailingLedgerDatabase()
        try:
            with self.assertRaisesRegex(RuntimeError, "ledger storage unavailable"):
                await lotteries.bilibili_action_ledgers_for_lotteries([1, 2], limit=12)
        finally:
            lotteries.database = original_database

    async def test_repair_and_ledger_batch_results_keep_per_lottery_shape(self):
        class RepairLedgerDatabase:
            async def fetch_all(self, query, values=None):
                if "SELECT bal.*" in query:
                    return [
                        {
                            "id": 20,
                            "lottery_id": 1,
                            "phase": "liked",
                            "ok": 1,
                            "evidence_rank": 1,
                        },
                        {
                            "id": 10,
                            "lottery_id": 2,
                            "phase": "followed",
                            "ok": 0,
                            "evidence_rank": 1,
                        },
                    ]
                if "FROM bilibili_action_ledger" in query:
                    return [{"lottery_id": 1, "phase": "liked"}]
                if "FROM events" in query:
                    return [{"lottery_id": 1, "phase": "commented"}]
                if "FROM task_phases" in query:
                    return [{"lottery_id": 2, "phase": "followed"}]
                raise AssertionError(f"Unexpected query: {query}")

        original_database = lotteries.database
        lotteries.database = RepairLedgerDatabase()
        try:
            completed = await lotteries.completed_real_run_actions_for_lotteries([1, 2])
            ledgers = await lotteries.bilibili_action_ledgers_for_lotteries([1, 2], limit=12)
        finally:
            lotteries.database = original_database

        self.assertEqual(completed[1], ["liked", "commented"])
        self.assertEqual(completed[2], ["followed"])
        self.assertEqual(ledgers[1][0]["ok"], True)
        self.assertEqual(ledgers[2][0]["ok"], False)
        self.assertNotIn("evidence_rank", ledgers[1][0])


if __name__ == "__main__":
    unittest.main()
