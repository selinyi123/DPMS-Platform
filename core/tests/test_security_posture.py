import base64
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.security_posture import (  # noqa: E402
    DEFAULT_ADMIN_TOKEN,
    DEFAULT_UPDATE_SECRET,
    secret_posture,
)
from app.utils.log import _redact_extra  # noqa: E402


GOOD_KEY = base64.b64encode(b"\x01" * 32).decode()


class SecretPostureTests(unittest.TestCase):
    def test_strong_secrets_have_no_problems(self):
        problems = secret_posture(
            admin_token="a-strong-admin-token-value",
            update_secret="a-strong-update-secret-value",
            encryption_key=GOOD_KEY,
            database_url="mysql+aiomysql://app:S3cret@mysql:3306/lottery",
        )
        self.assertEqual(problems, [])

    def test_default_admin_token_flagged(self):
        problems = secret_posture(
            admin_token=DEFAULT_ADMIN_TOKEN,
            update_secret="a-strong-update-secret-value",
            encryption_key=GOOD_KEY,
        )
        self.assertTrue(any(p["key"] == "ADMIN_TOKEN" for p in problems))

    def test_default_update_secret_flagged(self):
        problems = secret_posture(
            admin_token="a-strong-admin-token-value",
            update_secret=DEFAULT_UPDATE_SECRET,
            encryption_key=GOOD_KEY,
        )
        self.assertTrue(any(p["key"] == "UPDATE_SECRET" for p in problems))

    def test_short_token_flagged(self):
        problems = secret_posture(
            admin_token="short",
            update_secret="a-strong-update-secret-value",
            encryption_key=GOOD_KEY,
        )
        self.assertTrue(any(p["key"] == "ADMIN_TOKEN" for p in problems))

    def test_missing_encryption_key_flagged(self):
        problems = secret_posture(
            admin_token="a-strong-admin-token-value",
            update_secret="a-strong-update-secret-value",
            encryption_key="",
        )
        self.assertTrue(any(p["key"] == "ENCRYPTION_KEY" for p in problems))

    def test_wrong_length_encryption_key_flagged(self):
        problems = secret_posture(
            admin_token="a-strong-admin-token-value",
            update_secret="a-strong-update-secret-value",
            encryption_key=base64.b64encode(b"short").decode(),
        )
        self.assertTrue(any(p["key"] == "ENCRYPTION_KEY" for p in problems))

    def test_default_db_password_flagged(self):
        problems = secret_posture(
            admin_token="a-strong-admin-token-value",
            update_secret="a-strong-update-secret-value",
            encryption_key=GOOD_KEY,
            database_url="mysql+aiomysql://user:password@mysql:3306/lottery",
        )
        self.assertTrue(any(p["key"] == "DATABASE_URL" for p in problems))


class LogRedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_redacted(self):
        for key in ("token", "admin_token", "cookie", "credential", "password",
                    "feishu_webhook", "encryption_key", "signature", "authorization"):
            self.assertEqual(_redact_extra(key, "supersecret"), "<redacted>", key)

    def test_ordinary_keys_pass_through(self):
        self.assertEqual(_redact_extra("platform", "bilibili"), "bilibili")
        self.assertEqual(_redact_extra("task_id", "abc"), "abc")
        self.assertEqual(_redact_extra("count", 5), "5")


if __name__ == "__main__":
    unittest.main()
