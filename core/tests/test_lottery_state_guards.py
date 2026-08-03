import base64
import os
import unittest

from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import (  # noqa: E402
    RealRunCompletionAuthority,
    require_action_plan_mutation_safe,
    require_dispatch_snapshot_unchanged,
    require_dispatchable_lottery_state,
    require_lottery_not_executing,
    require_no_completed_actions_for_full_real_dispatch,
    require_repair_plan_unchanged,
)


class LotteryStateGuardTests(unittest.TestCase):
    def test_pending_unlocked_lottery_is_dispatchable(self):
        require_dispatchable_lottery_state({"status": "pending", "execution_lock": None})

    def test_terminal_lottery_cannot_be_fully_dispatched_again(self):
        with self.assertRaises(HTTPException) as caught:
            require_dispatchable_lottery_state({"status": "won", "execution_lock": None})
        self.assertEqual(caught.exception.status_code, 409)

    def test_any_execution_lock_blocks_dispatch_even_with_terminal_status(self):
        with self.assertRaises(HTTPException) as caught:
            require_dispatchable_lottery_state(
                {"status": "won", "execution_lock": "unknown-outcome-task"}
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_unknown_outcome_lock_blocks_manual_result(self):
        with self.assertRaises(HTTPException) as caught:
            require_lottery_not_executing(
                {"status": "running", "execution_lock": "unknown-outcome-task"},
                operation="record a result",
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_terminal_unlocked_lottery_allows_manual_result_correction(self):
        require_lottery_not_executing(
            {"status": "participated", "execution_lock": None},
            operation="record a result",
        )

    def test_dispatch_snapshot_rejects_action_plan_drift(self):
        before = {
            "platform": "bilibili",
            "raw_url": "https://t.bilibili.com/1",
            "canonical_url": "canonical://bilibili/dynamic/1",
            "rule_text": "关注并转评赞",
            "action_plan": '{"required_actions":["liked"]}',
        }
        locked = {**before, "action_plan": '{"required_actions":["commented"]}'}
        with self.assertRaises(HTTPException) as caught:
            require_dispatch_snapshot_unchanged(locked, before)
        self.assertEqual(caught.exception.status_code, 409)

    def test_confirmed_partial_actions_force_repair_instead_of_full_replay(self):
        with self.assertRaises(HTTPException) as caught:
            require_no_completed_actions_for_full_real_dispatch(["liked", "commented"])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["completed_actions"], ["liked", "commented"])

    def test_confirmed_partial_actions_freeze_mutable_action_plan(self):
        with self.assertRaises(HTTPException) as caught:
            require_action_plan_mutation_safe(
                RealRunCompletionAuthority(
                    completed_actions=("liked",),
                )
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "confirmed_real_actions_require_frozen_plan",
        )
        self.assertEqual(
            caught.exception.detail["completed_actions"],
            ["liked"],
        )

    def test_repair_preflight_rejects_newly_completed_action(self):
        before = {
            "eligible": True,
            "repair_action_plan": {"required_actions": ["commented", "reposted"]},
        }
        current = {
            "eligible": True,
            "repair_action_plan": {"required_actions": ["reposted"]},
        }
        with self.assertRaises(HTTPException) as caught:
            require_repair_plan_unchanged(current, before)
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
