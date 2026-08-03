import base64
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+aiomysql://u:p@localhost:3306/lottery",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app import account_calibrator, adapter_probe, task_runner  # noqa: E402


class CredentialDecryptionFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_credential_corruption_never_falls_back_to_plaintext(self):
        corrupted = memoryview(b"not-an-aes-gcm-credential")
        with (
            patch.object(
                task_runner.database,
                "fetch_one",
                new=AsyncMock(return_value={"encrypted_credential": corrupted}),
            ),
            patch.object(
                task_runner.cookie_vault,
                "decrypt",
                side_effect=ValueError("invalid ciphertext"),
            ) as decrypt,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "account_credential_decryption_failed",
            ):
                await task_runner.load_account_credential(7)

        decrypt.assert_called_once_with(
            b"not-an-aes-gcm-credential",
            aad=task_runner.CREDENTIAL_AAD,
        )

    async def test_probe_credential_corruption_never_falls_back_to_plaintext(self):
        corrupted = memoryview(b"not-an-aes-gcm-credential")
        with (
            patch.object(
                adapter_probe.database,
                "fetch_one",
                new=AsyncMock(return_value={"encrypted_credential": corrupted}),
            ),
            patch.object(
                adapter_probe.cookie_vault,
                "decrypt",
                side_effect=ValueError("invalid ciphertext"),
            ) as decrypt,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "account_credential_decryption_failed",
            ):
                await adapter_probe.load_probe_credential(7)

        decrypt.assert_called_once_with(
            b"not-an-aes-gcm-credential",
            aad=adapter_probe.CREDENTIAL_AAD,
        )

    async def test_calibration_corruption_never_reaches_cookie_injection(self):
        corrupted = memoryview(b"not-an-aes-gcm-credential")
        inject = AsyncMock()
        with (
            patch.object(
                account_calibrator.database,
                "fetch_one",
                new=AsyncMock(
                    return_value={
                        "encrypted_credential": corrupted,
                        "execution_revision": 3,
                    }
                ),
            ),
            patch.object(
                account_calibrator.cookie_vault,
                "decrypt",
                side_effect=ValueError("invalid ciphertext"),
            ) as decrypt,
            patch.object(
                account_calibrator,
                "inject_account_cookies",
                new=inject,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "account_credential_decryption_failed",
            ):
                await account_calibrator.inject_calibration_cookies(
                    object(),
                    7,
                    "bilibili",
                    expected_execution_revision=3,
                )

        decrypt.assert_called_once_with(
            b"not-an-aes-gcm-credential",
            aad=account_calibrator.CREDENTIAL_AAD,
        )
        inject.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
