import base64
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.real_run_readiness import action_plan_missing_rule_actions  # noqa: E402


class ActionPlanFreshnessTests(unittest.TestCase):
    def test_detects_saved_plan_missing_rule_required_actions(self):
        lottery = {
            "platform": "bilibili",
            "rule_text": "【转赞评】关注@COLG玩家社区 +@虹领金官方账号 6月30日抽一个小可爱送出【10元京东e卡】",
        }
        stale_plan = {"required_actions": ["followed", "reposted"], "review_required": False}

        self.assertEqual(action_plan_missing_rule_actions(lottery, stale_plan), ["liked", "commented"])

    def test_accepts_saved_plan_covering_current_rule(self):
        lottery = {
            "platform": "bilibili",
            "rule_text": "【转赞评】关注@COLG玩家社区 +@虹领金官方账号 6月30日抽一个小可爱送出【10元京东e卡】",
        }
        complete_plan = {
            "required_actions": ["followed", "liked", "commented", "reposted"],
            "review_required": False,
        }

        self.assertEqual(action_plan_missing_rule_actions(lottery, complete_plan), [])


if __name__ == "__main__":
    unittest.main()
