import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app import task_runner  # noqa: E402
from app.action_plan import (  # noqa: E402
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_target_hash,
)
from app.bilibili.preflight import (  # noqa: E402
    API_PREFLIGHT_KIND,
    ApiPreflightEvidence,
    hash_preflight_observation,
)


DYNAMIC_ID = "123456789012"
RAW_URL = f"https://t.bilibili.com/{DYNAMIC_ID}"
CANONICAL_URL = f"canonical://bilibili/dynamic/{DYNAMIC_ID}"
FOLLOW_HANDLE = "@ASUS华硕官方UP"


def plan_v2():
    plan = {
        "version": 2,
        "platform": "bilibili",
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "execution_path_id": "bilibili_api_v2",
        "required_actions": ["followed", "liked"],
        "action_payloads": {
            "followed": {"target_handle": FOLLOW_HANDLE},
            "liked": {},
        },
        "content_requirements": {
            "follow_targets": [FOLLOW_HANDLE],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def preflight_evidence():
    config_hash = compute_bilibili_api_config_hash(3)
    observation = {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": "bilibili_api_v2",
        "preflight_contract_version": 1,
        "execution_revision": 3,
        "config_hash": config_hash,
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
        "required_actions": ["followed", "liked"],
        "api_actions": ["follow", "like"],
        "capability_checks": {"followed": True, "liked": True},
    }
    return ApiPreflightEvidence(observation, hash_preflight_observation(observation))


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeShadowDatabase:
    def __init__(self):
        self.observation = None
        self.observation_kind = None
        self.observation_hash = None
        self.phases = []
        self.lease_active = 1
        self.lease_latest_generation = 1
        self.active_account_lease_count = 1

    def transaction(self):
        return Tx()

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            return {
                "execution_revision": 3,
                "platform": "bilibili",
                "status": "ready",
                "encrypted_credential": "SESSDATA=fake",
            }
        if "FROM task_runs tr" in query:
            plan = plan_v2()
            return {
                "status": "running",
                "worker_id": "worker-test",
                "account_id": 7,
                "lottery_id": 11,
                "execution_path_id": "bilibili_api_v2",
                "target_hash": compute_target_hash(CANONICAL_URL),
                "rule_hash": plan["rule_hash"],
                "action_plan_hash": plan["plan_hash"],
                "config_hash": compute_bilibili_api_config_hash(3),
                "account_lease_id": "lease-shadow",
                "account_lease_generation": 4,
                "execution_revision": 3,
                "lease_active": self.lease_active,
                "lease_unreleased": 1,
                "lease_latest_generation": self.lease_latest_generation,
                "active_account_lease_count": self.active_account_lease_count,
                "operation_kind": "shadow_run",
                "owner_id": "task-shadow",
                "lease_task_id": "task-shadow",
            }
        if "FROM account_operation_leases" in query:
            return {
                "lease_id": "lease-shadow",
                "account_id": 7,
                "generation": 4,
                "operation_kind": "shadow_run",
                "owner_id": "task-shadow",
                "task_id": "task-shadow",
                "lease_active": self.lease_active,
                "lease_unreleased": 1,
                "lease_latest_generation": self.lease_latest_generation,
                "active_account_lease_count": self.active_account_lease_count,
            }
        if "preflight_observation" in query and "FROM task_runs" in query:
            return {
                "preflight_observation": self.observation,
                "preflight_observation_kind": self.observation_kind,
                "preflight_observation_hash": self.observation_hash,
            }
        if "SELECT status, worker_id FROM task_runs" in query:
            return {"status": "running", "worker_id": "worker-test"}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        values = dict(values or {})
        if "SET preflight_observation" in query:
            self.observation = values["observation"]
            self.observation_kind = values["kind"]
            self.observation_hash = values["observation_hash"]
        elif "INSERT INTO task_phases" in query:
            self.phases.append(values["phase"])


class PoolMustNotBeUsed:
    async def get_account_context(self, *_args, **_kwargs):
        raise AssertionError("Bilibili API shadow must not open a browser")


class BilibiliShadowRunSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_database = task_runner.database
        self.original_worker = task_runner.WORKER_ID
        self.db = FakeShadowDatabase()
        task_runner.database = self.db
        task_runner.WORKER_ID = "worker-test"

    async def asyncTearDown(self):
        task_runner.database = self.original_database
        task_runner.WORKER_ID = self.original_worker

    def task(self):
        plan = plan_v2()
        return {
            "task_id": "task-shadow",
            "account_id": "7",
            "lottery_id": "11",
            "platform": "bilibili",
            "raw_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "action_plan": plan,
            "target_hash": compute_target_hash(CANONICAL_URL),
            "config_hash": compute_bilibili_api_config_hash(3),
            "account_lease_id": "lease-shadow",
            "account_lease_generation": "4",
        }

    def claim_inputs(self):
        plan = plan_v2()
        target_hash = compute_target_hash(CANONICAL_URL)
        config_hash = compute_bilibili_api_config_hash(3)
        task_row = {
            "task_id": "task-shadow",
            "account_id": 7,
            "lottery_id": 11,
            "rule_snapshot_id": 101,
            "rule_hash": plan["rule_hash"],
            "action_plan_hash": plan["plan_hash"],
            "execution_path_id": "bilibili_api_v2",
            "execution_revision": "3",
            "target_hash": target_hash,
            "config_hash": config_hash,
            "account_lease_id": "lease-shadow",
            "account_lease_generation": 4,
            "reconciliation_required": 0,
        }
        lottery = {
            "platform": "bilibili",
            "raw_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "action_plan": plan,
            "authoritative_rule_snapshot_id": 101,
            "rule_hash": plan["rule_hash"],
            "action_plan_hash": plan["plan_hash"],
        }
        account = {"platform": "bilibili", "execution_revision": 3}
        message = {
            "platform": "bilibili",
            "raw_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "action_plan": plan,
            "rule_snapshot_id": "101",
            "rule_hash": plan["rule_hash"],
            "action_plan_hash": plan["plan_hash"],
            "execution_path_id": "bilibili_api_v2",
            "execution_revision": "3",
            "target_hash": target_hash,
            "config_hash": config_hash,
            "account_lease_id": "lease-shadow",
            "account_lease_generation": "4",
        }
        return message, task_row, lottery, account

    async def test_shadow_claim_binds_exact_plan_revision_target_and_lease(self):
        message, task_row, lottery, account = self.claim_inputs()

        await task_runner.validate_bilibili_api_shadow_claim(
            task_message=message,
            task_row=task_row,
            lottery=lottery,
            account=account,
        )

        message["config_hash"] = "0" * 64
        with self.assertRaisesRegex(
            task_runner.TaskClaimConflict, "shadow_task_config_hash_mismatch"
        ):
            await task_runner.validate_bilibili_api_shadow_claim(
                task_message=message,
                task_row=task_row,
                lottery=lottery,
                account=account,
            )

    async def test_shadow_claim_rejects_stale_or_parallel_lease(self):
        for field, value in (
            ("lease_latest_generation", 0),
            ("active_account_lease_count", 2),
            ("lease_active", 0),
        ):
            with self.subTest(field=field):
                message, task_row, lottery, account = self.claim_inputs()
                setattr(self.db, field, value)
                with self.assertRaisesRegex(
                    task_runner.TaskClaimConflict,
                    "shadow_task_account_lease_binding_invalid",
                ):
                    await task_runner.validate_bilibili_api_shadow_claim(
                        task_message=message,
                        task_row=task_row,
                        lottery=lottery,
                        account=account,
                    )
                setattr(self.db, field, 1)

    async def test_bilibili_shadow_is_get_only_and_never_uses_browser_or_screenshot(self):
        evidence = preflight_evidence()
        record = AsyncMock(return_value="event-1")
        with patch(
            "app.task_runner.run_readonly_api_preflight",
            AsyncMock(return_value=evidence),
        ) as preflight, patch("app.task_runner.record_event", record):
            screenshot = await task_runner.execute_shadow_run(
                self.task(), adapter=None, pool=PoolMustNotBeUsed()
            )

        self.assertIsNone(screenshot)
        self.assertEqual(self.db.observation_kind, API_PREFLIGHT_KIND)
        self.assertEqual(self.db.observation_hash, evidence.observation_hash)
        self.assertEqual(json.loads(self.db.observation), evidence.observation)
        self.assertEqual(self.db.phases, ["completed"])
        preflight.assert_awaited_once()
        self.assertTrue(record.await_args_list[0].kwargs["payload"]["side_effects"] is False)

    async def test_expired_shadow_lease_rejects_observation(self):
        self.db.lease_active = 0
        with patch(
            "app.task_runner.run_readonly_api_preflight",
            AsyncMock(return_value=preflight_evidence()),
        ):
            with self.assertRaises(task_runner.TaskOwnershipLost):
                await task_runner.execute_shadow_run(
                    self.task(), adapter=None, pool=PoolMustNotBeUsed()
                )
        self.assertIsNone(self.db.observation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
