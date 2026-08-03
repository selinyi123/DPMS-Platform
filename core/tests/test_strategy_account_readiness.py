import asyncio
import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import (  # noqa: E402
    STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER,
    STRATEGY_ACCOUNT_RISK_HISTORY_BUDGET_BLOCKER,
    STRATEGY_ACCOUNT_ROWS_PER_PLATFORM,
    STRATEGY_ACCOUNT_TASK_HISTORY_BUDGET_BLOCKER,
    STRATEGY_BREAKER_QUERY_TIMEOUT_BLOCKER,
    STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM,
    STRATEGY_TARGET_METRICS_SQL,
    STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM,
    StrategyAccountRecommendations,
    compute_strategy_item,
    load_strategy_account_recommendations,
    load_strategy_breaker_statuses,
    strategy_queue,
)
from app.platform_modules import PlatformModuleUnavailableError  # noqa: E402
from app.services.real_run_readiness import (  # noqa: E402
    AccountScopedReadinessCandidatePrefilter,
    evaluate_account_scoped_real_run_readiness_batch,
)
from app.strategy.engine import empty_platform_knowledge  # noqa: E402


class StrategyAccountReadinessTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_account_count_excludes_active_operation_leases(self):
        normalized = " ".join(STRATEGY_TARGET_METRICS_SQL.split())
        self.assertIn(
            "FROM account_operation_leases lease",
            normalized,
        )
        self.assertIn("lease.account_id = a.id", normalized)
        self.assertIn("lease.released_at IS NULL", normalized)
        self.assertIn("lease.expires_at > NOW()", normalized)

    def lottery(self, **overrides):
        row = {
            "id": 1,
            "platform": "bilibili",
            "raw_url": "https://www.bilibili.com/opus/123456789",
            "canonical_url": (
                "https://www.bilibili.com/opus/123456789"
            ),
            "status": "pending",
            "value_score": 80,
            "safe_accounts": 2,
            "active_runs": 0,
            "dry_success": 1,
            "shadow_success": 1,
            "failed_runs": 0,
            "recent_platform_risk": 0,
            # A legacy selector summary must have no effect.
            "latest_probe_result": (
                '{"_summary":{"ready_for_real_actions":true}}'
            ),
        }
        row.update(overrides)
        return row

    def accounts(self):
        return {
            "bilibili": [
                {
                    "account_id": 4,
                    "platform": "bilibili",
                    "reputation_score": 90,
                },
                {
                    "account_id": 5,
                    "platform": "bilibili",
                    "reputation_score": 80,
                },
            ]
        }

    async def compute(self, row, account_readiness):
        with patch(
            "app.api.lotteries.circuit_breaker_allows",
            new=AsyncMock(return_value=(True, None)),
        ), patch(
            "app.api.lotteries.platform_has_runtime_real_adapter",
            return_value=True,
        ), patch(
            "app.api.lotteries.platform_real_adapter_kind",
            return_value="api",
        ):
            return await compute_strategy_item(
                row,
                selector_config={},
                real_run_enabled=True,
                platform_knowledge={
                    "bilibili": empty_platform_knowledge("bilibili")
                },
                account_recommendations=self.accounts(),
                account_readiness=account_readiness,
            )

    async def test_selector_probe_cannot_authorize_real_run(self):
        result = await self.compute(
            self.lottery(),
            {
                "account_id": 4,
                "readiness": {
                    "allowed": False,
                    "probe_ready": False,
                    "blockers": ["exact_execution_evidence_required"],
                },
            },
        )

        self.assertNotEqual(result["recommended_mode"], "real_run")
        self.assertFalse(result["execution_readiness_ready"])
        self.assertEqual(
            result["execution_readiness_blockers"],
            ["exact_execution_evidence_required"],
        )

    async def test_exact_account_readiness_authorizes_selected_account(self):
        result = await self.compute(
            self.lottery(dry_success=0, shadow_success=0),
            {
                "account_id": 5,
                "readiness": {
                    "allowed": True,
                    "probe_ready": True,
                    "blockers": [],
                    "execution_evidence_bound": True,
                    "execution_evidence_id": "evidence-5",
                    "execution_path_id": "bilibili_api_v2",
                },
            },
        )

        self.assertEqual(result["recommended_mode"], "real_run")
        self.assertTrue(result["execution_readiness_ready"])
        self.assertEqual(result["execution_readiness_blockers"], [])
        self.assertEqual(result["recommended_account"]["account_id"], 5)
        self.assertEqual(
            result["execution_readiness"]["execution_evidence_id"],
            "evidence-5",
        )

    async def test_breaker_reads_are_bounded_by_platform_not_target(self):
        checker = AsyncMock(return_value=(True, None))
        with patch(
            "app.api.lotteries.circuit_breaker_allows",
            new=checker,
        ):
            statuses = await load_strategy_breaker_statuses(
                ["bilibili"] * 100 + ["weibo"] * 100
            )

        self.assertEqual(
            statuses,
            {"bilibili": (True, None), "weibo": (True, None)},
        )
        self.assertEqual(checker.await_count, 2)

    async def test_breaker_timeout_is_platform_local_and_fail_closed(self):
        async def checker(platform):
            if platform == "bilibili":
                await asyncio.sleep(0.05)
            return True, None

        with patch(
            "app.api.lotteries.circuit_breaker_allows",
            new=AsyncMock(side_effect=checker),
        ), patch(
            "app.api.lotteries.STRATEGY_QUERY_TIMEOUT_SECONDS",
            0.01,
        ):
            statuses = await load_strategy_breaker_statuses(
                ["bilibili", "weibo"]
            )

        self.assertEqual(
            statuses["bilibili"],
            (False, STRATEGY_BREAKER_QUERY_TIMEOUT_BLOCKER),
        )
        self.assertEqual(statuses["weibo"], (True, None))

    async def test_account_recommendations_use_latest_same_platform_calibration(
        self,
    ):
        fetch_all = AsyncMock(return_value=[])
        with patch(
            "app.api.lotteries.database.fetch_all",
            new=fetch_all,
        ):
            recommendations = await load_strategy_account_recommendations(
                exclude_active_leases=True,
            )

        self.assertEqual(recommendations, {})
        query = fetch_all.await_args.args[0]
        self.assertIn("a.deleted_at IS NULL", query)
        self.assertIn("candidate.platform = a.platform", query)
        self.assertIn("latest_calibration.status = 'succeeded'", query)
        self.assertIn(
            "FROM account_operation_leases lease",
            query,
        )
        self.assertIn("lease.account_id = a.id", query)
        self.assertIn("lease.released_at IS NULL", query)
        self.assertIn("lease.expires_at > NOW()", query)
        self.assertEqual(query.count("SELECT candidate.id"), 1)
        self.assertNotIn("SELECT c.status", query)

    async def test_advisory_recommendations_remain_lease_neutral(self):
        fetch_all = AsyncMock(return_value=[])
        with patch(
            "app.api.lotteries.database.fetch_all",
            new=fetch_all,
        ):
            await load_strategy_account_recommendations()

        query = fetch_all.await_args.args[0]
        self.assertNotIn("account_operation_leases", query)

    async def test_account_recommendation_history_reads_are_hard_bounded(
        self,
    ):
        queries = []

        async def fetch_all(query, values=None):
            queries.append((query, dict(values or {})))
            if "FROM accounts a" in query:
                return [
                    {
                        "id": 4,
                        "platform": "bilibili",
                        "status": "ready",
                        "risk_score": 0,
                        "daily_task_count": 1,
                        "last_active_at": None,
                        "latest_calibration_status": "succeeded",
                    }
                ]
            if "FROM task_runs" in query:
                return [
                    {
                        "id": 10,
                        "account_id": 4,
                        "status": "succeeded",
                        "dry_run": 1,
                        "task_mode": "dry_run",
                        "created_at": "2026-07-24 10:00:00",
                    }
                ]
            if "FROM risk_events" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ):
            recommendations = await load_strategy_account_recommendations(
                platform="bilibili",
            )

        self.assertEqual(
            recommendations["bilibili"][0]["account_id"],
            4,
        )
        self.assertEqual(
            recommendations["bilibili"][0]["dry_runs"],
            1,
        )
        self.assertEqual(len(queries), 3)
        account_query, account_values = queries[0]
        task_query, task_values = queries[1]
        risk_query, risk_values = queries[2]
        self.assertIn("LIMIT :strategy_account_limit", account_query)
        self.assertEqual(
            account_values["strategy_account_limit"],
            STRATEGY_ACCOUNT_ROWS_PER_PLATFORM,
        )
        self.assertIn("LIMIT :strategy_task_history_limit", task_query)
        self.assertEqual(
            task_values["strategy_task_history_limit"],
            STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM + 1,
        )
        self.assertNotIn("strategy_risk_history_limit", task_values)
        self.assertIn("LIMIT :strategy_risk_history_limit", risk_query)
        self.assertEqual(
            risk_values["strategy_risk_history_limit"],
            STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM + 1,
        )
        self.assertNotIn("strategy_task_history_limit", risk_values)

    async def test_task_history_overflow_omits_only_that_platform(self):
        task_rows = [
            {
                "id": index + 1,
                "account_id": 4,
                "status": "succeeded",
                "dry_run": 0,
                "task_mode": "real_run",
                "created_at": "2026-07-24 10:00:00",
            }
            for index in range(STRATEGY_TASK_HISTORY_ROWS_PER_PLATFORM + 1)
        ]
        seen_risk_query = False

        async def fetch_all(query, values=None):
            nonlocal seen_risk_query
            platform = str((values or {}).get("strategy_platform") or "")
            if "FROM accounts a" in query:
                if platform == "bilibili":
                    return [
                        {
                            "id": 4,
                            "platform": "bilibili",
                            "status": "ready",
                            "risk_score": 0,
                            "daily_task_count": 0,
                            "last_active_at": None,
                            "latest_calibration_status": "succeeded",
                        }
                    ]
                return []
            if "FROM task_runs" in query:
                return task_rows
            if "FROM risk_events" in query:
                seen_risk_query = True
                return []
            raise AssertionError(f"unexpected query: {query}")

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ):
            recommendations = await load_strategy_account_recommendations()

        self.assertEqual(recommendations, {})
        self.assertEqual(
            recommendations.blockers_by_platform,
            {
                "bilibili": (
                    STRATEGY_ACCOUNT_TASK_HISTORY_BUDGET_BLOCKER
                )
            },
        )
        self.assertFalse(seen_risk_query)

    async def test_risk_history_overflow_cannot_upgrade_recommendation(self):
        risk_rows = [
            {
                "id": index + 1,
                "account_id": 4,
                "created_at": "2026-07-24 10:00:00",
            }
            for index in range(STRATEGY_RISK_HISTORY_ROWS_PER_PLATFORM + 1)
        ]

        async def fetch_all(query, _values=None):
            if "FROM accounts a" in query:
                return [
                    {
                        "id": 4,
                        "platform": "bilibili",
                        "status": "ready",
                        "risk_score": 0,
                        "daily_task_count": 0,
                        "last_active_at": None,
                        "latest_calibration_status": "succeeded",
                    }
                ]
            if "FROM task_runs" in query:
                return []
            if "FROM risk_events" in query:
                return risk_rows
            raise AssertionError(f"unexpected query: {query}")

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ):
            recommendations = await load_strategy_account_recommendations(
                platform="bilibili",
            )

        self.assertEqual(recommendations, {})
        self.assertEqual(
            recommendations.blockers_by_platform,
            {
                "bilibili": (
                    STRATEGY_ACCOUNT_RISK_HISTORY_BUDGET_BLOCKER
                )
            },
        )

    async def test_recommendation_timeout_is_reported_not_no_account(self):
        async def fetch_all(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ), patch(
            "app.api.lotteries.STRATEGY_QUERY_TIMEOUT_SECONDS",
            0.01,
        ):
            recommendations = await load_strategy_account_recommendations(
                platform="bilibili",
            )

        self.assertEqual(recommendations, {})
        self.assertEqual(
            recommendations.blockers_by_platform,
            {"bilibili": STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER},
        )

    async def test_recommendation_blocker_reaches_account_readiness(self):
        result = await evaluate_account_scoped_real_run_readiness_batch(
            [self.lottery()],
            account_candidates={},
            candidate_prefilter=AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={1: frozenset()}
            ),
            recommendation_blockers_by_platform={
                "bilibili": STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER
            },
        )

        self.assertIsNone(result[1]["account_id"])
        self.assertEqual(
            result[1]["readiness"]["blockers"],
            [STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER],
        )

    async def test_strategy_queue_target_query_timeout_is_fail_closed(self):
        async def slow_fetch_all(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

        with patch(
            "app.api.lotteries.load_runtime_selector_config",
            new=AsyncMock(return_value={}),
        ), patch(
            "app.api.lotteries.is_real_run_enabled",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.api.lotteries.load_strategy_platform_knowledge",
            new=AsyncMock(return_value={}),
        ), patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=slow_fetch_all),
        ), patch(
            "app.api.lotteries.STRATEGY_QUERY_TIMEOUT_SECONDS",
            0.001,
        ):
            with self.assertRaises(HTTPException) as raised:
                await strategy_queue()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": "strategy_queue_query_timeout"},
        )

    async def test_strategy_queue_has_request_wide_deadline(self):
        async def slow_selector():
            await asyncio.sleep(0.05)
            return {}

        with patch(
            "app.api.lotteries.load_runtime_selector_config",
            new=AsyncMock(side_effect=slow_selector),
        ), patch(
            "app.api.lotteries.STRATEGY_EVALUATION_TIMEOUT_SECONDS",
            0.01,
        ):
            with self.assertRaises(HTTPException) as raised:
                await strategy_queue()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": "strategy_evaluation_timeout"},
        )

    async def test_slow_account_history_lane_does_not_poison_sibling(self):
        async def fetch_all(query, values=None):
            platform = str((values or {}).get("strategy_platform") or "")
            if "FROM accounts a" in query:
                if platform == "bilibili":
                    await asyncio.sleep(0.05)
                    return []
                if platform == "weibo":
                    return [
                        {
                            "id": 5,
                            "platform": "weibo",
                            "status": "ready",
                            "risk_score": 0,
                            "daily_task_count": 0,
                            "last_active_at": None,
                            "latest_calibration_status": "succeeded",
                        }
                    ]
                return []
            if "FROM task_runs" in query or "FROM risk_events" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ), patch(
            "app.api.lotteries.STRATEGY_QUERY_TIMEOUT_SECONDS",
            0.01,
        ):
            recommendations = await load_strategy_account_recommendations()

        self.assertNotIn("bilibili", recommendations)
        self.assertEqual(
            recommendations["weibo"][0]["account_id"],
            5,
        )

    async def test_strategy_queue_passes_authoritative_prefilter_to_readiness(
        self,
    ):
        rows = [
            {
                "id": 1,
                "platform": "bilibili",
            }
        ]
        candidate_prefilter = object()
        recommendations = StrategyAccountRecommendations(
            self.accounts(),
            blockers_by_platform={
                "weibo": STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER
            },
        )
        readiness = {
            1: {
                "account_id": 4,
                "readiness": {"allowed": True, "blockers": []},
            }
        }
        evaluate = AsyncMock(return_value=readiness)
        with patch(
            "app.api.lotteries.load_runtime_selector_config",
            new=AsyncMock(return_value={}),
        ), patch(
            "app.api.lotteries.is_real_run_enabled",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.api.lotteries.load_strategy_platform_knowledge",
            new=AsyncMock(return_value={}),
        ), patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(return_value=rows),
        ), patch(
            "app.api.lotteries.load_account_scoped_readiness_candidate_prefilter",
            new=AsyncMock(return_value=candidate_prefilter),
        ) as load_prefilter, patch(
            "app.api.lotteries.load_strategy_account_recommendations",
            new=AsyncMock(return_value=recommendations),
        ), patch(
            "app.api.lotteries.load_strategy_breaker_statuses",
            new=AsyncMock(return_value={"bilibili": (True, None)}),
        ), patch(
            "app.api.lotteries.evaluate_account_scoped_real_run_readiness_batch",
            new=evaluate,
        ), patch(
            "app.api.lotteries.compute_strategy_item",
            new=AsyncMock(
                return_value={"strategy_score": 1, "lottery_id": 1}
            ),
        ):
            result = await strategy_queue()

        self.assertEqual(result["count"], 1)
        load_prefilter.assert_awaited_once_with(rows)
        self.assertIs(
            evaluate.await_args.kwargs["candidate_prefilter"],
            candidate_prefilter,
        )
        self.assertIs(
            evaluate.await_args.kwargs["account_candidates"],
            recommendations,
        )
        self.assertEqual(
            evaluate.await_args.kwargs[
                "recommendation_blockers_by_platform"
            ],
            {"weibo": STRATEGY_ACCOUNT_QUERY_TIMEOUT_BLOCKER},
        )

    async def test_unavailable_module_blocks_only_its_strategy_item(self):
        with patch(
            "app.api.lotteries.get_platform_module",
            side_effect=PlatformModuleUnavailableError("bilibili"),
        ), patch(
            "app.api.lotteries.circuit_breaker_allows",
            new=AsyncMock(return_value=(True, None)),
        ):
            result = await compute_strategy_item(
                self.lottery(),
                selector_config={},
                real_run_enabled=True,
                platform_knowledge={
                    "bilibili": empty_platform_knowledge("bilibili")
                },
                account_recommendations=self.accounts(),
                account_readiness={
                    "account_id": 4,
                    "readiness": {
                        "allowed": False,
                        "blockers": ["platform_module_unavailable"],
                    },
                },
            )

        self.assertEqual(result["recommended_mode"], "blocked")
        self.assertEqual(result["blockers"], ["platform_module_unavailable"])


if __name__ == "__main__":
    unittest.main()
