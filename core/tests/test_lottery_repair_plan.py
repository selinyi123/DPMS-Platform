import base64
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import missing_repair_actions, ordered_actions  # noqa: E402


class LotteryRepairPlanTests(unittest.TestCase):
    def test_ordered_actions_keeps_canonical_phase_order(self):
        self.assertEqual(
            ordered_actions(["commented", "followed", "reposted", "liked", "unsupported"]),
            ["followed", "liked", "commented", "reposted"],
        )

    def test_missing_repair_actions_only_returns_unfinished_required_actions(self):
        required = ["followed", "liked", "commented", "reposted"]
        completed = ["followed", "reposted"]

        self.assertEqual(missing_repair_actions(required, completed), ["liked", "commented"])

    def test_missing_repair_actions_never_adds_unrequired_completed_actions(self):
        required = ["liked", "commented"]
        completed = ["followed", "reposted"]

        self.assertEqual(missing_repair_actions(required, completed), ["liked", "commented"])

    def test_no_missing_actions_when_required_actions_are_complete(self):
        required = ["followed", "liked"]
        completed = ["liked", "followed", "reposted"]

        self.assertEqual(missing_repair_actions(required, completed), [])


if __name__ == "__main__":
    unittest.main()
