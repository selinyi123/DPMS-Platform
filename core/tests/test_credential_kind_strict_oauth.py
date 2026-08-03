import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.utils import credential_kind


OAUTH_PAYLOAD = json.dumps(
    {
        "credential_kind": "weibo_oauth",
        "access_token": "safe-placeholder-token",
        "uid": "1234567890",
        "expires_at": "2099-01-01T00:00:00Z",
    }
)
BROWSER_PAYLOAD = json.dumps([{"name": "SUB", "value": "legacy-session"}])
BILIBILI_PAYLOAD = json.dumps(
    [
        {"name": "SESSDATA", "value": "session"},
        {"name": "DedeUserID", "value": "24680"},
    ]
)
DEVICE_PAYLOAD = json.dumps(
    {
        "contract_version": 1,
        "credential_kind": "device_agent",
        "device_agent": {
            "agent_id": "a" * 64,
            "manifest_sha256": "b" * 64,
            "device_serial_sha256": "c" * 64,
            "account_id_sha256": "d" * 64,
        },
    }
)


class FakeVault:
    def __init__(self, *, strict_values=None, compatible_values=None):
        self.strict_values = dict(strict_values or {})
        self.compatible_values = dict(compatible_values or {})

    def decrypt_strict(self, value, *, aad):
        if value not in self.strict_values:
            raise ValueError("aad_binding_invalid")
        return self.strict_values[value]

    def decrypt(self, value, *, aad):
        if value not in self.compatible_values:
            raise ValueError("credential_invalid")
        return self.compatible_values[value]


class CredentialKindStrictOAuthTests(unittest.TestCase):
    def test_aad_bound_oauth_is_classified(self):
        vault = FakeVault(strict_values={b"bound": OAUTH_PAYLOAD})
        with patch.object(credential_kind, "cookie_vault", vault):
            self.assertEqual(
                "weibo_oauth",
                credential_kind.account_credential_kind("weibo", b"bound"),
            )

    def test_unbound_oauth_fails_closed_but_legacy_browser_remains_compatible(self):
        vault = FakeVault(
            compatible_values={
                b"legacy-oauth": OAUTH_PAYLOAD,
                b"legacy-browser": BROWSER_PAYLOAD,
            }
        )
        with patch.object(credential_kind, "cookie_vault", vault):
            self.assertEqual(
                "invalid",
                credential_kind.account_credential_kind(
                    "weibo",
                    b"legacy-oauth",
                ),
            )
            self.assertEqual(
                "browser_session",
                credential_kind.account_credential_kind(
                    "weibo",
                    b"legacy-browser",
                ),
            )

    def test_oauth_decrypt_helper_never_uses_legacy_fallback(self):
        vault = FakeVault(compatible_values={b"legacy-oauth": OAUTH_PAYLOAD})
        with patch.object(credential_kind, "cookie_vault", vault):
            with self.assertRaisesRegex(ValueError, "aad_binding_invalid"):
                credential_kind.decrypt_weibo_oauth_credential(
                    b"legacy-oauth"
                )

    def test_douyin_device_envelope_requires_aad_and_is_classified(self):
        vault = FakeVault(
            strict_values={b"bound-device": DEVICE_PAYLOAD},
            compatible_values={b"legacy-device": DEVICE_PAYLOAD},
        )
        with patch.object(credential_kind, "cookie_vault", vault):
            self.assertEqual(
                "device_agent",
                credential_kind.account_credential_kind(
                    "douyin", b"bound-device"
                ),
            )
            self.assertEqual(
                "invalid",
                credential_kind.account_credential_kind(
                    "douyin", b"legacy-device"
                ),
            )
            self.assertEqual(
                "device_agent",
                credential_kind.decrypt_douyin_device_credential(
                    b"bound-device"
                )["credential_kind"],
            )

    def test_remote_subject_uses_only_stable_non_secret_identity(self):
        self.assertEqual(
            "weibo:1234567890",
            credential_kind.account_remote_subject("weibo", OAUTH_PAYLOAD),
        )
        self.assertEqual(
            "bilibili:24680",
            credential_kind.account_remote_subject(
                "bilibili",
                BILIBILI_PAYLOAD,
            ),
        )
        self.assertEqual(
            f"douyin-device:{'d' * 64}",
            credential_kind.account_remote_subject(
                "douyin", DEVICE_PAYLOAD
            ),
        )
        self.assertIsNone(
            credential_kind.account_remote_subject(
                "bilibili",
                json.dumps(
                    [{"name": "SESSDATA", "value": "session"}]
                ),
            )
        )

    def test_remote_subject_is_independent_of_old_token_freshness(self):
        now = datetime.now(timezone.utc)
        for expiry in (
            now - timedelta(days=1),
            now + timedelta(seconds=30),
        ):
            payload = json.dumps(
                {
                    "credential_kind": "weibo_oauth",
                    "access_token": "old-placeholder-token",
                    "uid": "1234567890",
                    "expires_at": expiry.isoformat().replace("+00:00", "Z"),
                }
            )
            self.assertEqual(
                "weibo:1234567890",
                credential_kind.account_remote_subject("weibo", payload),
            )

    def test_remote_subject_still_rejects_malformed_stale_envelopes(self):
        payload = json.dumps(
            {
                "credential_kind": "weibo_oauth",
                "access_token": "old-placeholder-token",
                "uid": "1234567890",
                "expires_at": "not-an-instant",
            }
        )
        self.assertIsNone(
            credential_kind.account_remote_subject("weibo", payload)
        )
        self.assertIsNone(
            credential_kind.account_remote_subject(
                "bilibili",
                json.dumps(
                    [
                        {"name": "DedeUserID", "value": "24680"},
                        {"name": "DedeUserID", "value": "13579"},
                    ]
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
