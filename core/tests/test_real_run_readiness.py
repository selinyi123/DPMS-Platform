import base64
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.real_run_readiness import account_risk_payload, action_plan_missing_rule_actions  # noqa: E402


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


class AccountRiskPayloadTests(unittest.TestCase):
    def test_empty_risk_payload_is_not_recent_risk(self):
        self.assertEqual(
            account_risk_payload(None),
            {"has_recent_risk": False, "cooldown_hours": 24},
        )

    def test_risk_payload_exposes_latest_event_and_cooldown_until(self):
        row = {
            "id": 4,
            "account_id": 14,
            "event_type": "cooling",
            "detail": '{"reason":"action_window"}',
            "created_at": "2026-06-24 06:16:36",
            "cooldown_until": "2026-06-25 06:16:36",
        }

        payload = account_risk_payload(row)

        self.assertTrue(payload["has_recent_risk"])
        self.assertEqual(payload["latest_event"]["account_id"], 14)
        self.assertEqual(payload["latest_event"]["detail"], {"reason": "action_window"})
        self.assertEqual(payload["latest_event"]["created_at"], "2026-06-24T06:16:36")
        self.assertEqual(payload["cooldown_until"], "2026-06-25T06:16:36")


if __name__ == "__main__":
    unittest.main()
