import base64
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bilibili.errors import Outcome, classify  # noqa: E402
from app.bilibili.runtime import (  # noqa: E402
    account_status_for_results,
    dpms_phases_to_api_actions,
    extract_bilibili_dynamic_id,
)
from app.utils.cookies import credential_to_cookie_header  # noqa: E402


class BilibiliRuntimeTests(unittest.TestCase):
    def test_extract_dynamic_id_from_supported_targets(self):
        self.assertEqual(extract_bilibili_dynamic_id("https://t.bilibili.com/123456789012"), "123456789012")
        self.assertEqual(extract_bilibili_dynamic_id("https://t.bilibili.com/opus/123456789012"), "123456789012")
        self.assertEqual(extract_bilibili_dynamic_id("https://www.bilibili.com/opus/123456789012"), "123456789012")
        self.assertEqual(extract_bilibili_dynamic_id("123456789012"), "123456789012")

    def test_rejects_non_dynamic_targets_for_api_real_run(self):
        with self.assertRaisesRegex(ValueError, "bilibili_dynamic_target_required"):
            extract_bilibili_dynamic_id("https://www.bilibili.com/video/BV1xx411c7mD")
        with self.assertRaisesRegex(ValueError, "bilibili_dynamic_target_required"):
            extract_bilibili_dynamic_id("https://b23.tv/abc123")

    def test_phase_mapping(self):
        self.assertEqual(
            dpms_phases_to_api_actions(["followed", "liked", "commented", "reposted"]),
            ["follow", "like", "comment", "repost"],
        )

    def test_cookie_header_from_json_and_raw_credentials(self):
        credential = json.dumps(
            [
                {"name": "SESSDATA", "value": "s1", "domain": ".bilibili.com"},
                {"name": "DedeUserID", "value": "42", "domain": ".bilibili.com"},
                {"name": "bili_jct", "value": "csrf", "domain": ".bilibili.com"},
            ]
        )
        header = credential_to_cookie_header(credential)
        self.assertIn("SESSDATA=s1", header)
        self.assertIn("DedeUserID=42", header)
        self.assertIn("bili_jct=csrf", header)
        self.assertEqual(credential_to_cookie_header("Cookie: SESSDATA=s1; bili_jct=csrf"), "SESSDATA=s1; bili_jct=csrf")

    def test_account_status_for_risk_results(self):
        self.assertEqual(account_status_for_results({"comment": classify("comment", 12015)}), ("cooling", "bilibili_comment_captcha"))
        self.assertEqual(account_status_for_results({"like": classify("like", -101)}), ("login_required", "bilibili_like_auth"))
        self.assertIsNone(account_status_for_results({"follow": classify("follow", 0)}))


if __name__ == "__main__":
    unittest.main()
