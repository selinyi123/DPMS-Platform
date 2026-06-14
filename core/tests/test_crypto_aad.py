import base64
import os
import unittest

# A real 32-byte key so the vault constructs.
os.environ["ENCRYPTION_KEY"] = base64.b64encode(os.urandom(32)).decode()
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from cryptography.exceptions import InvalidTag  # noqa: E402

from app.utils.crypto import (  # noqa: E402
    CREDENTIAL_AAD,
    CookieVault,
    cookie_vault,
    notification_secret_aad,
)


class VaultAadTests(unittest.TestCase):
    def test_roundtrip_without_aad(self):
        blob = cookie_vault.encrypt("hello")
        self.assertEqual(cookie_vault.decrypt(blob), "hello")

    def test_roundtrip_with_aad(self):
        blob = cookie_vault.encrypt("secret", aad=CREDENTIAL_AAD)
        self.assertEqual(cookie_vault.decrypt(blob, aad=CREDENTIAL_AAD), "secret")

    def test_legacy_ciphertext_decrypts_under_aad_call(self):
        """Data written before AAD binding (no AAD) must still decrypt when the
        reader now passes an AAD — the backward-compat fallback."""
        legacy = cookie_vault.encrypt("old-credential")  # no aad
        self.assertEqual(cookie_vault.decrypt(legacy, aad=CREDENTIAL_AAD), "old-credential")

    def test_wrong_aad_is_rejected(self):
        """A ciphertext bound to one purpose cannot be decrypted under another;
        and an AAD-bound blob cannot be read as legacy (unbound) data."""
        blob = cookie_vault.encrypt("credential", aad=CREDENTIAL_AAD)
        with self.assertRaises(InvalidTag):
            cookie_vault.decrypt(blob, aad=notification_secret_aad("FEISHU_WEBHOOK"))
        with self.assertRaises(InvalidTag):
            cookie_vault.decrypt(blob, aad=None)

    def test_per_key_notification_aad_is_distinct(self):
        blob = cookie_vault.encrypt("token", aad=notification_secret_aad("TELEGRAM_BOT_TOKEN"))
        self.assertEqual(
            cookie_vault.decrypt(blob, aad=notification_secret_aad("TELEGRAM_BOT_TOKEN")),
            "token",
        )
        # A secret for one channel key cannot be read under another key's AAD.
        with self.assertRaises(InvalidTag):
            cookie_vault.decrypt(blob, aad=notification_secret_aad("FEISHU_WEBHOOK"))

    def test_notification_aad_format(self):
        self.assertEqual(notification_secret_aad("X"), "dpms:notification-secret:X")

    def test_worker_vault_shares_credential_aad(self):
        """Core and worker must agree on the credential AAD byte-for-byte, or the
        worker could not decrypt a credential core encrypted. Checked by reading
        the worker module's literal so this test has no worker import side effects."""
        from pathlib import Path

        worker_crypto = Path(__file__).resolve().parents[2] / "worker" / "app" / "utils" / "crypto.py"
        text = worker_crypto.read_text(encoding="utf-8")
        self.assertIn(f'CREDENTIAL_AAD = "{CREDENTIAL_AAD}"', text)


class CookieVaultConstructionTests(unittest.TestCase):
    def test_rejects_short_key(self):
        import app.config as config

        original = config.settings.encryption_key
        try:
            config.settings.encryption_key = base64.b64encode(os.urandom(16)).decode()
            with self.assertRaises(ValueError):
                CookieVault()
        finally:
            config.settings.encryption_key = original


if __name__ == "__main__":
    unittest.main()
