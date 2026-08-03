import base64
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import orchestration  # noqa: E402


class OrchestrationApiQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_subject_comparison_uses_explicit_collation(self):
        target_rows = [
            {
                "id": 1,
                "platform": "xiaohongshu",
                "value_score": 50,
                "shadow_eligible": 0,
                "gate_ready": 0,
            }
        ]
        fetch_all = AsyncMock(return_value=target_rows)
        with (
            patch.object(
                orchestration.database,
                "fetch_one",
                AsyncMock(return_value={"version": 1}),
            ),
            patch.object(orchestration.database, "fetch_all", fetch_all),
        ):
            targets, gate_state = await orchestration._load_targets(None)

        sql = fetch_all.await_args.args[0]
        self.assertEqual(sql.count("COLLATE utf8mb4_0900_ai_ci"), 2)
        self.assertNotIn("subject_id COLLATE", sql)
        self.assertIn("CHARACTER SET utf8mb4", sql)
        self.assertEqual(targets[0]["lottery_id"], 1)
        self.assertFalse(gate_state[1]["gate_ready"])


if __name__ == "__main__":
    unittest.main()
