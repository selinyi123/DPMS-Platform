"""Offline tests for the Bilibili API-v2 consume/action gate."""

import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "httpx" not in sys.modules and importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.AsyncBaseTransport = object
    httpx.AsyncClient = object
    httpx.TransportError = RuntimeError
    sys.modules["httpx"] = httpx

from app.action_plan import (  # noqa: E402
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
)
from app.bilibili.preflight import (  # noqa: E402
    API_PREFLIGHT_KIND,
    hash_preflight_observation,
)
from app.real_run_gate import (  # noqa: E402
    RealRunGateBlocked,
    enforce_real_run_gate,
    open_unknown_outcome_breaker,
)


DYNAMIC_ID = "123456789012"
RAW_URL = f"https://t.bilibili.com/{DYNAMIC_ID}"
CANONICAL_URL = f"canonical://bilibili/dynamic/{DYNAMIC_ID}"
FOLLOW_HANDLE = "@ASUS华硕官方UP"
RULE_TEXT = "关注@ASUS华硕官方UP，转评赞并在评论中带话题#ASUS翻转夏日#。"
RULE_HASH = compute_rule_hash(RULE_TEXT)
REVISION = 5
CONFIG_HASH = compute_bilibili_api_config_hash(REVISION)


def _plan():
    plan = {
        "version": 2,
        "platform": "bilibili",
        "rule_snapshot_id": 101,
        "rule_hash": RULE_HASH,
        "execution_path_id": "bilibili_api_v2",
        "required_actions": ["followed", "liked", "commented", "reposted"],
        "action_payloads": {
            "followed": {"target_handle": FOLLOW_HANDLE},
            "liked": {},
            "commented": {
                "text": f"#ASUS翻转夏日# {FOLLOW_HANDLE} 精确评论",
                "topic_tags": ["#ASUS翻转夏日#"],
                "mentions": [FOLLOW_HANDLE],
            },
            "reposted": {"text": "精确转发"},
        },
        "content_requirements": {
            "follow_targets": [FOLLOW_HANDLE],
            "commented": {
                "topic_tags": ["#ASUS翻转夏日#"],
                "mentions": [FOLLOW_HANDLE],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def _observation():
    return {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": "bilibili_api_v2",
        "preflight_contract_version": 1,
        "execution_revision": REVISION,
        "config_hash": CONFIG_HASH,
        "side_effects": False,
        "account_authenticated": True,
        "api_preflight_complete": True,
        "requested_dynamic_id": DYNAMIC_ID,
        "observed_dynamic_id": DYNAMIC_ID,
        "target_type": 4,
        "target_uid": 10086,
        "author_handle": FOLLOW_HANDLE,
        "follow_target_handle": FOLLOW_HANDLE,
        "target_identity": {
            "verified": True,
            "dynamic_id": DYNAMIC_ID,
            "author_uid": 10086,
            "author_handle": FOLLOW_HANDLE,
        },
        "comment_rid_str": DYNAMIC_ID,
        "comment_type": 17,
        "required_actions": ["followed", "liked", "commented", "reposted"],
        "api_actions": ["follow", "like", "comment", "repost"],
        "capability_checks": {
            "followed": True,
            "liked": True,
            "commented": True,
            "reposted": True,
        },
    }


def _task():
    plan = _plan()
    return {
        "task_id": "task-1",
        "account_id": "41",
        "lottery_id": "73",
        "platform": "bilibili",
        "mode": "real_run",
        "raw_url": RAW_URL,
        "canonical_url": CANONICAL_URL,
        "rule_snapshot_id": "101",
        "rule_hash": RULE_HASH,
        "action_plan_hash": plan["plan_hash"],
        "execution_path_id": "bilibili_api_v2",
        "target_hash": compute_target_hash(CANONICAL_URL),
        "config_hash": CONFIG_HASH,
        "execution_evidence_id": "evidence-1",
        "account_lease_id": "lease-1",
        "account_lease_generation": "7",
        "action_plan": plan,
        # This suite exercises the pre-intent compatibility envelope.  Legacy
        # tasks are accepted only when Core's immutable shared-stream Outbox
        # row and fan-out provenance are both present.
        "legacy_source_stream": "lottery_tasks",
        "legacy_source_message_id": "1700000000000-0",
    }


def _decision_row():
    plan = _plan()
    observation = _observation()
    observation_hash = hash_preflight_observation(observation)
    target_hash = compute_target_hash(CANONICAL_URL)
    return {
        "task_id": "task-1",
        "account_id": 41,
        "lottery_id": 73,
        "task_status": "queued",
        "task_mode": "real_run",
        "task_worker_id": None,
        "decision_id": "decision-1",
        "task_policy_version": 3,
        "task_rule_snapshot_id": 101,
        "task_rule_hash": RULE_HASH,
        "task_action_plan_hash": plan["plan_hash"],
        "task_target_hash": target_hash,
        "task_config_hash": CONFIG_HASH,
        "task_execution_evidence_id": "evidence-1",
        "task_execution_path_id": "bilibili_api_v2",
        "task_account_lease_id": "lease-1",
        "task_account_lease_generation": 7,
        "task_reconciliation_required": 0,
        "bound_account_id": 41,
        "account_platform": "bilibili",
        "account_status": "ready",
        "account_execution_revision": REVISION,
        "account_active_risk": 0,
        "bound_lottery_id": 73,
        "lottery_platform": "bilibili",
        "lottery_status": "claimed",
        "lottery_execution_lock": "task-1",
        "lottery_raw_url": RAW_URL,
        "lottery_canonical_url": CANONICAL_URL,
        "lottery_action_plan": plan,
        "lottery_rule_snapshot_id": 101,
        "lottery_rule_hash": RULE_HASH,
        "lottery_action_plan_hash": plan["plan_hash"],
        "lottery_rule_text": RULE_TEXT,
        "rule_snapshot_platform": "bilibili",
        "snapshot_rule_text": RULE_TEXT,
        "rule_snapshot_complete": 1,
        "rule_snapshot_attested_by": "operator-1",
        "rule_snapshot_attested_at": "2026-07-21T00:00:00Z",
        "snapshot_rule_hash": RULE_HASH,
        "evidence_id": "evidence-1",
        "evidence_lottery_id": 73,
        "evidence_account_id": 41,
        "evidence_platform": "bilibili",
        "evidence_rule_snapshot_id": 101,
        "evidence_execution_path_id": "bilibili_api_v2",
        "evidence_target_hash": target_hash,
        "evidence_rule_hash": RULE_HASH,
        "evidence_action_plan_hash": plan["plan_hash"],
        "evidence_config_hash": CONFIG_HASH,
        "evidence_probe_id": "probe-1",
        "evidence_shadow_task_id": "shadow-1",
        "evidence_probe_observation_kind": API_PREFLIGHT_KIND,
        "evidence_probe_observation_hash": observation_hash,
        "evidence_shadow_observation_kind": API_PREFLIGHT_KIND,
        "evidence_shadow_observation_hash": observation_hash,
        "evidence_status": "verified",
        "evidence_verified_at": "2026-07-21T00:00:00Z",
        "evidence_expires_at": "2026-07-21T23:59:59Z",
        "evidence_active": 1,
        "evidence_time_bounded": 1,
        "evidence_probe_status": "succeeded",
        "evidence_probe_observation": json.dumps(observation, ensure_ascii=False),
        "source_probe_observation_kind": API_PREFLIGHT_KIND,
        "source_probe_observation_hash": observation_hash,
        "evidence_probe_finished_at": "2026-07-21T00:00:00Z",
        "evidence_probe_fresh": 1,
        "evidence_probe_lease_released": 1,
        "evidence_probe_lease_covers_observation": 1,
        "evidence_shadow_status": "succeeded",
        "evidence_shadow_task_mode": "shadow_run",
        "evidence_shadow_observation": json.dumps(observation, ensure_ascii=False),
        "source_shadow_observation_kind": API_PREFLIGHT_KIND,
        "source_shadow_observation_hash": observation_hash,
        "evidence_shadow_finished_at": "2026-07-21T00:00:00Z",
        "evidence_shadow_fresh": 1,
        "evidence_shadow_lease_released": 1,
        "evidence_shadow_lease_covers_observation": 1,
        "evidence_shadow_target_hash": target_hash,
        "evidence_shadow_config_hash": CONFIG_HASH,
        "lease_account_id": 41,
        "lease_id": "lease-1",
        "lease_generation": 7,
        "lease_operation_kind": "real_run",
        "lease_owner_id": "task-1",
        "lease_task_id": "task-1",
        "lease_active": 1,
        "lease_unreleased": 1,
        "lease_latest_generation": 1,
        "active_account_lease_count": 1,
        "policy_decision_id": "decision-1",
        "decision_policy_key": "real_run_gate",
        "decision_policy_version": 3,
        "decision_subject_type": "lottery",
        "decision_subject_id": "73",
        "decision_outcome": "allow",
        "policy_active": 1,
        "legacy_outbox_stream_key": "lottery_tasks",
        "legacy_outbox_status": "sent",
        "legacy_outbox_dedup_key": "task-1",
        "legacy_outbox_payload": {
            "task_id": "task-1",
            "account_id": "41",
            "lottery_id": "73",
            "platform": "bilibili",
            "mode": "real_run",
            "action_plan": plan,
            "action_plan_hash": plan["plan_hash"],
        },
    }


class FakeGateDatabase:
    def __init__(self):
        self.setting = {"setting_value": "true"}
        self.decision = _decision_row()
        self.breakers = [{"scope": "global", "status": "closed", "reason": None}]
        self.raise_on = None
        self.executions = []
        self.platform_breaker_status = "open"

    async def fetch_one(self, query, values=None):
        if (
            self.raise_on == "evidence_fetch_one"
            and "execution_evidence_bindings" in query
        ):
            raise RuntimeError("platform evidence database unavailable")
        if self.raise_on == "fetch_one":
            raise RuntimeError("database unavailable")
        if "runtime_settings" in query:
            return self.setting
        if "SELECT status FROM circuit_breakers" in query:
            return {"status": self.platform_breaker_status}
        if "FROM task_runs" in query:
            return self.decision
        raise AssertionError(f"unexpected fetch_one query: {query}")

    async def fetch_all(self, query, values=None):
        if self.raise_on == "fetch_all":
            raise RuntimeError("database unavailable")
        if "circuit_breakers" in query:
            return self.breakers
        raise AssertionError(f"unexpected fetch_all query: {query}")

    async def execute(self, query, values=None):
        if self.raise_on == "execute":
            raise RuntimeError("database unavailable")
        self.executions.append((query, dict(values or {})))


class RealRunConsumeGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process_gate = patch.dict(
            os.environ,
            {"REAL_RUN_ENABLED": "true"},
            clear=False,
        )
        self.process_gate.start()
        self.addAsyncCleanup(self.process_gate.stop)

    async def assert_blocked(self, db, code, task=None):
        with self.assertRaises(RealRunGateBlocked) as caught:
            await enforce_real_run_gate(task or _task(), db=db, worker_id="worker-a")
        self.assertEqual(caught.exception.code, code)

    async def test_all_authorities_allow_before_and_after_start(self):
        db = FakeGateDatabase()
        snapshot = await enforce_real_run_gate(_task(), db=db, worker_id="worker-a")
        self.assertEqual(snapshot.stage, "preclaim")
        db.decision.update(
            task_status="running",
            lottery_status="running",
            account_status="executing",
            task_worker_id="worker-a",
        )
        snapshot = await enforce_real_run_gate(_task(), db=db, worker_id="worker-a")
        self.assertEqual(snapshot.stage, "running")

    async def test_runtime_switch_is_rechecked_and_fail_closed(self):
        db = FakeGateDatabase()
        db.setting = {"setting_value": "false"}
        await self.assert_blocked(db, "real_run_disabled")
        db.setting = None
        await self.assert_blocked(db, "runtime_setting_missing")

    async def test_active_account_risk_is_rechecked_before_every_action(self):
        db = FakeGateDatabase()
        db.decision["account_active_risk"] = 1

        await self.assert_blocked(db, "recent_account_risk_event")

    def test_gate_query_uses_the_materialized_active_risk_state(self):
        from app import real_run_gate

        compact = " ".join(real_run_gate._TASK_DECISION_QUERY.split())
        self.assertIn(
            "FROM account_active_risk_states active_risk",
            compact,
        )
        self.assertIn(
            "active_risk.active_until > NOW()",
            compact,
        )

    async def test_process_switch_is_independent_and_fail_closed(self):
        for process_value in (None, "", "false", "0", "invalid"):
            with self.subTest(process_value=process_value):
                db = FakeGateDatabase()
                # Prove the process gate blocks before consulting otherwise
                # still-enabled durable database state.
                db.raise_on = "fetch_one"
                with patch.dict(os.environ, {}, clear=False):
                    if process_value is None:
                        os.environ.pop("REAL_RUN_ENABLED", None)
                    else:
                        os.environ["REAL_RUN_ENABLED"] = process_value
                    await self.assert_blocked(db, "process_real_run_disabled")

    async def test_observation_identity_hash_and_follow_target_are_exact(self):
        for field, value, code in (
            ("source_probe_observation_hash", "0" * 64, "probe_shadow_observation_integrity_invalid"),
            ("evidence_probe_observation_kind", "selector_probe", "probe_shadow_observation_integrity_invalid"),
        ):
            with self.subTest(field=field):
                db = FakeGateDatabase()
                db.decision[field] = value
                await self.assert_blocked(db, code)

        db = FakeGateDatabase()
        observation = json.loads(db.decision["evidence_probe_observation"])
        observation["target_identity"]["author_handle"] = "@另一个账号"
        db.decision["evidence_probe_observation"] = json.dumps(observation, ensure_ascii=False)
        await self.assert_blocked(db, "probe_shadow_observation_invalid")

    async def test_each_source_must_be_fresh_released_and_covered_by_lease(self):
        for field in (
            "evidence_probe_fresh",
            "evidence_shadow_fresh",
            "evidence_probe_lease_released",
            "evidence_shadow_lease_released",
            "evidence_probe_lease_covers_observation",
            "evidence_shadow_lease_covers_observation",
        ):
            with self.subTest(field=field):
                db = FakeGateDatabase()
                db.decision[field] = 0
                await self.assert_blocked(db, "probe_shadow_evidence_incomplete")

    async def test_evidence_time_must_be_bounded_by_both_source_observations(self):
        for field, value in (
            ("evidence_time_bounded", 0),
            ("evidence_expires_at", None),
            ("evidence_verified_at", None),
        ):
            with self.subTest(field=field):
                db = FakeGateDatabase()
                db.decision[field] = value
                await self.assert_blocked(db, "execution_evidence_not_active")

    async def test_credential_execution_revision_change_invalidates_evidence(self):
        db = FakeGateDatabase()
        db.decision["account_execution_revision"] = REVISION + 1
        await self.assert_blocked(db, "execution_evidence_binding_invalid")

    async def test_real_lease_must_be_latest_unique_active_generation(self):
        for field, value in (
            ("lease_latest_generation", 0),
            ("active_account_lease_count", 2),
            ("lease_unreleased", 0),
            ("task_reconciliation_required", 1),
        ):
            with self.subTest(field=field):
                db = FakeGateDatabase()
                db.decision[field] = value
                expected = (
                    "task_reconciliation_required"
                    if field == "task_reconciliation_required"
                    else "account_lease_binding_invalid"
                )
                await self.assert_blocked(db, expected)

    async def test_breakers_and_policy_remain_authoritative(self):
        db = FakeGateDatabase()
        db.breakers = [{"scope": "global", "status": "open", "reason": "stop"}]
        await self.assert_blocked(db, "circuit_breaker_blocked")
        db = FakeGateDatabase()
        db.decision["decision_outcome"] = "deny"
        await self.assert_blocked(db, "policy_decision_denied")

    async def test_database_errors_are_collapsed_to_safe_gate_code(self):
        db = FakeGateDatabase()
        db.raise_on = "fetch_one"
        await self.assert_blocked(db, "gate_database_error")

    async def test_platform_evidence_loader_errors_fail_closed(self):
        db = FakeGateDatabase()
        db.raise_on = "evidence_fetch_one"
        await self.assert_blocked(db, "gate_database_error")

    async def test_unknown_outcome_opens_platform_breaker(self):
        db = FakeGateDatabase()
        await open_unknown_outcome_breaker(db=db, platform=" BiliBili ", action=" Like ")
        query, values = db.executions[0]
        self.assertIn("INSERT INTO circuit_breakers", query)
        self.assertEqual(values["scope"], "platform:bilibili")
        self.assertEqual(values["reason"], "bilibili_like_outcome_unknown")

    async def test_unknown_outcome_breaker_must_read_back_open(self):
        db = FakeGateDatabase()
        db.platform_breaker_status = "closed"
        with self.assertRaises(RealRunGateBlocked) as caught:
            await open_unknown_outcome_breaker(db=db, platform="bilibili", action="like")
        self.assertEqual(caught.exception.code, "unknown_outcome_breaker_write_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
