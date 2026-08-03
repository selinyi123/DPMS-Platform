import base64
import os
import unittest


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.metrics import _intent_observation  # noqa: E402


class ExternalActionObservationTests(unittest.TestCase):
    def test_remote_reference_and_error_body_are_never_exposed(self):
        item = _intent_observation(
            {
                "intent_id": "intent-1",
                "payload_hash": "a" * 64,
                "remote_ref": "https://example.invalid/result?token=secret",
                "has_remote_ref": 1,
                "has_error": 1,
                "reconciliation_required": 1,
            }
        )

        self.assertNotIn("remote_ref", item)
        self.assertNotIn("error_message", item)
        self.assertTrue(item["remote_ref_redacted"])
        self.assertTrue(item["has_error"])
        self.assertTrue(item["reconciliation_required"])

    def test_absent_remote_reference_does_not_claim_redaction(self):
        item = _intent_observation(
            {
                "intent_id": "intent-2",
                "has_remote_ref": 0,
                "has_error": 0,
                "reconciliation_required": 0,
            }
        )

        self.assertFalse(item["remote_ref_redacted"])
        self.assertFalse(item["has_error"])
        self.assertFalse(item["reconciliation_required"])


if __name__ == "__main__":
    unittest.main()
