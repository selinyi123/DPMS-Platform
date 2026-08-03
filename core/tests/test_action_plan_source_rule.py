import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import (  # noqa: E402
    RealRunCompletionAuthority,
    protected_source_rule_text,
    update_lottery_action_plan,
)
from app.models.schemas import LotteryActionPlanUpdate  # noqa: E402


class RecordingTransaction:
    def __init__(self, database):
        self.database = database

    async def __aenter__(self):
        self.database.in_transaction = True
        self.database.calls.append(("transaction_enter",))

    async def __aexit__(self, exc_type, exc, traceback):
        self.database.calls.append(("transaction_exit", exc_type))
        self.database.in_transaction = False


class RecordingDatabase:
    def __init__(self, lottery):
        self.lottery = lottery
        self.in_transaction = False
        self.calls = []

    def transaction(self):
        return RecordingTransaction(self)

    async def fetch_one(self, query, values):
        self.calls.append(("fetch_one", query, values, self.in_transaction))
        return self.lottery

    async def execute(self, query, values):
        self.calls.append(("execute", query, values, self.in_transaction))
        return 1


class ProtectedSourceRuleTextTests(unittest.TestCase):
    def test_action_plan_review_is_opt_in(self):
        data = LotteryActionPlanUpdate(required_actions=["liked"])

        self.assertFalse(data.reviewed)

    def test_existing_source_rule_is_preserved_when_editor_omits_it(self):
        source = "带话题 #ASUS翻转夏日# 并@ASUS华硕官方UP 晒出照片+翻译，关注并转评赞"
        self.assertEqual(protected_source_rule_text(source, None), source)

    def test_existing_source_rule_cannot_be_replaced_by_simplified_summary(self):
        source = "带话题 #ASUS翻转夏日# 并@ASUS华硕官方UP 晒出照片+翻译，关注并转评赞"
        with self.assertRaises(HTTPException) as caught:
            protected_source_rule_text(source, "关注并转评赞")
        self.assertEqual(caught.exception.status_code, 409)

    def test_existing_source_rule_cannot_be_cleared(self):
        with self.assertRaises(HTTPException) as caught:
            protected_source_rule_text("关注并转评赞", "   ")
        self.assertEqual(caught.exception.status_code, 400)

    def test_empty_legacy_source_can_be_populated_once(self):
        self.assertEqual(
            protected_source_rule_text(None, "  关注并转评赞  "),
            "关注并转评赞",
        )

    def test_empty_source_cannot_be_reviewed_without_rule_text(self):
        with self.assertRaises(HTTPException) as caught:
            protected_source_rule_text(None, None)
        self.assertEqual(caught.exception.status_code, 400)


class ActionPlanUpdateTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_real_effects_block_plan_edit_before_snapshot_write(self):
        database = RecordingDatabase(
            {
                "id": 6,
                "platform": "bilibili",
                "rule_text": "点赞并评论",
                "status": "pending",
                "execution_lock": None,
                "current_intent_id": (
                    "00000000-0000-4000-8000-000000000001"
                ),
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["liked", "commented"],
            reviewed=True,
        )
        ensure_snapshot = AsyncMock()
        current_intent = object()

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.lotteries.load_real_run_completion_authority",
                new=AsyncMock(
                    return_value=RealRunCompletionAuthority(
                        completed_actions=("liked",),
                    )
                ),
            ) as load_authority,
            patch(
                "app.api.lotteries.load_lottery_execution_intent",
                new=AsyncMock(return_value=current_intent),
            ),
            patch(
                "app.api.lotteries.ensure_rule_snapshot",
                new=ensure_snapshot,
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await update_lottery_action_plan(6, data, object())

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "confirmed_real_actions_require_frozen_plan",
        )
        load_authority.assert_awaited_once_with(
            6,
            "bilibili",
            for_update=True,
            execution_intent=current_intent,
        )
        ensure_snapshot.assert_not_awaited()
        self.assertFalse(
            any(call[0] == "execute" for call in database.calls)
        )

    async def test_locked_read_and_update_share_one_transaction(self):
        database = RecordingDatabase(
            {
                "id": 7,
                "platform": "bilibili",
                "rule_text": "关注并转评赞",
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["followed", "liked", "commented", "reposted"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(7, data, object())

        fetch = next(call for call in database.calls if call[0] == "fetch_one")
        update = next(call for call in database.calls if call[0] == "execute")
        self.assertIn("FOR UPDATE", fetch[1].upper())
        self.assertTrue(fetch[3])
        self.assertTrue(update[3])
        self.assertEqual(database.calls[0][0], "transaction_enter")
        self.assertEqual(database.calls[-1][0], "transaction_exit")
        self.assertEqual(result["status"], "saved")

    async def test_missing_lottery_keeps_404_precedence_over_invalid_actions(self):
        database = RecordingDatabase(None)
        data = LotteryActionPlanUpdate(
            required_actions=["unsupported-action"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await update_lottery_action_plan(999, data, object())

        self.assertEqual(caught.exception.status_code, 404)
        fetch = next(call for call in database.calls if call[0] == "fetch_one")
        self.assertIn("FOR UPDATE", fetch[1].upper())
        self.assertTrue(fetch[3])
        self.assertFalse(any(call[0] == "execute" for call in database.calls))

    async def test_source_rule_protection_runs_after_locked_read(self):
        database = RecordingDatabase(
            {
                "id": 8,
                "platform": "bilibili",
                "rule_text": "带话题 #ASUS翻转夏日# 并@ASUS华硕官方UP 晒出照片+翻译，关注并转评赞",
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["followed", "liked", "commented", "reposted"],
            rule_text="关注并转评赞",
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await update_lottery_action_plan(8, data, object())

        self.assertEqual(caught.exception.status_code, 409)
        fetch = next(call for call in database.calls if call[0] == "fetch_one")
        self.assertIn("FOR UPDATE", fetch[1].upper())
        self.assertTrue(fetch[3])
        self.assertFalse(any(call[0] == "execute" for call in database.calls))
        self.assertEqual(database.calls[-1], ("transaction_exit", HTTPException))

    async def test_active_execution_cannot_change_action_plan(self):
        database = RecordingDatabase(
            {
                "id": 9,
                "platform": "bilibili",
                "rule_text": "关注并转评赞",
                "status": "running",
                "execution_lock": "task-active",
            }
        )
        data = LotteryActionPlanUpdate(required_actions=["liked"], reviewed=True)

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await update_lottery_action_plan(9, data, object())

        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(any(call[0] == "execute" for call in database.calls))

    async def test_operator_review_cannot_hide_unexpressed_content_requirements(self):
        source_rule = (
            "带话题 #ASUS翻转夏日#并@ASUS华硕官方UP 晒出你家‘踩稿官’的视频/照片+翻译，"
            "关注@ASUS华硕官方UP +转评赞本条动态"
        )
        database = RecordingDatabase(
            {
                "id": 10,
                "platform": "bilibili",
                "rule_text": source_rule,
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["followed", "liked", "commented", "reposted"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(10, data, object())

        plan = result["action_plan"]
        self.assertTrue(plan["review_required"])
        self.assertTrue(
            {"topic_tag", "mention_account", "media_submission", "translation_required"}
            .issubset(set(plan["unsupported_actions"]))
        )
        self.assertEqual(
            {
                "follow_targets": ["@ASUS华硕官方UP"],
                "commented": {
                    "topic_tags": ["#ASUS翻转夏日#"],
                    "mentions": ["@ASUS华硕官方UP"],
                },
                "reposted": {"topic_tags": [], "mentions": []},
            },
            plan["content_requirements"],
        )

    async def test_operator_review_cannot_omit_actions_detected_in_source_rule(self):
        database = RecordingDatabase(
            {
                "id": 11,
                "platform": "bilibili",
                "rule_text": "抽奖：关注并转评赞本条动态",
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["liked"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(11, data, object())

        self.assertEqual(["liked"], result["action_plan"]["required_actions"])
        self.assertTrue(result["action_plan"]["review_required"])
        self.assertEqual(0.5, result["action_plan"]["confidence"])

    async def test_operator_review_cannot_invent_actions_for_rule_without_detected_action(self):
        database = RecordingDatabase(
            {
                "id": 12,
                "platform": "bilibili",
                "rule_text": "抽奖送键盘，参与方式请查看图片",
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["liked"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(12, data, object())

        self.assertTrue(result["action_plan"]["review_required"])
        self.assertEqual(0.5, result["action_plan"]["confidence"])

    async def test_operator_review_cannot_add_side_effects_not_required_by_rule(self):
        database = RecordingDatabase(
            {
                "id": 13,
                "platform": "bilibili",
                "rule_text": "抽奖：点赞本条动态",
                "status": "pending",
                "execution_lock": None,
            }
        )
        data = LotteryActionPlanUpdate(
            required_actions=["liked", "followed"],
            reviewed=True,
        )

        with (
            patch("app.api.lotteries.database", database),
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch("app.api.lotteries.audit_event", new=AsyncMock()),
            patch("app.api.lotteries.record_event", new=AsyncMock()),
        ):
            result = await update_lottery_action_plan(13, data, object())

        self.assertEqual(["followed", "liked"], result["action_plan"]["required_actions"])
        self.assertTrue(result["action_plan"]["review_required"])
        self.assertEqual(0.5, result["action_plan"]["confidence"])


if __name__ == "__main__":
    unittest.main()
