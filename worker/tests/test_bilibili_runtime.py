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
        self.assertEqual(
            extract_bilibili_dynamic_id(
                "canonical://bilibili/dynamic/opus_123456789012"
            ),
            "123456789012",
        )

    def test_rejects_non_dynamic_targets_for_api_real_run(self):
        for target in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://b23.tv/abc123",
            "http://t.bilibili.com/123456789012",
            "https://t.bilibili.com:444/123456789012",
            "https://t.bilibili.com:443@evil.example/123456789012",
            "canonical://bilibili:123/dynamic/123456789012",
            "canonical://bilibili:not-a-port/dynamic/123456789012",
            "canonical://operator@bilibili/dynamic/123456789012",
            "https://t.bilibili.com/" + "\uff11" * 12,
            "https://t.bilibili.com/" + "1" * 21,
            "\uff11" * 12,
            "1" * 21,
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "bilibili_dynamic_target_required"):
                    extract_bilibili_dynamic_id(target)

    def test_dynamic_id_extraction_fails_closed_for_malformed_canonical_authority(self):
        for target in (
            "canonical://[bilibili/dynamic/123456789012",
            "canonical://bilibili／evil/dynamic/123456789012",
            "canonical://bilibili：443/dynamic/123456789012",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    ValueError, "bilibili_dynamic_target_required"
                ):
                    extract_bilibili_dynamic_id(target)

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

    def test_cookie_header_rejects_duplicate_json_cookie_names(self):
        credential = json.dumps(
            [
                {"name": "DedeUserID", "value": "42"},
                {"name": "DedeUserID", "value": "84"},
            ]
        )
        with self.assertRaisesRegex(
            ValueError,
            "Duplicate cookie name.*DedeUserID",
        ):
            credential_to_cookie_header(credential)

    def test_cookie_header_keeps_optional_duplicate_names_compatible(self):
        credential = json.dumps(
            [
                {"name": "buvid3", "value": "host-a"},
                {"name": "buvid3", "value": "host-b"},
            ]
        )
        self.assertEqual(
            credential_to_cookie_header(credential),
            "buvid3=host-a; buvid3=host-b",
        )

    def test_account_status_for_risk_results(self):
        self.assertEqual(account_status_for_results({"comment": classify("comment", 12015)}), ("cooling", "bilibili_comment_captcha"))
        self.assertEqual(account_status_for_results({"like": classify("like", -101)}), ("login_required", "bilibili_like_auth"))
        self.assertIsNone(account_status_for_results({"follow": classify("follow", 0)}))


if __name__ == "__main__":
    unittest.main()
