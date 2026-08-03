import asyncio
import base64
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import (  # noqa: E402
    BILIBILI_API_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_config_hash,
    compute_target_hash,
    weibo_runtime_capability_requirements,
)
from app.api import lotteries  # noqa: E402
from app.platform_modules import PlatformModuleUnavailableError  # noqa: E402
from app.services import real_run_readiness  # noqa: E402
from app.services.execution_intents import (  # noqa: E402
    ExecutionIntentLoadFailure,
)


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
            rows = [
                {"id": 4, "platform": "bilibili"},
                {"id": 5, "platform": "bilibili"},
                {"id": 6, "platform": "weibo"},
            ]
            platform = str((values or {}).get("risk_summary_platform") or "")
            return [row for row in rows if row["platform"] == platform]
        if "FROM account_active_risk_states active_risk" in query:
            rows = [
                {
                    "id": 10,
                    "account_id": 5,
                    "event_type": "action_window",
                    "detail": json.dumps({"reason": "action_window"}),
                    "created_at": "2026-07-14 10:00:00",
                }
            ]
            scoped_ids = {
                int(value)
                for key, value in (values or {}).items()
                if key.startswith("risk_summary_account_")
            }
            return [row for row in rows if row["account_id"] in scoped_ids]
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
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
        if "FROM external_action_intents" in query:
            return []
        if "FROM events" in query:
            return []
        if "FROM task_phases" in query:
            return []
        if "FROM lottery_execution_intent_heads AS head" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": datetime(2026, 7, 14, 11, 0, 0)}
        raise AssertionError(f"Unexpected query: {query}")


class AccountScopedReadinessDatabase:
    def __init__(self):
        self.fetch_all_queries = []
        self.fetch_all_values = []
        self.fetch_one_queries = []

    async def fetch_all(self, query, values=None):
        self.fetch_all_queries.append(query)
        self.fetch_all_values.append(dict(values or {}))
        if "FROM lottery_rule_snapshots" in query:
            return []
        if "FROM accounts a" in query:
            return []
        if "FROM account_active_risk_states active_risk" in query:
            return []
        if "FROM execution_evidence_bindings" in query:
            return []
        if "FROM task_runs tr" in query:
            return []
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        self.fetch_one_queries.append(query)
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": datetime(2026, 7, 23, 10, 0, 0)}
        raise AssertionError(f"Unexpected query: {query}")


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FinalMutableStateConnection:
    def __init__(self):
        self.transaction_calls = 0
        self.queries = []
        self.now = datetime(2026, 7, 24, 12, 0, 0)

    def transaction(self):
        self.transaction_calls += 1
        return _AsyncContext(self)

    async def fetch_one(self, query, values=None):
        self.queries.append(query)
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": self.now}
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if "FROM accounts a" in query:
            return [
                {
                    "id": 4,
                    "platform": "bilibili",
                    "status": "ready",
                    "execution_revision": 2,
                    "credential_size": 16,
                }
            ]
        if "FROM account_active_risk_states active_risk" in query:
            return [
                {
                    "id": 91,
                    "account_id": 4,
                    "event_type": "hard_signal",
                    "detail": json.dumps(
                        {"reason": "page_risk_signal"}
                    ),
                    "created_at": self.now,
                }
            ]
        raise AssertionError(f"Unexpected query: {query}")


class FinalMutableStateDatabase:
    def __init__(self):
        self.connection_calls = 0
        self.connection_value = FinalMutableStateConnection()

    def connection(self):
        self.connection_calls += 1
        return _AsyncContext(self.connection_value)

    async def fetch_one(self, *_args, **_kwargs):
        raise AssertionError("query escaped the explicit connection")

    async def fetch_all(self, *_args, **_kwargs):
        raise AssertionError("query escaped the explicit connection")


def exact_binding_action_plan(
    platform,
    *,
    rule_snapshot_id,
    rule_hash,
):
    execution_path_id = (
        BILIBILI_API_EXECUTION_PATH
        if platform == "bilibili"
        else WEIBO_OAUTH_EXECUTION_PATH
    )
    required_actions = ["liked"]
    empty_requirements = {
        "follow_targets": [],
        "commented": {"topic_tags": [], "mentions": []},
        "reposted": {"topic_tags": [], "mentions": []},
    }
    plan = {
        "version": 2,
        "platform": platform,
        "is_lottery": True,
        "required_actions": required_actions,
        "action_payloads": {"liked": {}},
        "source_content_requirements": empty_requirements,
        "content_requirements": empty_requirements,
        "friend_mention_requirements": {},
        "runtime_capability_requirements": (
            weibo_runtime_capability_requirements(required_actions)
            if platform == "weibo"
            else {}
        ),
        "execution_path_id": execution_path_id,
        "rule_snapshot_id": rule_snapshot_id,
        "rule_hash": rule_hash,
        "review_required": False,
        "executable": True,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def bilibili_exact_loader_fixture(
    *,
    lottery_id: int,
    account_id: int = 7,
    rule_snapshot_id: int = 101,
    execution_revision: int = 9,
):
    rule_hash = "a" * 64
    canonical_url = (
        "https://www.bilibili.com/opus/1220306071196794898"
    )
    plan = exact_binding_action_plan(
        "bilibili",
        rule_snapshot_id=rule_snapshot_id,
        rule_hash=rule_hash,
    )
    binding = {
        "lottery_id": lottery_id,
        "account_id": account_id,
        "rule_snapshot_id": rule_snapshot_id,
        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
        "target_hash": compute_target_hash(canonical_url),
        "rule_hash": rule_hash,
        "action_plan_hash": plan["plan_hash"],
        "config_hash": compute_bilibili_api_config_hash(
            execution_revision
        ),
    }
    lottery = {
        "id": lottery_id,
        "platform": "bilibili",
        "raw_url": canonical_url,
        "canonical_url": canonical_url,
        "action_plan": json.dumps(plan),
    }
    account = {
        "id": account_id,
        "platform": "bilibili",
        "status": "ready",
        "execution_revision": execution_revision,
        "credential_size": 64,
    }
    return lottery, account, binding


def weibo_exact_loader_fixture(
    *,
    lottery_id: int,
    account_id: int = 8,
    rule_snapshot_id: int = 102,
    execution_revision: int = 10,
):
    rule_hash = "b" * 64
    canonical_url = "https://weibo.com/1234567890/Nabcde"
    plan = exact_binding_action_plan(
        "weibo",
        rule_snapshot_id=rule_snapshot_id,
        rule_hash=rule_hash,
    )
    binding = {
        "lottery_id": lottery_id,
        "account_id": account_id,
        "rule_snapshot_id": rule_snapshot_id,
        "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
        "target_hash": compute_target_hash(canonical_url),
        "rule_hash": rule_hash,
        "action_plan_hash": plan["plan_hash"],
        "config_hash": compute_config_hash(
            {
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "execution_revision": execution_revision,
                "runtime_capability_requirements": (
                    plan["runtime_capability_requirements"]
                ),
                "weibo_rip_hash": "",
            }
        ),
    }
    lottery = {
        "id": lottery_id,
        "platform": "weibo",
        "canonical_url": canonical_url,
        "action_plan": json.dumps(plan),
    }
    account = {
        "id": account_id,
        "platform": "weibo",
        "status": "ready",
        "execution_revision": execution_revision,
        "encrypted_credential": "encrypted",
        "credential_size": 64,
    }
    return lottery, account, binding


class ExactBindingReadinessDatabase:
    def __init__(self, *, platform, account, evidence_rows):
        self.platform = platform
        self.account = account
        self.evidence_rows = evidence_rows
        self.binding_query = ""
        self.bindings = []
        self.evidence_page_count = 0
        self.served_evidence_ids = []

    async def fetch_all(self, query, values=None):
        values = dict(values or {})
        if "FROM lottery_rule_snapshots" in query:
            return []
        if "FROM accounts a" in query:
            return [self.account]
        if "FROM account_active_risk_states active_risk" in query:
            return []
        if (
            "FROM execution_evidence_bindings" in query
            or "FROM task_runs tr" in query
        ):
            self.binding_query = query
            self.evidence_page_count += 1
            key = f"readiness_{self.platform}_exact_bindings"
            self.bindings = json.loads(values[key])
            if self.platform == "bilibili":
                timestamp_field = "verified_at"
                cursor_timestamp = values[
                    "readiness_evidence_cursor_verified_at"
                ]
                cursor_id = values["readiness_evidence_cursor_id"]
                page_limit = values["readiness_evidence_page_limit"]
            else:
                timestamp_field = "finished_at"
                cursor_timestamp = values[
                    "readiness_dry_run_cursor_finished_at"
                ]
                cursor_id = values["readiness_dry_run_cursor_id"]
                page_limit = values["readiness_dry_run_page_limit"]
            rows = sorted(
                self.evidence_rows,
                key=lambda row: (
                    row[timestamp_field],
                    int(row["id"]),
                ),
                reverse=True,
            )
            if cursor_timestamp is not None:
                cursor = (cursor_timestamp, int(cursor_id))
                rows = [
                    row
                    for row in rows
                    if (
                        row[timestamp_field],
                        int(row["id"]),
                    )
                    < cursor
                ]
            page = rows[:page_limit]
            self.served_evidence_ids.extend(row["id"] for row in page)
            return page
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": datetime(2026, 7, 23, 10, 0, 0)}
        raise AssertionError(f"Unexpected query: {query}")


class RealRunEvidenceBatchTests(unittest.IsolatedAsyncioTestCase):
    def candidate_prefilter(self, mapping, *, failed_platforms=()):
        return (
            real_run_readiness.AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={
                    int(lottery_id): frozenset(account_ids)
                    for lottery_id, account_ids in mapping.items()
                },
                failed_platforms=frozenset(failed_platforms),
            )
        )

    def test_readiness_phase_budgets_fit_ten_second_envelope(self):
        self.assertLessEqual(
            (
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS
                + real_run_readiness
                .ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS
                + real_run_readiness
                .ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_SECONDS
            ),
            (
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_PHASE_BUDGET_SECONDS
            ),
        )

    def test_batched_evidence_helpers_recheck_final_cutoff(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        final_at = loaded_at + timedelta(seconds=10)
        bilibili_row = {
            "verified_at": loaded_at - timedelta(minutes=1),
            "expires_at": loaded_at + timedelta(seconds=5),
            "probe_finished_at": loaded_at - timedelta(minutes=2),
            "shadow_finished_at": loaded_at - timedelta(minutes=1),
        }
        weibo_dry_run = {
            "finished_at": loaded_at - timedelta(
                hours=23,
                minutes=59,
                seconds=55,
            )
        }

        self.assertTrue(
            real_run_readiness._batched_bilibili_evidence_fresh_at(
                bilibili_row,
                cutoff=loaded_at,
            )
        )
        self.assertFalse(
            real_run_readiness._batched_bilibili_evidence_fresh_at(
                bilibili_row,
                cutoff=final_at,
            )
        )
        self.assertTrue(
            real_run_readiness._batched_weibo_dry_run_fresh_at(
                weibo_dry_run,
                cutoff=loaded_at,
            )
        )
        self.assertFalse(
            real_run_readiness._batched_weibo_dry_run_fresh_at(
                weibo_dry_run,
                cutoff=final_at,
            )
        )

    def test_aware_evidence_timestamps_are_normalized_to_utc(self):
        observed = real_run_readiness.normalize_datetime(
            datetime.fromisoformat("2026-07-24T12:00:00+08:00")
        )

        self.assertEqual(observed, datetime(2026, 7, 24, 4, 0, 0))
        self.assertEqual(
            real_run_readiness.normalize_datetime(
                datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)
            ),
            observed,
        )

    async def test_final_mutable_snapshot_stays_on_one_explicit_connection(
        self,
    ):
        fake = FinalMutableStateDatabase()
        original_database = real_run_readiness.database
        real_run_readiness.database = fake
        try:
            db_now, accounts, risks = await (
                real_run_readiness
                ._load_final_account_mutable_state_snapshot({4})
            )
        finally:
            real_run_readiness.database = original_database

        self.assertEqual(db_now, fake.connection_value.now)
        self.assertEqual(set(accounts), {4})
        self.assertTrue(risks[4]["has_recent_risk"])
        self.assertEqual(fake.connection_calls, 1)
        self.assertEqual(fake.connection_value.transaction_calls, 1)
        self.assertEqual(len(fake.connection_value.queries), 3)

    def test_batched_evidence_requires_full_response_safety_margin(self):
        observed_at = datetime(2026, 7, 24, 12, 0, 0)
        margin = timedelta(
            seconds=(
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_RESPONSE_SAFETY_MARGIN_SECONDS
            )
        )
        required_valid_at = observed_at + margin
        row = {
            "verified_at": observed_at - timedelta(minutes=1),
            "expires_at": required_valid_at - timedelta(microseconds=1),
            "probe_finished_at": observed_at - timedelta(minutes=2),
            "shadow_finished_at": observed_at - timedelta(minutes=1),
        }
        oauth_verified_at = (
            observed_at - timedelta(hours=24) + timedelta(seconds=2)
        )
        weibo_account = {
            "calibration_created_at": observed_at - timedelta(hours=1),
            "calibration_finished_at": observed_at - timedelta(minutes=59),
            "calibration_result": json.dumps(
                {
                    "oauth_capabilities": {
                        "verified_at": (
                            oauth_verified_at.isoformat() + "Z"
                        ),
                        "attested_at": (
                            oauth_verified_at.isoformat() + "Z"
                        ),
                    }
                }
            ),
        }

        self.assertTrue(
            real_run_readiness._batched_bilibili_evidence_fresh_at(
                row,
                cutoff=observed_at,
            )
        )
        self.assertFalse(
            real_run_readiness._batched_bilibili_evidence_fresh_at(
                row,
                cutoff=required_valid_at,
            )
        )
        self.assertTrue(
            real_run_readiness._batched_weibo_calibration_fresh_at(
                weibo_account,
                cutoff=observed_at,
            )
        )
        self.assertFalse(
            real_run_readiness._batched_weibo_calibration_fresh_at(
                weibo_account,
                cutoff=required_valid_at,
            )
        )

    async def test_allowed_candidate_is_revalidated_at_response_cutoff(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        final_at = loaded_at
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
            freshness_snapshot_at=loaded_at,
            freshness_cutoff_at=loaded_at,
        )

        async def readiness(_lottery, *, evidence_batch, **_kwargs):
            fresh = evidence_batch.freshness_cutoff_at <= loaded_at
            return {
                "allowed": fresh,
                "blockers": [] if fresh else ["exact_execution_evidence_required"],
                "execution_evidence_bound": fresh,
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ) as validate, patch(
            (
                "app.services.real_run_readiness."
                "_load_final_account_mutable_state_snapshot"
            ),
            new=AsyncMock(
                return_value=(
                    final_at,
                    {},
                    {4: real_run_readiness.account_risk_payload(None)},
                )
            ),
        ):
            results = await (
                real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter({1: {4}}),
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["exact_execution_evidence_required"],
        )
        self.assertEqual(validate.await_count, 2)

    async def test_invalid_db_clock_fails_final_freshness_recheck_closed(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
            freshness_snapshot_at=loaded_at,
            freshness_cutoff_at=loaded_at,
        )
        async def readiness(_lottery, **_kwargs):
            return {
                "allowed": True,
                "blockers": [],
                "execution_evidence_bound": True,
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ), patch(
            (
                "app.services.real_run_readiness."
                "_load_final_account_mutable_state_snapshot"
            ),
            new=AsyncMock(
                side_effect=RuntimeError(
                    "database clock response is missing or invalid"
                )
            ),
        ):
            results = await (
                real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter({1: {4}}),
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_FAILED_BLOCKER
            ],
        )
        self.assertFalse(
            results[1]["readiness"]["execution_evidence_bound"]
        )

    async def test_final_refresh_observes_a_new_active_account_risk(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        account = {
            "id": 4,
            "platform": "bilibili",
            "status": "ready",
            "execution_revision": 2,
            "credential_size": 16,
        }
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
            accounts={4: account},
            account_risks={
                4: real_run_readiness.account_risk_payload(None)
            },
            freshness_snapshot_at=loaded_at,
            freshness_cutoff_at=loaded_at,
        )
        new_risk = real_run_readiness.account_risk_payload(
            {
                "id": 91,
                "account_id": 4,
                "event_type": "hard_signal",
                "detail": {"reason": "page_risk_signal"},
                "created_at": loaded_at,
            }
        )

        async def readiness(_lottery, *, evidence_batch, **_kwargs):
            risk = evidence_batch.account_risks[4]
            allowed = not risk["has_recent_risk"]
            return {
                "allowed": allowed,
                "blockers": (
                    [] if allowed else ["recent_account_risk_event"]
                ),
                "execution_evidence_bound": allowed,
            }

        with (
            patch(
                (
                    "app.services.real_run_readiness."
                    "load_account_scoped_real_run_readiness_batch"
                ),
                new=AsyncMock(return_value=batch),
            ),
            patch(
                "app.services.real_run_readiness.validate_real_run_evidence",
                new=AsyncMock(side_effect=readiness),
            ),
            patch(
                (
                    "app.services.real_run_readiness."
                    "_load_final_account_mutable_state_snapshot"
                ),
                new=AsyncMock(
                    return_value=(
                        loaded_at + timedelta(seconds=1),
                        {4: account},
                        {4: new_risk},
                    )
                ),
            ) as refresh,
        ):
            results = await (
                real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter({1: {4}}),
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["recent_account_risk_event"],
        )
        refresh.assert_awaited_once_with({4})

    async def test_final_refresh_removes_newly_leased_or_deleted_account(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        account = {
            "id": 4,
            "platform": "bilibili",
            "status": "ready",
            "execution_revision": 2,
            "credential_size": 16,
        }
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
            accounts={4: account},
            account_risks={
                4: real_run_readiness.account_risk_payload(None)
            },
            freshness_snapshot_at=loaded_at,
            freshness_cutoff_at=loaded_at,
        )

        async def readiness(_lottery, *, evidence_batch, **_kwargs):
            allowed = 4 in evidence_batch.accounts
            return {
                "allowed": allowed,
                "blockers": (
                    [] if allowed else ["execution_account_not_ready"]
                ),
                "execution_evidence_bound": allowed,
            }

        with (
            patch(
                (
                    "app.services.real_run_readiness."
                    "load_account_scoped_real_run_readiness_batch"
                ),
                new=AsyncMock(return_value=batch),
            ),
            patch(
                "app.services.real_run_readiness.validate_real_run_evidence",
                new=AsyncMock(side_effect=readiness),
            ),
            patch(
                (
                    "app.services.real_run_readiness."
                    "_load_final_account_mutable_state_snapshot"
                ),
                new=AsyncMock(
                    return_value=(
                        loaded_at + timedelta(seconds=1),
                        {},
                        {
                            4: (
                                real_run_readiness
                                .account_risk_payload(None)
                            )
                        },
                    )
                ),
            ),
        ):
            results = await (
                real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter({1: {4}}),
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["execution_account_not_ready"],
        )

    async def test_final_freshness_timeout_is_scoped_to_one_platform(self):
        loaded_at = datetime(2026, 7, 24, 12, 0, 0)
        accounts = {
            4: {
                "id": 4,
                "platform": "bilibili",
                "status": "ready",
                "execution_revision": 2,
                "credential_size": 16,
            },
            5: {
                "id": 5,
                "platform": "weibo",
                "status": "ready",
                "execution_revision": 3,
                "credential_size": 16,
            },
        }
        batches = {
            platform: real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset({account_id}),
                account_scoped_readiness=True,
                accounts={account_id: accounts[account_id]},
                account_risks={
                    account_id: real_run_readiness.account_risk_payload(None)
                },
                freshness_snapshot_at=loaded_at,
                freshness_cutoff_at=loaded_at,
            )
            for platform, account_id in {
                "bilibili": 4,
                "weibo": 5,
            }.items()
        }
        evaluation_count = {"bilibili": 0, "weibo": 0}

        async def load_batch(lotteries, **_kwargs):
            return batches[lotteries[0]["platform"]]

        async def readiness(lottery, **_kwargs):
            platform = lottery["platform"]
            evaluation_count[platform] += 1
            if platform == "bilibili" and evaluation_count[platform] == 2:
                await asyncio.Event().wait()
            return {
                "allowed": True,
                "blockers": [],
                "execution_evidence_bound": True,
            }

        async def final_snapshot(account_ids):
            account_id = next(iter(account_ids))
            return (
                loaded_at + timedelta(seconds=1),
                {account_id: accounts[account_id]},
                {
                    account_id: (
                        real_run_readiness.account_risk_payload(None)
                    )
                },
            )

        with (
            patch(
                (
                    "app.services.real_run_readiness."
                    "load_account_scoped_real_run_readiness_batch"
                ),
                new=AsyncMock(side_effect=load_batch),
            ),
            patch(
                "app.services.real_run_readiness.validate_real_run_evidence",
                new=AsyncMock(side_effect=readiness),
            ),
            patch(
                (
                    "app.services.real_run_readiness."
                    "_load_final_account_mutable_state_snapshot"
                ),
                new=AsyncMock(side_effect=final_snapshot),
            ),
            patch.object(
                real_run_readiness,
                "ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_SECONDS",
                0.02,
            ),
        ):
            results = await (
                real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_FRESHNESS_RECHECK_TIMEOUT_BLOCKER
            ],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)
        self.assertEqual(evaluation_count, {"bilibili": 2, "weibo": 2})

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

        self.assertEqual(fake.fetch_all_calls, 4)
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
        # Frozen execution intents use one batch query. Missing roots now fail
        # closed before the four completion-evidence reads, and missing active
        # Redis repair consumers fail before DB heartbeat reads. Neither path
        # scales with the number of lotteries.
        self.assertEqual(counts[0], (5, 1))

    async def test_evidence_list_query_timeout_fails_before_downstream_reads(
        self,
    ):
        async def slow_fetch_all(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

        with patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(side_effect=slow_fetch_all),
        ), patch.object(
            lotteries,
            "REAL_RUN_EVIDENCE_LIST_QUERY_TIMEOUT_SECONDS",
            0.001,
        ), patch.object(
            lotteries,
            "load_real_run_evidence_batch",
            new=AsyncMock(),
        ) as load_batch:
            with self.assertRaises(HTTPException) as raised:
                await lotteries.list_real_run_evidence()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            {"code": "real_run_evidence_list_query_timeout"},
        )
        load_batch.assert_not_awaited()

    async def test_evidence_endpoint_contains_corrupt_intent_to_one_platform(
        self,
    ):
        class LotteryDatabase:
            async def fetch_all(self, query, values=None):
                if "SELECT * FROM lotteries" in query:
                    return [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ]
                raise AssertionError(f"Unexpected query: {query}")

        async def gate(lottery, **_kwargs):
            return {
                "lottery_id": int(lottery["id"]),
                "platform": str(lottery["platform"]),
            }

        authorities = {
            lottery_id: lotteries.RealRunCompletionAuthority(())
            for lottery_id in (1, 2)
        }
        with patch.object(
            lotteries,
            "database",
            LotteryDatabase(),
        ), patch.object(
            lotteries,
            "load_runtime_selector_config",
            new=AsyncMock(return_value={}),
        ), patch.object(
            lotteries,
            "is_real_run_enabled",
            new=AsyncMock(return_value=False),
        ), patch.object(
            lotteries,
            "load_real_run_evidence_batch",
            new=AsyncMock(return_value=object()),
        ), patch.object(
            lotteries,
            "real_run_account_risk_summaries",
            new=AsyncMock(
                return_value={"bilibili": {}, "weibo": {}}
            ),
        ), patch.object(
            lotteries,
            "load_real_run_completion_authorities_for_lotteries",
            new=AsyncMock(return_value=authorities),
        ), patch.object(
            lotteries,
            "load_lottery_execution_intents",
            new=AsyncMock(
                return_value={
                    1: ExecutionIntentLoadFailure(
                        lottery_id=1,
                        code="execution_intent_hash_mismatch",
                    )
                }
            ),
        ), patch.object(
            lotteries,
            "bilibili_action_ledgers_for_lotteries",
            new=AsyncMock(return_value={1: [], 2: []}),
        ), patch.object(
            lotteries,
            "real_run_gate_status",
            new=AsyncMock(side_effect=gate),
        ):
            response = await lotteries.list_real_run_evidence(limit=2)

        items = {
            int(item["lottery_id"]): item
            for item in response["items"]
        }
        self.assertEqual(
            items[1]["repair_plan"]["reason"],
            "execution_intent_invalid",
        )
        self.assertEqual(
            items[1]["repair_plan"]["integrity_blocker"],
            "execution_intent_hash_mismatch",
        )
        self.assertEqual(
            items[2]["repair_plan"]["reason"],
            "execution_intent_missing",
        )

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
                if "FROM external_action_intents" in query:
                    normalized = " ".join(query.split())
                    for predicate in ("tr.task_mode = 'real_run'",):
                        if predicate not in normalized:
                            raise AssertionError(
                                f"Missing authoritative predicate: {predicate}"
                            )
                    return [
                        {
                            "lottery_id": 2,
                            "intent_id": (
                                "00000000-0000-4000-8000-000000000031"
                            ),
                            "phase": "liked",
                            "status": "succeeded",
                            "effect_certainty": "confirmed_effect",
                            "outcome": "ok",
                        }
                    ]
                if "FROM events" in query:
                    return [{"lottery_id": 1, "phase": "commented"}]
                if "FROM task_phases" in query:
                    return [{"lottery_id": 2, "phase": "followed"}]
                raise AssertionError(f"Unexpected query: {query}")

        original_database = lotteries.database
        lotteries.database = RepairLedgerDatabase()
        try:
            completed = await lotteries.completed_real_run_actions_for_lotteries(
                {1: "bilibili", 2: "weibo"}
            )
            ledgers = await lotteries.bilibili_action_ledgers_for_lotteries([1, 2], limit=12)
        finally:
            lotteries.database = original_database

        self.assertEqual(completed[1], ["liked", "commented"])
        self.assertEqual(completed[2], ["followed", "liked"])
        self.assertEqual(ledgers[1][0]["ok"], True)
        self.assertEqual(ledgers[2][0]["ok"], False)
        self.assertNotIn("evidence_rank", ledgers[1][0])

    async def test_account_scoped_empty_page_query_count_is_lottery_bounded(
        self,
    ):
        counts = []
        original_database = real_run_readiness.database
        try:
            for lottery_count in (2, 100):
                fake = AccountScopedReadinessDatabase()
                real_run_readiness.database = fake
                rows = [
                    {
                        "id": index,
                        "platform": (
                            "bilibili" if index % 2 else "weibo"
                        ),
                        "authoritative_rule_snapshot_id": index,
                    }
                    for index in range(1, lottery_count + 1)
                ]
                batch = await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                    rows,
                    account_ids=[4, 5],
                )
                self.assertEqual(batch.account_ids, frozenset({4, 5}))
                account_query = next(
                    query
                    for query in fake.fetch_all_queries
                    if "FROM accounts a" in query
                )
                normalized_account_query = " ".join(
                    account_query.split()
                )
                self.assertIn(
                    "NOT EXISTS ( SELECT 1 FROM "
                    "account_operation_leases lease",
                    normalized_account_query,
                )
                self.assertIn(
                    "lease.account_id = a.id",
                    normalized_account_query,
                )
                self.assertIn(
                    "lease.released_at IS NULL",
                    normalized_account_query,
                )
                self.assertIn(
                    "lease.expires_at > NOW()",
                    normalized_account_query,
                )
                bounded_evidence_queries = [
                    query
                    for query in fake.fetch_all_queries
                    if (
                        "FROM execution_evidence_bindings" in query
                        or "FROM task_runs tr" in query
                    )
                ]
                self.assertEqual(len(bounded_evidence_queries), 2)
                self.assertTrue(
                    all(
                        "JOIN exact_bindings expected" in query
                        and "cursor_" in query
                        and "LIMIT :readiness_" in query
                        and "ROW_NUMBER() OVER" not in query
                        for query in bounded_evidence_queries
                    )
                )
                self.assertTrue(
                    all(
                        len(
                            [
                                key
                                for key in values
                                if key.startswith("readiness_account_")
                            ]
                        )
                        <= (
                            real_run_readiness
                            .ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
                        )
                        for values in fake.fetch_all_values
                    )
                )
                self.assertTrue(
                    all(
                        len(
                            [
                                key
                                for key in values
                                if "lottery_" in key
                            ]
                        )
                        <= (
                            real_run_readiness
                            .MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES
                        )
                        for values in fake.fetch_all_values
                    )
                )
                counts.append(
                    (
                        len(fake.fetch_all_queries),
                        len(fake.fetch_one_queries),
                    )
                )
        finally:
            real_run_readiness.database = original_database

        self.assertEqual(counts[0], counts[1])
        self.assertEqual(counts[0], (5, 1))

    async def test_bilibili_paging_reaches_ninth_exact_evidence_candidate(
        self,
    ):
        lottery_id = 11
        account_id = 7
        rule_snapshot_id = 101
        rule_hash = "a" * 64
        execution_revision = 9
        canonical_url = (
            "https://www.bilibili.com/opus/1220306071196794898"
        )
        plan = exact_binding_action_plan(
            "bilibili",
            rule_snapshot_id=rule_snapshot_id,
            rule_hash=rule_hash,
        )
        exact_binding = {
            "lottery_id": lottery_id,
            "account_id": account_id,
            "rule_snapshot_id": rule_snapshot_id,
            "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            "target_hash": compute_target_hash(canonical_url),
            "rule_hash": rule_hash,
            "action_plan_hash": plan["plan_hash"],
            "config_hash": compute_bilibili_api_config_hash(
                execution_revision
            ),
        }
        invalid_observation_rows = [
            {
                **exact_binding,
                "id": 100 - index,
                "probe_id": f"invalid-probe-{index}",
                "shadow_task_id": f"invalid-shadow-{index}",
                "verified_at": "2026-07-23 09:59:00",
            }
            for index in range(
                real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR
            )
        ]
        exact_row = {
            **exact_binding,
            "id": 1,
            "probe_id": "current-probe",
            "shadow_task_id": "current-shadow",
            "verified_at": "2026-07-23 09:00:00",
        }
        fake = ExactBindingReadinessDatabase(
            platform="bilibili",
            account={
                "id": account_id,
                "platform": "bilibili",
                "status": "ready",
                "execution_revision": execution_revision,
                "credential_size": 64,
            },
            evidence_rows=[*invalid_observation_rows, exact_row],
        )
        with patch.object(
            real_run_readiness,
            "database",
            fake,
        ), patch.object(
            real_run_readiness,
            "_exact_bilibili_evidence_observations_valid",
            side_effect=lambda row, **_kwargs: row["id"] == exact_row["id"],
        ):
            batch = (
                await real_run_readiness
                .load_account_scoped_real_run_readiness_batch(
                    [
                        {
                            "id": lottery_id,
                            "platform": "bilibili",
                            "raw_url": canonical_url,
                            "canonical_url": canonical_url,
                            "action_plan": json.dumps(plan),
                        }
                    ],
                    account_ids=[account_id],
                )
            )

        self.assertEqual(
            len(invalid_observation_rows),
            real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR,
        )
        self.assertIn("JOIN exact_bindings expected", fake.binding_query)
        self.assertIn(
            ":readiness_evidence_cursor_verified_at",
            fake.binding_query,
        )
        self.assertNotIn("ROW_NUMBER() OVER", fake.binding_query)
        self.assertEqual(fake.evidence_page_count, 2)
        self.assertEqual(fake.bindings, [exact_binding])
        self.assertEqual(
            [
                row["id"]
                for row in batch.bilibili_execution_evidence[
                    (lottery_id, account_id)
                ]
            ],
            [exact_row["id"]],
        )

    async def test_weibo_keyset_defensively_skips_nonmatching_first_page(
        self,
    ):
        lottery_id = 12
        account_id = 8
        rule_snapshot_id = 102
        rule_hash = "b" * 64
        execution_revision = 10
        canonical_url = "https://weibo.com/1234567890/Nabcde"
        plan = exact_binding_action_plan(
            "weibo",
            rule_snapshot_id=rule_snapshot_id,
            rule_hash=rule_hash,
        )
        exact_binding = {
            "lottery_id": lottery_id,
            "account_id": account_id,
            "rule_snapshot_id": rule_snapshot_id,
            "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
            "target_hash": compute_target_hash(canonical_url),
            "rule_hash": rule_hash,
            "action_plan_hash": plan["plan_hash"],
            "config_hash": compute_config_hash(
                {
                    "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                    "execution_revision": execution_revision,
                    "runtime_capability_requirements": (
                        plan["runtime_capability_requirements"]
                    ),
                    "weibo_rip_hash": "",
                }
            ),
        }
        stale_rows = [
            {
                **exact_binding,
                "id": 100 - index,
                "task_id": f"stale-dry-run-{index}",
                # JSON_TABLE excludes these in MySQL; the application-side
                # binding check remains defensive if an adapter/query
                # regression ever returns them.
                "action_plan_hash": "f" * 64,
                "finished_at": "2026-07-23 09:59:00",
            }
            for index in range(
                real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR
            )
        ]
        exact_row = {
            **exact_binding,
            "id": 1,
            "task_id": "current-dry-run",
            "finished_at": "2026-07-23 09:00:00",
        }
        fake = ExactBindingReadinessDatabase(
            platform="weibo",
            account={
                "id": account_id,
                "platform": "weibo",
                "status": "ready",
                "execution_revision": execution_revision,
                "encrypted_credential": "encrypted",
                "credential_size": 64,
            },
            evidence_rows=[*stale_rows, exact_row],
        )
        with patch.object(real_run_readiness, "database", fake):
            batch = (
                await real_run_readiness
                .load_account_scoped_real_run_readiness_batch(
                    [
                        {
                            "id": lottery_id,
                            "platform": "weibo",
                            "canonical_url": canonical_url,
                            "action_plan": json.dumps(plan),
                        }
                    ],
                    account_ids=[account_id],
                )
            )

        self.assertEqual(
            len(stale_rows),
            real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR,
        )
        self.assertIn("JOIN exact_bindings expected", fake.binding_query)
        self.assertIn(
            ":readiness_dry_run_cursor_finished_at",
            fake.binding_query,
        )
        self.assertNotIn("ROW_NUMBER() OVER", fake.binding_query)
        self.assertEqual(fake.evidence_page_count, 2)
        self.assertIn(exact_row["id"], fake.served_evidence_ids)
        self.assertEqual(fake.bindings, [exact_binding])
        self.assertEqual(
            [
                row["id"]
                for row in batch.weibo_oauth_dry_runs[
                    (lottery_id, account_id)
                ]
            ],
            [exact_row["id"]],
        )

    async def test_invalid_bilibili_pages_stop_at_exact_evidence_budget(
        self,
    ):
        lottery, account, binding = bilibili_exact_loader_fixture(
            lottery_id=21
        )
        page_limit = (
            real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR
        )
        invalid_rows = [
            {
                **binding,
                "id": 1000 - index,
                "probe_id": f"invalid-probe-{index}",
                "shadow_task_id": f"invalid-shadow-{index}",
                "verified_at": "2026-07-23 09:59:00",
            }
            for index in range(page_limit * 3)
        ]
        fake = ExactBindingReadinessDatabase(
            platform="bilibili",
            account=account,
            evidence_rows=invalid_rows,
        )

        with patch.object(
            real_run_readiness,
            "database",
            fake,
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_MAX_EXACT_EVIDENCE_PAGES",
            2,
        ), patch.object(
            real_run_readiness,
            "_exact_bilibili_evidence_observations_valid",
            return_value=False,
        ):
            batch = (
                await real_run_readiness
                .load_account_scoped_real_run_readiness_batch(
                    [lottery],
                    account_ids=[account["id"]],
                )
            )

        self.assertEqual(fake.evidence_page_count, 2)
        self.assertEqual(
            batch.readiness_budget_blockers_by_platform,
            {
                "bilibili": (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER
                )
            },
        )
        self.assertNotIn(
            (lottery["id"], account["id"]),
            batch.bilibili_execution_evidence,
        )

    async def test_weibo_pages_missing_other_binding_stop_at_budget(self):
        lottery, account, binding = weibo_exact_loader_fixture(
            lottery_id=23
        )
        sibling_lottery, _sibling_account, sibling_binding = (
            weibo_exact_loader_fixture(lottery_id=24)
        )
        page_limit = (
            real_run_readiness.MAX_BATCH_EVIDENCE_ROWS_PER_PAIR * 2
        )
        crowded_rows = [
            {
                **binding,
                "id": 1000 - index,
                "task_id": f"dry-run-{index}",
                "finished_at": "2026-07-23 09:59:00",
            }
            for index in range(page_limit * 3)
        ]
        fake = ExactBindingReadinessDatabase(
            platform="weibo",
            account=account,
            evidence_rows=crowded_rows,
        )

        with patch.object(
            real_run_readiness,
            "database",
            fake,
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_MAX_EXACT_EVIDENCE_PAGES",
            2,
        ):
            batch = (
                await real_run_readiness
                .load_account_scoped_real_run_readiness_batch(
                    [lottery, sibling_lottery],
                    account_ids=[account["id"]],
                )
            )

        self.assertEqual(fake.evidence_page_count, 2)
        self.assertEqual(
            batch.readiness_budget_blockers_by_platform,
            {
                "weibo": (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER
                )
            },
        )
        self.assertIn(
            (lottery["id"], account["id"]),
            batch.weibo_oauth_dry_runs,
        )
        self.assertNotIn(
            (sibling_binding["lottery_id"], account["id"]),
            batch.weibo_oauth_dry_runs,
        )

    async def test_exact_evidence_database_timeout_is_explicit(self):
        lottery, account, binding = bilibili_exact_loader_fixture(
            lottery_id=22
        )

        class SlowExactBindingDatabase(ExactBindingReadinessDatabase):
            async def fetch_all(self, query, values=None):
                if "FROM execution_evidence_bindings" in query:
                    await asyncio.sleep(0.05)
                return await super().fetch_all(query, values)

        fake = SlowExactBindingDatabase(
            platform="bilibili",
            account=account,
            evidence_rows=[
                {
                    **binding,
                    "id": 1,
                    "probe_id": "probe",
                    "shadow_task_id": "shadow",
                    "verified_at": "2026-07-23 09:59:00",
                }
            ],
        )
        with patch.object(
            real_run_readiness,
            "database",
            fake,
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_EXACT_EVIDENCE_DB_TIMEOUT_SECONDS",
            0.001,
        ):
            batch = (
                await real_run_readiness
                .load_account_scoped_real_run_readiness_batch(
                    [lottery],
                    account_ids=[account["id"]],
                )
            )

        self.assertEqual(
            batch.readiness_budget_blockers_by_platform,
            {
                "bilibili": (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER
                )
            },
        )

    async def test_account_scoped_batch_cannot_cross_scope_or_lock(self):
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
        )
        lottery = {
            "id": 1,
            "platform": "bilibili",
            "raw_url": "https://www.bilibili.com/opus/123",
        }
        with self.assertRaisesRegex(ValueError, "account scope mismatch"):
            await real_run_readiness.validate_real_run_evidence(
                lottery,
                account_id=5,
                evidence_batch=batch,
            )
        with self.assertRaisesRegex(ValueError, "cannot authorize dispatch"):
            await real_run_readiness.validate_real_run_evidence(
                lottery,
                account_id=4,
                evidence_batch=batch,
                for_update=True,
            )

    async def test_strategy_batch_selects_first_exact_ready_account(self):
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4, 5}),
        )

        async def readiness(_lottery, account_id=None, **_kwargs):
            return {
                "allowed": account_id == 5,
                "blockers": (
                    [] if account_id == 5 else ["exact_evidence_missing"]
                ),
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ) as validate:
            results = (
                await real_run_readiness.evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"},
                            {"account_id": 5, "platform": "bilibili"},
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4, 5}}
                    ),
                )
            )

        self.assertEqual(results[1]["account_id"], 5)
        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertEqual(validate.await_count, 2)

    async def test_strategy_batch_does_not_hide_ready_sixth_account(self):
        account_ids = frozenset(range(1, 7))
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=account_ids,
        )

        async def readiness(_lottery, account_id=None, **_kwargs):
            return {
                "allowed": account_id == 6,
                "blockers": (
                    [] if account_id == 6 else ["exact_evidence_missing"]
                ),
            }

        load_batch = AsyncMock(return_value=batch)
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=load_batch,
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ) as validate:
            results = (
                await real_run_readiness.evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {
                                "account_id": account_id,
                                "platform": "bilibili",
                            }
                            for account_id in range(1, 7)
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: account_ids}
                    ),
                )
            )

        self.assertEqual(results[1]["account_id"], 6)
        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertEqual(validate.await_count, 6)
        self.assertEqual(
            set(load_batch.await_args.kwargs["account_ids"]),
            set(account_ids),
        )

    async def test_candidate_prefilter_uses_only_authoritative_persisted_evidence(
        self,
    ):
        fetch_all = AsyncMock(
            side_effect=[
                [{"lottery_id": 1, "account_id": 997}],
                [{"lottery_id": 2, "account_id": 5}],
            ]
        )
        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=fetch_all,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ]
                )
            )

        self.assertEqual(
            candidate_prefilter.account_ids_for(1),
            frozenset({997}),
        )
        self.assertEqual(
            candidate_prefilter.account_ids_for(2),
            frozenset({5}),
        )
        bilibili_query = fetch_all.await_args_list[0].args[0]
        weibo_query = fetch_all.await_args_list[1].args[0]
        self.assertIn("FROM execution_evidence_bindings e", bilibili_query)
        self.assertIn("e.status = 'verified'", bilibili_query)
        self.assertIn("e.expires_at > NOW()", bilibili_query)
        self.assertIn("FROM task_runs tr", weibo_query)
        self.assertIn("tr.status = 'succeeded'", weibo_query)
        self.assertIn("INTERVAL 24 HOUR", weibo_query)
        self.assertIn("lease.released_at IS NOT NULL", weibo_query)
        self.assertNotIn("selector", (bilibili_query + weibo_query).lower())

    async def test_candidate_prefilter_caps_pathological_platform_rows(self):
        row_limit = (
            real_run_readiness
            .ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
        )
        fetch_all = AsyncMock(
            return_value=[
                {"lottery_id": 1, "account_id": account_id}
                for account_id in range(1, row_limit + 2)
            ]
        )
        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=fetch_all,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [{"id": 1, "platform": "bilibili"}]
                )
            )

        self.assertEqual(
            len(candidate_prefilter.account_ids_for(1)),
            row_limit,
        )
        self.assertEqual(
            candidate_prefilter.budget_blockers_by_platform,
            {},
        )
        self.assertEqual(
            candidate_prefilter.budget_blockers_by_lottery,
            {
                1: (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
                )
            },
        )
        self.assertEqual(
            fetch_all.await_args.args[1][
                "candidate_prefilter_per_lottery_limit"
            ],
            row_limit + 1,
        )

    async def test_candidate_prefilter_uses_a_separate_limit_for_each_lottery(
        self,
    ):
        row_limit = (
            real_run_readiness
            .ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
        )
        fetch_all = AsyncMock(
            return_value=[
                *[
                    {"lottery_id": 1, "account_id": account_id}
                    for account_id in range(1, row_limit + 2)
                ],
                {"lottery_id": 2, "account_id": 9001},
            ]
        )
        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=fetch_all,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "bilibili"},
                    ]
                )
            )

        query = " ".join(fetch_all.await_args.args[0].split())
        values = fetch_all.await_args.args[1]
        self.assertIn("candidate_branch_0", query)
        self.assertIn("candidate_branch_1", query)
        self.assertIn("UNION ALL", query)
        self.assertNotIn("e.lottery_id IN", query)
        self.assertEqual(
            query.count(
                "LIMIT :candidate_prefilter_per_lottery_limit"
            ),
            2,
        )
        self.assertEqual(
            values["candidate_prefilter_per_lottery_limit"],
            row_limit + 1,
        )
        self.assertEqual(
            len(candidate_prefilter.account_ids_for(1)),
            row_limit,
        )
        self.assertEqual(
            candidate_prefilter.account_ids_for(2),
            frozenset({9001}),
        )
        self.assertNotIn(
            2,
            candidate_prefilter.budget_blockers_by_lottery,
        )

    async def test_platform_prefilters_run_concurrently(self):
        entered_platforms: set[str] = set()
        both_entered = asyncio.Event()

        async def fetch_all(query, _values):
            platform = (
                "bilibili"
                if "execution_evidence_bindings" in query
                else "weibo"
            )
            entered_platforms.add(platform)
            if len(entered_platforms) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=0.2)
            return [
                {
                    "lottery_id": 1 if platform == "bilibili" else 2,
                    "account_id": 4 if platform == "bilibili" else 5,
                }
            ]

        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS",
            0.1,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ]
                )
            )

        self.assertEqual(entered_platforms, {"bilibili", "weibo"})
        self.assertEqual(
            candidate_prefilter.account_ids_for(1),
            frozenset({4}),
        )
        self.assertEqual(
            candidate_prefilter.account_ids_for(2),
            frozenset({5}),
        )

    async def test_prefilter_timeout_isolated_from_sibling_platform(self):
        async def fetch_all(query, _values):
            if "execution_evidence_bindings" in query:
                await asyncio.sleep(0.2)
            return [{"lottery_id": 2, "account_id": 5}]

        started_at = time.monotonic()
        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS",
            0.02,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ]
                )
            )
        elapsed = time.monotonic() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertEqual(
            candidate_prefilter.budget_blockers_by_platform,
            {
                "bilibili": (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_BLOCKER
                )
            },
        )
        self.assertEqual(
            candidate_prefilter.account_ids_for(2),
            frozenset({5}),
        )

    async def test_pathological_candidate_iterable_stops_at_platform_budget(
        self,
    ):
        loads = []
        pulled = 0

        def candidates():
            nonlocal pulled
            account_id = 1
            while True:
                pulled += 1
                yield {
                    "account_id": account_id,
                    "platform": "bilibili",
                }
                account_id += 1

        async def load_batch(lotteries, *, account_ids):
            normalized_account_ids = list(account_ids)
            loads.append(
                (
                    [int(lottery["id"]) for lottery in lotteries],
                    normalized_account_ids,
                )
            )
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(normalized_account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(_lottery, account_id=None, **_kwargs):
            return {
                "allowed": False,
                "blockers": ["exact_execution_evidence_required"],
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ) as validate:
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": candidates()
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {997}}
                    ),
                )
            )

        self.assertEqual(
            pulled,
            (
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_MAX_CANDIDATES_PER_PLATFORM
                + 1
            ),
        )
        self.assertIsNone(results[1]["account_id"])
        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
            ],
        )
        self.assertEqual(loads, [([1], [])])
        self.assertEqual(validate.await_count, 1)

    async def test_candidate_prefilter_isolated_per_lottery(self):
        evaluated_pairs = []

        async def load_batch(_lotteries, *, account_ids):
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, account_id=None, **_kwargs):
            pair = (int(lottery["id"]), int(account_id))
            evaluated_pairs.append(pair)
            return {"allowed": True, "blockers": []}

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "bilibili"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"},
                            {"account_id": 5, "platform": "bilibili"},
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertEqual(evaluated_pairs, [(1, 4), (2, 5)])
        self.assertEqual(results[1]["account_id"], 4)
        self.assertEqual(results[2]["account_id"], 5)

    async def test_one_lottery_prefilter_overflow_does_not_poison_peer(self):
        async def load_batch(_lotteries, *, account_ids):
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, account_id=None, **_kwargs):
            allowed = int(lottery["id"]) == 2 and int(account_id) == 5
            return {
                "allowed": allowed,
                "blockers": [] if allowed else ["not_ready"],
            }

        candidate_prefilter = (
            real_run_readiness.AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={
                    1: frozenset({4}),
                    2: frozenset({5}),
                },
                budget_blockers_by_lottery={
                    1: (
                        real_run_readiness
                        .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
                    )
                },
            )
        )
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "bilibili"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"},
                            {"account_id": 5, "platform": "bilibili"},
                        ]
                    },
                    candidate_prefilter=candidate_prefilter,
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
            ],
        )
        self.assertEqual(results[2]["account_id"], 5)
        self.assertTrue(results[2]["readiness"]["allowed"])

    async def test_per_lottery_budget_survives_cross_platform_split(self):
        async def load_batch(_lotteries, *, account_ids):
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, account_id=None, **_kwargs):
            allowed = str(lottery["platform"]) == "weibo"
            return {
                "allowed": allowed,
                "blockers": [] if allowed else ["not_ready"],
            }

        candidate_prefilter = (
            real_run_readiness.AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={
                    1: frozenset({4}),
                    2: frozenset({5}),
                },
                budget_blockers_by_lottery={
                    1: (
                        real_run_readiness
                        .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
                    )
                },
            )
        )
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=candidate_prefilter,
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_CANDIDATE_BUDGET_BLOCKER
            ],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])

    async def test_no_authoritative_candidate_reports_evidence_blocker(
        self,
    ):
        loads = []

        async def load_batch(lotteries, *, account_ids):
            normalized_account_ids = list(account_ids)
            loads.append(
                (
                    str(lotteries[0]["platform"]),
                    normalized_account_ids,
                )
            )
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(normalized_account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, account_id=None, **_kwargs):
            self.assertIsNone(account_id)
            blocker = (
                "execution_account_scope_required"
                if lottery["platform"] == "bilibili"
                else "weibo_oauth_account_scope_required"
            )
            return {
                "allowed": False,
                "blockers": [blocker],
                "capability_reason": blocker,
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {
                            "id": 1,
                            "platform": "bilibili",
                            "safe_accounts": 50,
                        },
                        {
                            "id": 2,
                            "platform": "weibo",
                            "safe_accounts": 50,
                        },
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: set(), 2: set()}
                    ),
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["exact_execution_evidence_required"],
        )
        self.assertEqual(
            results[2]["readiness"]["blockers"],
            ["recent_oauth_dry_run_required"],
        )
        self.assertEqual(
            loads,
            [("bilibili", []), ("weibo", [])],
        )

    async def test_no_ready_account_keeps_account_scope_blocker(self):
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset(),
            account_scoped_readiness=True,
        )
        readiness = AsyncMock(
            return_value={
                "allowed": False,
                "blockers": ["execution_account_scope_required"],
                "capability_reason": "execution_account_scope_required",
            }
        )
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=readiness,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {
                            "id": 1,
                            "platform": "bilibili",
                            "safe_accounts": 0,
                        }
                    ],
                    account_candidates={"bilibili": []},
                    candidate_prefilter=self.candidate_prefilter(
                        {1: set()}
                    ),
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["execution_account_scope_required"],
        )

    async def test_prefilter_query_failure_is_local_and_fail_closed(self):
        async def fetch_all(query, _values):
            if "execution_evidence_bindings" in query:
                raise RuntimeError("bilibili evidence database unavailable")
            return [{"lottery_id": 2, "account_id": 5}]

        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=AsyncMock(side_effect=fetch_all),
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ]
                )
            )

        self.assertEqual(
            candidate_prefilter.failed_platforms,
            frozenset({"bilibili"}),
        )
        self.assertEqual(
            candidate_prefilter.account_ids_for(2),
            frozenset({5}),
        )

        load_batch = AsyncMock(
            return_value=real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset({5}),
                account_scoped_readiness=True,
            )
        )
        validate = AsyncMock(return_value={"allowed": True, "blockers": []})
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=load_batch,
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=validate,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=candidate_prefilter,
                )
            )

        self.assertFalse(results[1]["readiness"]["allowed"])
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["account_scoped_real_run_readiness_unavailable"],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)
        self.assertEqual(
            load_batch.await_args.kwargs["account_ids"],
            [5],
        )
        self.assertEqual(validate.await_count, 1)

    async def test_evidence_batch_load_failure_is_local_to_one_platform(self):
        async def load_batch(lotteries, *, account_ids):
            platform = str(lotteries[0]["platform"])
            if platform == "bilibili":
                raise RuntimeError("bilibili evidence batch unavailable")
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        validate = AsyncMock(return_value={"allowed": True, "blockers": []})
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=validate,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["account_scoped_real_run_readiness_unavailable"],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)
        validate.assert_awaited_once()

    async def test_candidate_validation_failure_is_local_to_one_platform(self):
        load_batch = AsyncMock(
            side_effect=lambda _lotteries, *, account_ids: (
                real_run_readiness.RealRunEvidenceBatch(
                    account_id=None,
                    account_ids=frozenset(account_ids),
                    account_scoped_readiness=True,
                )
            )
        )

        async def validate(lottery, account_id=None, **_kwargs):
            if lottery["platform"] == "bilibili":
                raise RuntimeError("bilibili readiness provider failed")
            return {"allowed": True, "blockers": []}

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=load_batch,
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=validate),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            ["account_scoped_real_run_readiness_unavailable"],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)

    async def test_candidate_validation_failure_does_not_hide_later_ready_account(
        self,
    ):
        load_batch = AsyncMock(
            side_effect=lambda _lotteries, *, account_ids: (
                real_run_readiness.RealRunEvidenceBatch(
                    account_id=None,
                    account_ids=frozenset(account_ids),
                    account_scoped_readiness=True,
                )
            )
        )

        async def validate(_lottery, account_id=None, **_kwargs):
            if account_id == 4:
                raise RuntimeError("candidate evidence row is corrupt")
            return {
                "allowed": account_id == 5,
                "blockers": [] if account_id == 5 else ["not_ready"],
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=load_batch,
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=validate),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"},
                            {"account_id": 5, "platform": "bilibili"},
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4, 5}}
                    ),
                )
            )

        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertEqual(results[1]["account_id"], 5)
        load_batch.assert_awaited_once()

    async def test_strategy_progresses_to_later_bounded_account_batch(self):
        loads = []

        async def load_batch(lotteries, *, account_ids):
            lottery_ids = [int(lottery["id"]) for lottery in lotteries]
            account_ids = list(account_ids)
            loads.append((lottery_ids, account_ids))
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, account_id=None, **_kwargs):
            allowed = (
                (int(lottery["id"]) == 1 and account_id == 1)
                or (int(lottery["id"]) == 2 and account_id == 17)
            )
            return {
                "allowed": allowed,
                "blockers": [] if allowed else ["exact_evidence_missing"],
            }

        candidates = [
            {"account_id": account_id, "platform": "bilibili"}
            for account_id in range(1, 18)
        ]
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ) as validate:
            results = (
                await real_run_readiness.evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "bilibili"},
                    ],
                    account_candidates={"bilibili": candidates},
                    candidate_prefilter=self.candidate_prefilter(
                        {
                            1: range(1, 18),
                            2: range(1, 18),
                        }
                    ),
                )
            )

        self.assertEqual(results[1]["account_id"], 1)
        self.assertEqual(results[2]["account_id"], 17)
        self.assertEqual(
            loads,
            [
                ([1, 2], list(range(1, 17))),
                ([2], [17]),
            ],
        )
        self.assertEqual(validate.await_count, 18)
        self.assertTrue(
            all(
                len(account_ids)
                <= (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
                )
                for _lottery_ids, account_ids in loads
            )
        )

    async def test_account_scoped_loader_rejects_oversized_dimensions(self):
        lotteries = [
            {"id": lottery_id, "platform": "bilibili"}
            for lottery_id in range(
                1,
                (
                    real_run_readiness
                    .MAX_ACCOUNT_SCOPED_READINESS_LOTTERIES
                    + 2
                ),
            )
        ]
        with self.assertRaisesRegex(ValueError, "lottery batch exceeds"):
            await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                lotteries,
                account_ids=(),
            )

        account_ids = range(
            1,
            (
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_ACCOUNT_BATCH_SIZE
                + 2
            ),
        )
        with self.assertRaisesRegex(ValueError, "account batch exceeds"):
            await real_run_readiness.load_account_scoped_real_run_readiness_batch(
                [{"id": 1, "platform": "bilibili"}],
                account_ids=account_ids,
            )

    async def test_platform_evaluation_timeout_has_distinct_blocker(self):
        candidate_prefilter = (
            real_run_readiness.AccountScopedReadinessCandidatePrefilter(
                account_ids_by_lottery={1: frozenset({4})},
            )
        )
        async def load_batch(_lotteries, *, account_ids):
            await asyncio.sleep(0.05)
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        validate = AsyncMock()
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=validate,
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS",
            0.001,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=candidate_prefilter,
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
            ],
        )
        validate.assert_not_awaited()

    async def test_ranking_delay_does_not_consume_evidence_budget(self):
        with patch(
            "app.services.real_run_readiness.database.fetch_all",
            new=AsyncMock(
                return_value=[{"lottery_id": 1, "account_id": 4}]
            ),
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PREFILTER_TIMEOUT_SECONDS",
            0.01,
        ):
            candidate_prefilter = (
                await real_run_readiness
                .load_account_scoped_readiness_candidate_prefilter(
                    [{"id": 1, "platform": "bilibili"}]
                )
            )

        # Simulate the full account ranking query occurring between phases.
        await asyncio.sleep(0.03)
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
        )
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(return_value={"allowed": True, "blockers": []}),
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS",
            0.02,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=candidate_prefilter,
                )
            )

        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertEqual(results[1]["account_id"], 4)

    async def test_pathological_first_platform_does_not_consume_sibling_budget(
        self,
    ):
        weibo_loaded = asyncio.Event()

        async def load_batch(lotteries, *, account_ids):
            platform = str(lotteries[0]["platform"])
            if platform == "bilibili":
                await asyncio.sleep(0.2)
            else:
                weibo_loaded.set()
            return real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )

        async def readiness(lottery, **_kwargs):
            allowed = lottery["platform"] == "weibo"
            return {
                "allowed": allowed,
                "blockers": [] if allowed else ["not_ready"],
            }

        started_at = time.monotonic()
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ), patch.object(
            real_run_readiness,
            "ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_SECONDS",
            0.03,
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )
        elapsed = time.monotonic() - started_at

        self.assertTrue(weibo_loaded.is_set())
        self.assertLess(elapsed, 0.1)
        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_PLATFORM_EVALUATION_TIMEOUT_BLOCKER
            ],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)

    async def test_exact_evidence_timeout_isolated_from_sibling_platform(
        self,
    ):
        async def load_batch(lotteries, *, account_ids):
            platform = str(lotteries[0]["platform"])
            batch = real_run_readiness.RealRunEvidenceBatch(
                account_id=None,
                account_ids=frozenset(account_ids),
                account_scoped_readiness=True,
            )
            if platform == "bilibili":
                batch.readiness_budget_blockers_by_platform[platform] = (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER
                )
            return batch

        async def readiness(lottery, **_kwargs):
            allowed = lottery["platform"] == "weibo"
            return {
                "allowed": allowed,
                "blockers": [] if allowed else ["evidence_missing"],
            }

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(side_effect=load_batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertEqual(
            results[1]["readiness"]["blockers"],
            [
                real_run_readiness
                .ACCOUNT_SCOPED_READINESS_EVIDENCE_QUERY_TIMEOUT_BLOCKER
            ],
        )
        self.assertTrue(results[2]["readiness"]["allowed"])
        self.assertEqual(results[2]["account_id"], 5)

    async def test_ready_candidate_survives_page_budget_exhaustion(self):
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4}),
            account_scoped_readiness=True,
            readiness_budget_blockers_by_platform={
                "bilibili": (
                    real_run_readiness
                    .ACCOUNT_SCOPED_READINESS_EVIDENCE_PAGE_BUDGET_BLOCKER
                )
            },
        )
        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(return_value={"allowed": True, "blockers": []}),
        ):
            results = (
                await real_run_readiness
                .evaluate_account_scoped_real_run_readiness_batch(
                    [{"id": 1, "platform": "bilibili"}],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ]
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}}
                    ),
                )
            )

        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertEqual(results[1]["account_id"], 4)

    async def test_one_unavailable_platform_does_not_poison_strategy_batch(self):
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({4, 5}),
            account_scoped_readiness=True,
        )

        async def readiness(lottery, account_id=None, **_kwargs):
            if lottery["platform"] == "weibo":
                raise PlatformModuleUnavailableError("weibo")
            return {"allowed": True, "blockers": []}

        with patch(
            "app.services.real_run_readiness.load_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=batch),
        ), patch(
            "app.services.real_run_readiness.validate_real_run_evidence",
            new=AsyncMock(side_effect=readiness),
        ):
            results = (
                await real_run_readiness.evaluate_account_scoped_real_run_readiness_batch(
                    [
                        {"id": 1, "platform": "bilibili"},
                        {"id": 2, "platform": "weibo"},
                    ],
                    account_candidates={
                        "bilibili": [
                            {"account_id": 4, "platform": "bilibili"}
                        ],
                        "weibo": [
                            {"account_id": 5, "platform": "weibo"}
                        ],
                    },
                    candidate_prefilter=self.candidate_prefilter(
                        {1: {4}, 2: {5}}
                    ),
                )
            )

        self.assertTrue(results[1]["readiness"]["allowed"])
        self.assertFalse(results[2]["readiness"]["allowed"])
        self.assertEqual(
            results[2]["readiness"]["blockers"],
            ["platform_module_unavailable"],
        )


if __name__ == "__main__":
    unittest.main()
