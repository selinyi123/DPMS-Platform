import base64
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import (  # noqa: E402
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
    DOUYIN_NO_OFFICIAL_API_BLOCKER,
    compute_action_plan_hash,
    compute_rule_hash,
)
from app.api.accounts import queue_account_calibration  # noqa: E402
from app.api.lotteries import update_lottery_action_plan  # noqa: E402
from app.models.schemas import LotteryActionPlanUpdate  # noqa: E402
from app.services import real_run_readiness  # noqa: E402
from app.services.real_run_readiness import (  # noqa: E402
    qualified_douyin_manual_shadow_observation,
    validate_real_run_evidence,
)
from shared.douyin_device_contract import (  # noqa: E402
    DOUYIN_DEVICE_CALIBRATION_CHECK_URL,
)


RULE_TEXT = (
    "抽奖福利：关注博主+点赞+评论+收藏本视频，"
    "评论带#夏日好物#并@品牌官方，抽2位粉丝"
)
NOTE_URL = "https://www.douyin.com/note/7659275356428852849"
CANONICAL_URL = "canonical://douyin/note/7659275356428852849"
REQUIRED_ACTIONS = ("followed", "liked", "commented", "favorited")


def complete_douyin_plan(snapshot_id=601):
    plan = {
        "version": 2,
        "platform": "douyin",
        "is_lottery": True,
        "required_actions": list(REQUIRED_ACTIONS),
        "action_payloads": {
            "followed": {"target_handle": "@抖音博主"},
            "liked": {},
            "commented": {
                "text": "#夏日好物# @品牌官方 参与抽奖",
                "topic_tags": ["#夏日好物#"],
                "mentions": ["@品牌官方"],
            },
            "favorited": {},
        },
        "content_requirements": {
            "follow_targets": ["@抖音博主"],
            "commented": {
                "topic_tags": ["#夏日好物#"],
                "mentions": ["@品牌官方"],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "execution_path_id": DOUYIN_MANUAL_EXECUTION_PATH,
        "rule_snapshot_id": snapshot_id,
        "rule_hash": compute_rule_hash(RULE_TEXT),
        "review_required": False,
        "executable": False,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [
            "topic_tag",
            "mention_account",
            "comment_content",
        ],
        "represented_requirements": [
            "topic_tag",
            "mention_account",
            "comment_content",
        ],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [DOUYIN_NO_OFFICIAL_API_BLOCKER],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def lottery_row(plan=None):
    plan = plan if plan is not None else complete_douyin_plan()
    return {
        "id": 51,
        "platform": "douyin",
        "source_type": "manual",
        "source_id": None,
        "raw_url": NOTE_URL,
        "canonical_url": CANONICAL_URL,
        "rule_text": RULE_TEXT,
        "status": "pending",
        "execution_lock": None,
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 601,
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
                "id": 601,
                "platform": "douyin",
                "rule_hash": compute_rule_hash(RULE_TEXT),
                "is_complete": 1,
                "attested_by": "operator-1",
                "attested_at": "2026-07-22 00:00:00",
            }
        return None

    async def execute(self, query, values=None):
        if "UPDATE lotteries" in query:
            self.saved_values = values
        return 1


class ReadinessDatabase:
    async def fetch_one(self, query, values=None):
        if "FROM lottery_rule_snapshots" in query:
            return {
                "id": 601,
                "platform": "douyin",
                "rule_hash": compute_rule_hash(RULE_TEXT),
                "is_complete": 1,
                "attested_by": "operator-1",
                "attested_at": "2026-07-22 00:00:00",
            }
        return None


class DouyinActionPlanApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_saves_variable_plan_with_device_default_path(self):
        database = ActionPlanDatabase()
        data = LotteryActionPlanUpdate(
            required_actions=["favorited", "commented", "followed", "liked"],
            reviewed=True,
            rule_complete_confirmed=True,
            action_payloads={
                "followed": {"target_handle": "@抖音博主"},
                "liked": {},
                "commented": {
                    "text": "#夏日好物# @品牌官方 参与抽奖",
                    "topic_tags": ["#夏日好物#"],
                    "mentions": ["@品牌官方"],
                },
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
            result = await update_lottery_action_plan(51, data, object())

        plan = result["action_plan"]
        self.assertEqual(list(REQUIRED_ACTIONS), plan["required_actions"])
        self.assertEqual(DOUYIN_DEVICE_EXECUTION_PATH, plan["execution_path_id"])
        self.assertTrue(plan["executable"])
        self.assertEqual([], plan["capability_blockers"])
        self.assertEqual(["@抖音博主"], plan["content_requirements"]["follow_targets"])
        self.assertEqual(
            plan,
            json.loads(database.saved_values["action_plan"]),
        )

    async def test_manual_authoring_and_readiness_share_the_same_blocker(self):
        database = ActionPlanDatabase()
        data = LotteryActionPlanUpdate(
            required_actions=list(REQUIRED_ACTIONS),
            reviewed=True,
            rule_complete_confirmed=True,
            execution_path_id=DOUYIN_MANUAL_EXECUTION_PATH,
            action_payloads=complete_douyin_plan()["action_payloads"],
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
            authored = await update_lottery_action_plan(51, data, object())

        plan = authored["action_plan"]
        self.assertFalse(plan["executable"])
        self.assertEqual(
            [DOUYIN_NO_OFFICIAL_API_BLOCKER], plan["capability_blockers"]
        )
        with patch.object(real_run_readiness, "database", ReadinessDatabase()):
            readiness = await validate_real_run_evidence(lottery_row(plan))
        self.assertTrue(readiness["action_plan_ready"])
        self.assertIn(DOUYIN_NO_OFFICIAL_API_BLOCKER, readiness["blockers"])
        self.assertNotIn(
            "action_plan_capability_binding_mismatch", readiness["blockers"]
        )


class DouyinDeviceAccountQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_account_queues_read_only_device_calibration(self):
        class QueueDatabase:
            def __init__(self):
                self.messages = []

            def transaction(self):
                return Transaction()

            async def fetch_one(self, _query, _values=None):
                return {"encrypted_credential": b"device-envelope"}

            async def execute(self, query, values=None):
                if "INSERT INTO account_calibrations" in query:
                    self.messages.append(dict(values or {}))
                return 1

        database = QueueDatabase()
        outbox = AsyncMock()
        with (
            patch("app.api.accounts.database", database),
            patch(
                "app.api.accounts.account_credential_kind",
                return_value="device_agent",
            ),
            patch(
                "app.api.accounts.enqueue_account_calibration_outbox",
                outbox,
            ),
        ):
            queued = await queue_account_calibration(7, "douyin")

        self.assertEqual("device_agent", queued["calibration_kind"])
        self.assertEqual(
            DOUYIN_DEVICE_CALIBRATION_CHECK_URL, queued["check_url"]
        )
        self.assertEqual(
            DOUYIN_DEVICE_CALIBRATION_CHECK_URL,
            database.messages[0]["check_url"],
        )
        self.assertEqual(
            "device_agent", outbox.await_args.args[0]["calibration_kind"]
        )


class DouyinReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewed_plan_is_ready_but_real_run_always_fails_closed(self):
        with patch.object(real_run_readiness, "database", ReadinessDatabase()):
            result = await validate_real_run_evidence(lottery_row())

        self.assertTrue(result["action_plan_ready"])
        self.assertFalse(result["allowed"])
        self.assertFalse(result["real_run_supported"])
        self.assertIn(DOUYIN_NO_OFFICIAL_API_BLOCKER, result["blockers"])

    async def test_legacy_review_flag_cannot_bypass_v2_manual_contract(self):
        legacy = {
            "required_actions": ["followed", "liked", "commented"],
            "review_required": False,
        }
        with patch.object(real_run_readiness, "database", ReadinessDatabase()):
            result = await validate_real_run_evidence(lottery_row(legacy))

        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_action_plan_v2_required", result["blockers"])
        self.assertIn("douyin_device_config_invalid", result["blockers"])
        self.assertIn("execution_account_scope_required", result["blockers"])


class DouyinManualObservationTests(unittest.TestCase):
    def observation(self):
        return {
            "side_effects": False,
            "qualified": False,
            "selector_observation_complete": True,
            "manual_confirmation_required": True,
            "real_run_capable": False,
            "capability_block_reason": DOUYIN_NO_OFFICIAL_API_BLOCKER,
            "required_phases": list(REQUIRED_ACTIONS),
            "visible_phases": {
                "followed": "button.follow",
                "liked": "button.like",
                "commented": {"input": "textarea", "submit": "button.send"},
                "favorited": "button.favorite",
            },
            "screenshot_path": "/profiles/shadow-runs/douyin.png",
        }

    def test_variable_phase_observation_is_accepted_without_real_ready_claim(self):
        self.assertTrue(
            qualified_douyin_manual_shadow_observation(
                self.observation(), REQUIRED_ACTIONS
            )
        )

    def test_share_cannot_substitute_for_required_favorite_observation(self):
        payload = self.observation()
        payload["visible_phases"].pop("favorited")
        payload["visible_phases"]["reposted"] = "button.share"

        self.assertFalse(
            qualified_douyin_manual_shadow_observation(payload, REQUIRED_ACTIONS)
        )


if __name__ == "__main__":
    unittest.main()
