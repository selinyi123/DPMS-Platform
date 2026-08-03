import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app.action_plan import (  # noqa: E402
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_config_hash,
    compute_rule_hash,
    compute_target_hash,
)
from app.adapter_probe import (  # noqa: E402
    build_recommended_config,
    claim_probe,
    probe_loop,
    probe_phases_for_platform,
    summarize_probe_result,
)
from app.platform_modules.registry import get_platform_module  # noqa: E402


DYNAMIC_ID = "123456789012"
RAW_URL = f"https://t.bilibili.com/{DYNAMIC_ID}"
CANONICAL_URL = f"canonical://bilibili/dynamic/{DYNAMIC_ID}"
FOLLOW_HANDLE = "@ASUS华硕官方UP"
RULE_TEXT = "关注@ASUS华硕官方UP并点赞。"
RULE_HASH = compute_rule_hash(RULE_TEXT)


def plan_v2():
    plan = {
        "version": 2,
        "platform": "bilibili",
        "rule_snapshot_id": 101,
        "rule_hash": RULE_HASH,
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


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeProbeDatabase:
    def __init__(self):
        plan = plan_v2()
        self.row = {
            "probe_id": "probe-1",
            "platform": "bilibili",
            "account_id": 7,
            "lottery_id": 11,
            "target_url": RAW_URL,
            "status": "queued",
            "execution_path_id": "bilibili_api_v2",
            "rule_snapshot_id": 101,
            "target_hash": compute_target_hash(CANONICAL_URL),
            "rule_hash": RULE_HASH,
            "action_plan_hash": plan["plan_hash"],
            "config_hash": compute_bilibili_api_config_hash(3),
            "account_lease_id": "lease-probe",
            "account_lease_generation": 4,
            "lottery_platform": "bilibili",
            "lottery_raw_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "lottery_rule_text": RULE_TEXT,
            "lottery_action_plan": json.dumps(plan, ensure_ascii=False),
            "authoritative_rule_snapshot_id": 101,
            "lottery_rule_hash": RULE_HASH,
            "lottery_action_plan_hash": plan["plan_hash"],
            "snapshot_rule_text": RULE_TEXT,
            "snapshot_rule_hash": RULE_HASH,
            "snapshot_complete": 1,
            "snapshot_attested_by": "operator-1",
            "snapshot_attested_at": "2026-07-21T00:00:00Z",
            "account_platform": "bilibili",
            "account_status": "ready",
            "execution_revision": 3,
            "credential_present": 1,
            "lease_id": "lease-probe",
            "lease_generation": 4,
            "operation_kind": "adapter_probe",
            "owner_id": "probe-1",
            "lease_task_id": None,
            "lease_active": 1,
            "lease_unreleased": 1,
            "lease_latest_generation": 1,
            "active_account_lease_count": 1,
        }
        self.affected = 0
        self.executions = []

    def transaction(self):
        return Tx()

    async def fetch_one(self, query, values=None):
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        if "FROM adapter_calibrations ac" in query:
            return dict(self.row)
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        self.affected = 0
        if "SET status = 'running'" in query and self.row["status"] == "queued":
            self.row["status"] = "running"
            self.affected = 1


class ProbeLoopStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_loop_initializes_stream_without_browser_or_directory(self):
        class FakeRedis:
            def __init__(self):
                self.group_calls = []

            async def xgroup_create(self, *args, **kwargs):
                self.group_calls.append((args, kwargs))

        fake_redis = FakeRedis()
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        with patch("app.adapter_probe.redis", fake_redis):
            await probe_loop(object(), shutdown_event)
        self.assertEqual(len(fake_redis.group_calls), 0)


class AdapterProbeClaimTests(unittest.IsolatedAsyncioTestCase):
    def message(self):
        plan = plan_v2()
        return {
            "probe_id": "probe-1",
            "platform": "bilibili",
            "account_id": "7",
            "lottery_id": "11",
            "target_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "execution_path_id": "bilibili_api_v2",
            "rule_snapshot_id": "101",
            "target_hash": compute_target_hash(CANONICAL_URL),
            "rule_hash": RULE_HASH,
            "action_plan_hash": plan["plan_hash"],
            "config_hash": compute_bilibili_api_config_hash(3),
            "execution_revision": "3",
            "account_lease_id": "lease-probe",
            "account_lease_generation": "4",
        }

    async def test_exact_api_contract_and_active_lease_are_claimed(self):
        fake = FakeProbeDatabase()
        with patch("app.adapter_probe.database", fake):
            binding = await claim_probe(self.message())
        self.assertEqual(fake.row["status"], "running")
        self.assertEqual(binding["execution_revision"], 3)
        self.assertEqual(binding["action_plan"]["plan_hash"], plan_v2()["plan_hash"])

    async def test_tampered_message_revision_config_or_lease_is_rejected(self):
        for field, value, code in (
            ("execution_revision", "4", "adapter_probe_api_binding_mismatch"),
            ("config_hash", "0" * 64, "adapter_probe_api_binding_mismatch"),
            ("account_lease_generation", "5", "adapter_probe_account_lease_binding_invalid"),
        ):
            with self.subTest(field=field):
                fake = FakeProbeDatabase()
                message = self.message()
                message[field] = value
                with patch("app.adapter_probe.database", fake):
                    with self.assertRaisesRegex(ValueError, code):
                        await claim_probe(message)
                self.assertEqual(fake.row["status"], "queued")

    async def test_nonlatest_or_parallel_active_lease_is_rejected(self):
        for field, value in (
            ("lease_latest_generation", 0),
            ("active_account_lease_count", 2),
            ("lease_active", 0),
        ):
            with self.subTest(field=field):
                fake = FakeProbeDatabase()
                fake.row[field] = value
                with patch("app.adapter_probe.database", fake):
                    with self.assertRaisesRegex(
                        ValueError, "adapter_probe_account_lease_binding_invalid"
                    ):
                        await claim_probe(self.message())


class ManualAdapterProbeClaimTests(unittest.IsolatedAsyncioTestCase):
    PLATFORM = "douyin"
    DOUYIN_ID = "1234567890123456789"
    RAW_URL = f"https://www.douyin.com/video/{DOUYIN_ID}"
    CANONICAL_URL = f"canonical://douyin/video/{DOUYIN_ID}"
    SELECTORS = {
        "followed": ["button.follow"],
        "liked": ["button.like"],
        "commented": {"input": ["textarea"], "submit": ["button.publish"]},
        "favorited": {"done": ["[data-state='collected']"]},
        "reposted": {"done": ["[data-state='reposted']"]},
    }

    def database(self):
        fake = FakeProbeDatabase()
        fake.row.update(
            {
                "platform": self.PLATFORM,
                "target_url": self.RAW_URL,
                "execution_path_id": f"{self.PLATFORM}_selector_v1",
                "rule_snapshot_id": None,
                "target_hash": compute_target_hash(self.CANONICAL_URL),
                "rule_hash": None,
                "action_plan_hash": None,
                "config_hash": compute_config_hash(
                    {
                        "platform": self.PLATFORM,
                        "execution_revision": 3,
                        "selector_config": self.SELECTORS,
                    }
                ),
                "lottery_platform": self.PLATFORM,
                "lottery_raw_url": self.RAW_URL,
                "canonical_url": self.CANONICAL_URL,
                "account_platform": self.PLATFORM,
            }
        )
        original_fetch_one = fake.fetch_one

        async def fetch_one(query, values=None):
            if "FROM adapter_selector_configs" in query:
                return {"config_json": json.dumps(self.SELECTORS)}
            return await original_fetch_one(query, values)

        fake.fetch_one = fetch_one
        return fake

    def message(self):
        return {
            "probe_id": "probe-1",
            "platform": self.PLATFORM,
            "account_id": "7",
            "lottery_id": "11",
            "target_url": self.RAW_URL,
            "canonical_url": self.CANONICAL_URL,
            "execution_path_id": f"{self.PLATFORM}_selector_v1",
            "rule_snapshot_id": "",
            "target_hash": compute_target_hash(self.CANONICAL_URL),
            "rule_hash": "",
            "action_plan_hash": "",
            "config_hash": compute_config_hash(
                {
                    "platform": self.PLATFORM,
                    "execution_revision": 3,
                    "selector_config": self.SELECTORS,
                }
            ),
            "execution_revision": "3",
            "account_lease_id": "lease-probe",
            "account_lease_generation": "4",
        }

    async def test_claim_binds_database_selector_snapshot(self):
        fake = self.database()
        with patch("app.adapter_probe.database", fake):
            binding = await claim_probe(self.message())
        self.assertEqual(binding["selector_config"], self.SELECTORS)
        self.assertEqual(binding["execution_revision"], 3)

    async def test_changed_selector_snapshot_rejects_stale_probe(self):
        fake = self.database()
        stale = self.message()
        fake.row["config_hash"] = stale["config_hash"]
        changed = {**self.SELECTORS, "liked": ["button.like.changed"]}

        async def changed_fetch_one(query, values=None):
            if "FROM adapter_selector_configs" in query:
                return {"config_json": json.dumps(changed)}
            if "FROM adapter_calibrations ac" in query:
                return dict(fake.row)
            if "ROW_COUNT()" in query:
                return {"affected": fake.affected}
            raise AssertionError(f"unexpected query: {query}")

        fake.fetch_one = changed_fetch_one
        with patch("app.adapter_probe.database", fake):
            with self.assertRaisesRegex(ValueError, "adapter_probe_selector_binding_mismatch"):
                await claim_probe(stale)


class WeiboAdapterProbeClaimTests(ManualAdapterProbeClaimTests):
    PLATFORM = "weibo"
    RAW_URL = "https://weibo.com/123456/PCAGRFqKj"
    CANONICAL_URL = "canonical://weibo/status/PCAGRFqKj"
    SELECTORS = {
        "followed": ["button.follow"],
        "liked": ["button.like"],
        "commented": {"input": ["textarea"], "submit": ["button.publish"]},
        "reposted": ["button.repost"],
    }


class AdapterProbeSummaryTests(unittest.TestCase):
    @staticmethod
    def visible(selector: str) -> dict:
        return {"selector": selector, "visible": True, "count": 1, "error": None}

    def test_weibo_probe_uses_its_module_action_contract(self):
        result = {
            "followed": [self.visible("button.follow")],
            "liked": [self.visible("button.like")],
            "commented": [
                self.visible("textarea.comment"),
                self.visible("button.publish"),
            ],
            "favorited": [self.visible("button.favorite")],
            "reposted": [self.visible("button.repost")],
        }
        summary = summarize_probe_result("weibo", result)
        self.assertEqual(
            probe_phases_for_platform("weibo"),
            list(get_platform_module("weibo").action_order),
        )
        self.assertTrue(summary["selector_observation_complete"])
        self.assertFalse(summary["ready_for_real_actions"])
        self.assertFalse(summary["real_run_capable"])
        self.assertTrue(summary["manual_confirmation_required"])
        self.assertEqual(
            summary["capability_block_reason"],
            "weibo_selector_observation_only",
        )
        self.assertEqual(
            build_recommended_config("weibo", result)["weibo"]["favorited"],
            ["button.favorite"],
        )

    def test_weibo_probe_cannot_complete_without_favorite_observation(self):
        result = {
            "followed": [self.visible("button.follow")],
            "liked": [self.visible("button.like")],
            "commented": [
                self.visible("textarea.comment"),
                self.visible("button.publish"),
            ],
            "reposted": [self.visible("button.repost")],
        }

        summary = summarize_probe_result("weibo", result)

        self.assertFalse(summary["selector_observation_complete"])
        self.assertIn("favorited", summary["missing_phases"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
