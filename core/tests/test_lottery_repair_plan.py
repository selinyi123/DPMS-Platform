import base64
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import lotteries  # noqa: E402
from app.api.lotteries import missing_repair_actions, normalize_action_ledger_row, ordered_actions  # noqa: E402
from app.platform_modules import get_platform_module  # noqa: E402


class LotteryRepairPlanTests(unittest.TestCase):
    def test_ordered_actions_keeps_canonical_phase_order(self):
        self.assertEqual(
            ordered_actions(
                "bilibili",
                ["commented", "followed", "reposted", "liked", "unsupported"],
            ),
            ["followed", "liked", "commented", "reposted"],
        )

    def test_missing_repair_actions_only_returns_unfinished_required_actions(self):
        required = ["followed", "liked", "commented", "reposted"]
        completed = ["followed", "reposted"]

        self.assertEqual(
            missing_repair_actions("bilibili", required, completed),
            ["liked", "commented"],
        )

    def test_missing_repair_actions_never_adds_unrequired_completed_actions(self):
        required = ["liked", "commented"]
        completed = ["followed", "reposted"]

        self.assertEqual(
            missing_repair_actions("bilibili", required, completed),
            ["liked", "commented"],
        )

    def test_no_missing_actions_when_required_actions_are_complete(self):
        required = ["followed", "liked"]
        completed = ["liked", "followed", "reposted"]

        self.assertEqual(
            missing_repair_actions("bilibili", required, completed),
            [],
        )

    def test_one_platform_action_evolution_does_not_change_another_order(self):
        weibo = get_platform_module("weibo")
        evolved_weibo = replace(
            weibo,
            action_order=(*weibo.action_order, "bookmarked"),
        )

        def module_for(platform):
            return evolved_weibo if platform == "weibo" else get_platform_module(platform)

        with patch.object(lotteries, "get_platform_module", side_effect=module_for):
            self.assertEqual(
                ["liked", "bookmarked"],
                ordered_actions("weibo", ["bookmarked", "liked"]),
            )
            self.assertEqual(
                ["liked"],
                ordered_actions("bilibili", ["bookmarked", "liked"]),
            )

    def test_action_ledger_row_normalizes_ok_to_boolean(self):
        row = {
            "task_id": "task-1",
            "action": "like",
            "phase": "liked",
            "outcome": "ok",
            "ok": 1,
        }

        normalized = normalize_action_ledger_row(row)

        self.assertIs(normalized["ok"], True)
        self.assertEqual(normalized["phase"], "liked")


if __name__ == "__main__":
    unittest.main()
