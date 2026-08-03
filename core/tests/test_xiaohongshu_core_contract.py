import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import (  # noqa: E402
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
    compute_action_plan_hash,
    compute_rule_hash,
)
from app.platform_modules.catalog import (  # noqa: E402
    XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER,
)
from app.api.lotteries import (  # noqa: E402
    dispatch_lottery,
    update_lottery_action_plan,
    xiaohongshu_manual_plan_binding,
)
from app.models.schemas import (  # noqa: E402
    DispatchTaskRequest,
    LotteryActionPlanUpdate,
)
from app.services import real_run_readiness  # noqa: E402
from app.services.real_run_readiness import (  # noqa: E402
    qualified_xiaohongshu_manual_shadow_observation,
    real_run_gate_status,
    validate_real_run_evidence,
)


RULE_TEXT = "抽奖：关注博主、点赞、评论并收藏本篇笔记"
NOTE_URL = "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1"
CANONICAL_URL = "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1"


def complete_xiaohongshu_plan(snapshot_id=501):
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "is_lottery": True,
        "required_actions": list(XIAOHONGSHU_ACTION_ORDER),
        "action_payloads": {
            "followed": {"target_handle": "@小红书博主"},
            "liked": {},
            "commented": {"text": "已认真阅读，参与抽奖"},
            "favorited": {},
        },
        "content_requirements": {
            "follow_targets": ["@小红书博主"],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        "rule_snapshot_id": snapshot_id,
        "rule_hash": compute_rule_hash(RULE_TEXT),
        "review_required": False,
        "executable": False,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def lottery_row(plan=None):
    plan = plan if plan is not None else complete_xiaohongshu_plan()
    return {
        "id": 41,
        "platform": "xiaohongshu",
        "source_type": "manual",
        "source_id": None,
        "raw_url": NOTE_URL,
        "canonical_url": CANONICAL_URL,
        "rule_text": RULE_TEXT,
        "status": "pending",
        "execution_lock": None,
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 501,
        "rule_hash": compute_rule_hash(RULE_TEXT),
        "action_plan_hash": plan.get("plan_hash") if isinstance(plan, dict) else None,
    }


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ActionPlanDatabase:
    def __init__(self):
        self.saved_values = None

    def transaction(self):
        return Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM lotteries" in query:
            return lottery_row(plan={})
        if "FROM lottery_rule_snapshots" in query:
            return {
                "id": 501,
                "rule_hash": compute_rule_hash(RULE_TEXT),
                "is_complete": 1,
                "attested_by": "operator-1",
                "attested_at": "2026-07-21 00:00:00",
            }
        return None

    async def execute(self, query, values=None):
        if "UPDATE lotteries" in query:
            self.saved_values = values
        return 1


class ReadinessDatabase:
    def __init__(self):
        self.queries = []

    async def fetch_one(self, query, values=None):
        self.queries.append(query)
        if "FROM lottery_rule_snapshots" in query:
            return {
                "id": 501,
                "platform": "xiaohongshu",
                "rule_hash": compute_rule_hash(RULE_TEXT),
                "is_complete": 1,
                "attested_by": "operator-1",
                "attested_at": "2026-07-21 00:00:00",
            }
        return None


class XiaohongshuActionPlanApiTests(unittest.IsolatedAsyncioTestCase):
    def test_request_model_keeps_legacy_bilibili_default_visible_to_clients(self):
        data = LotteryActionPlanUpdate(required_actions=[])

        self.assertEqual("bilibili_api_v2", data.execution_path_id)
        self.assertNotIn("execution_path_id", data.model_fields_set)

    async def test_review_defaults_to_exact_executable_browser_plan(self):
        database = ActionPlanDatabase()
        data = LotteryActionPlanUpdate(
            required_actions=["commented", "favorited", "followed", "liked"],
            reviewed=True,
            rule_complete_confirmed=True,
            action_payloads={
                "followed": {"target_handle": "@小红书博主"},
                "liked": {},
                "commented": {"text": "已认真阅读，参与抽奖"},
                "favorited": {},
            },
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries._record_post_commit_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(41, data, object())

        plan = result["action_plan"]
        self.assertEqual(list(XIAOHONGSHU_ACTION_ORDER), plan["required_actions"])
        self.assertEqual(
            XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            plan["execution_path_id"],
        )
        self.assertEqual(
            "已认真阅读，参与抽奖",
            plan["action_payloads"]["commented"]["text"],
        )
        self.assertEqual(
            ["@小红书博主"],
            plan["content_requirements"]["follow_targets"],
        )
        self.assertFalse(plan["review_required"])
        self.assertTrue(plan["executable"])
        self.assertEqual([], plan["capability_blockers"])

    async def test_api_authored_manual_fallback_is_shadow_review_ready(self):
        database = ActionPlanDatabase()
        data = LotteryActionPlanUpdate(
            required_actions=list(XIAOHONGSHU_ACTION_ORDER),
            reviewed=True,
            rule_complete_confirmed=True,
            execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            action_payloads={
                "followed": {"target_handle": "@creator"},
                "liked": {},
                "commented": {"text": "exact comment"},
                "favorited": {},
            },
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries._record_post_commit_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(41, data, object())

        plan = result["action_plan"]
        self.assertFalse(plan["executable"])
        self.assertEqual(
            [XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER],
            plan["capability_blockers"],
        )
        readiness_database = ReadinessDatabase()
        with patch.object(real_run_readiness, "database", readiness_database):
            readiness = await validate_real_run_evidence(lottery_row(plan))
        self.assertTrue(readiness["action_plan_ready"])
        self.assertEqual("manual_assisted", readiness["execution_mode"])

    async def test_comment_presence_without_exact_text_cannot_be_reviewed(self):
        database = ActionPlanDatabase()
        data = LotteryActionPlanUpdate(
            required_actions=list(XIAOHONGSHU_ACTION_ORDER),
            reviewed=True,
            rule_complete_confirmed=True,
            action_payloads={
                "followed": {"target_handle": "@小红书博主"},
                "liked": {},
                "commented": {},
                "favorited": {},
            },
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries._record_post_commit_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(41, data, object())

        self.assertTrue(result["action_plan"]["review_required"])
        self.assertIn(
            "action_payload_commented_text_required",
            result["action_plan"]["payload_validation_errors"],
        )


class XiaohongshuManualPlanBindingTests(unittest.TestCase):
    def assert_binding_rejected(self, plan, expected_blocker):
        with self.assertRaises(HTTPException) as caught:
            xiaohongshu_manual_plan_binding(
                lottery_row(plan),
                execution_revision=7,
                selector_config={},
            )

        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            [expected_blocker],
            caught.exception.detail["blockers"],
        )

    def test_binding_rejects_executable_claim(self):
        plan = complete_xiaohongshu_plan()
        plan["executable"] = True
        plan["plan_hash"] = compute_action_plan_hash(plan)

        self.assert_binding_rejected(
            plan,
            "xiaohongshu_manual_plan_must_be_non_executable",
        )

    def test_binding_rejects_non_manual_path(self):
        plan = complete_xiaohongshu_plan()
        plan["execution_path_id"] = "xiaohongshu_selector_v1"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        self.assert_binding_rejected(
            plan,
            "xiaohongshu_execution_path_not_supported",
        )

    def test_binding_rejects_nonempty_legacy_repost_bucket(self):
        plan = complete_xiaohongshu_plan()
        plan["content_requirements"]["reposted"]["mentions"] = ["@好友"]
        plan["plan_hash"] = compute_action_plan_hash(plan)

        self.assert_binding_rejected(
            plan,
            "xiaohongshu_repost_content_not_supported",
        )


class XiaohongshuReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewed_manual_plan_is_ready_but_real_run_is_always_blocked(self):
        database = ReadinessDatabase()
        with patch.object(real_run_readiness, "database", database):
            result = await validate_real_run_evidence(lottery_row())

        self.assertTrue(result["action_plan_ready"])
        self.assertFalse(result["allowed"])
        self.assertIn(XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER, result["blockers"])
        self.assertEqual("manual_assisted", result["execution_mode"])
        self.assertTrue(result["manual_shadow_supported"])
        self.assertTrue(result["manual_confirmation_required"])
        self.assertFalse(result["real_run_supported"])

    async def test_legacy_plan_fails_closed(self):
        legacy = {
            "required_actions": list(XIAOHONGSHU_ACTION_ORDER),
            "review_required": False,
        }
        database = ReadinessDatabase()
        with patch.object(real_run_readiness, "database", database):
            result = await validate_real_run_evidence(lottery_row(legacy))

        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_action_plan_v2_required", result["blockers"])
        self.assertIn(
            "xiaohongshu_selector_config_incomplete",
            result["blockers"],
        )

    async def test_manual_plan_claiming_executable_fails_closed(self):
        plan = complete_xiaohongshu_plan()
        plan["executable"] = True
        plan["plan_hash"] = compute_action_plan_hash(plan)
        database = ReadinessDatabase()
        with patch.object(real_run_readiness, "database", database):
            result = await validate_real_run_evidence(lottery_row(plan))

        self.assertFalse(result["action_plan_ready"])
        self.assertFalse(result["allowed"])
        self.assertIn(
            "xiaohongshu_manual_plan_must_be_non_executable",
            result["blockers"],
        )

    async def test_real_selector_config_cannot_promote_a_manual_plan(self):
        selector_config = {
            "xiaohongshu": {
                "followed": {"click": ["f"], "done": ["fd"]},
                "liked": {"click": ["l"], "done": ["ld"]},
                "favorited": {"click": ["r"], "done": ["rd"]},
                "commented": {
                    "input": ["c"],
                    "submit": ["s"],
                    "done": ["cd"],
                },
            }
        }
        database = ReadinessDatabase()
        with patch.object(real_run_readiness, "database", database):
            result = await real_run_gate_status(
                lottery_row(),
                selector_config=selector_config,
                real_run_enabled=True,
                account_summary={
                    "ready_accounts": 1,
                    "runnable_accounts": 1,
                    "latest_recent_risk": None,
                },
            )

        self.assertTrue(result["adapter_enabled"])
        self.assertEqual("selector", result["adapter_kind"])
        self.assertFalse(result["allowed"])
        self.assertEqual("manual_assisted", result["next_action"])
        self.assertNotIn("real_adapter_not_enabled", result["blockers"])
        self.assertIn(XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER, result["blockers"])


class XiaohongshuManualShadowObservationTests(unittest.TestCase):
    def valid_observation(self):
        return {
            "qualified": False,
            "side_effects": False,
            "selector_observation_complete": True,
            "manual_confirmation_required": True,
            "real_run_capable": False,
            "capability_block_reason": XIAOHONGSHU_NO_OFFICIAL_API_BLOCKER,
            "required_phases": list(XIAOHONGSHU_ACTION_ORDER),
            "visible_phases": {
                "followed": True,
                "liked": True,
                "commented": {"input": True, "submit": True},
                "favorited": True,
            },
            "screenshot_path": "/profiles/shadow-runs/xhs.png",
        }

    def test_manual_shadow_contract_accepts_only_explicit_non_real_evidence(self):
        self.assertTrue(
            qualified_xiaohongshu_manual_shadow_observation(
                self.valid_observation()
            )
        )

    def test_manual_shadow_contract_rejects_real_capability_claim(self):
        observation = self.valid_observation()
        observation["real_run_capable"] = True

        self.assertFalse(
            qualified_xiaohongshu_manual_shadow_observation(observation)
        )

    def test_manual_shadow_contract_rejects_qualified_claim(self):
        observation = self.valid_observation()
        observation["qualified"] = True

        self.assertFalse(
            qualified_xiaohongshu_manual_shadow_observation(observation)
        )

    def test_manual_shadow_contract_rejects_capability_reason_mismatch(self):
        observation = self.valid_observation()
        observation["capability_block_reason"] = "selector_configured"

        self.assertFalse(
            qualified_xiaohongshu_manual_shadow_observation(observation)
        )

    def test_manual_shadow_contract_rejects_missing_favorite_phase(self):
        observation = self.valid_observation()
        observation["visible_phases"].pop("favorited")

        self.assertFalse(
            qualified_xiaohongshu_manual_shadow_observation(observation)
        )


class XiaohongshuDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_is_rejected_without_dropping_favorite(self):
        database = AsyncMock()
        database.fetch_one.return_value = lottery_row()
        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.lotteries.load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery(
                    41, DispatchTaskRequest(dry_run=True), object()
                )

        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            ["xiaohongshu_manual_shadow_only"],
            caught.exception.detail["blockers"],
        )

    async def test_real_run_is_rejected_by_capability_before_runtime_switch(self):
        database = AsyncMock()
        database.fetch_one.return_value = lottery_row()
        runtime_switch = AsyncMock(return_value=True)
        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "admin-1"},
            ),
            patch(
                "app.api.lotteries.load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
            patch("app.api.lotteries.is_real_run_enabled", runtime_switch),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries._record_post_commit_event", new=AsyncMock()),
            patch(
                "app.api.lotteries.emit_real_run_gate_notification",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery(
                    41,
                    DispatchTaskRequest(
                        mode="real_run", dry_run=False, confirm=True
                    ),
                    object(),
                )

        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(
            ["xiaohongshu_manual_execution_selected"],
            caught.exception.detail["blockers"],
        )
        runtime_switch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
