import unittest

from shared.database_credentials import (
    database_credential_problems,
    require_production_database_credentials,
)
from shared.runtime_secrets import require_production_encryption_key


class DatabaseCredentialTests(unittest.TestCase):
    def test_strong_role_scoped_urls_are_accepted(self):
        self.assertEqual(
            database_credential_problems(
                "mysql+aiomysql://app_runtime:strong-runtime-secret@mysql:3306/lottery",
                role="runtime",
            ),
            (),
        )
        self.assertEqual(
            database_credential_problems(
                "mysql+aiomysql://app_migrate:strong-migration-secret@mysql:3306/lottery",
                role="migration",
            ),
            (),
        )

    def test_all_shipped_passwords_are_rejected(self):
        for password in (
            "password",
            "dpms-runtime-local-only-change-me-2026",
            "dpms-migrate-local-only-change-me-2026",
        ):
            with self.subTest(password=password):
                self.assertIn(
                    "database_password_is_built_in_default",
                    database_credential_problems(
                        f"mysql+aiomysql://app:{password}@mysql:3306/lottery",
                        role="runtime",
                    ),
                )

    def test_short_custom_password_and_invalid_username_are_rejected(self):
        self.assertIn(
            "database_password_length_invalid",
            database_credential_problems(
                "mysql+aiomysql://app:short@mysql:3306/lottery",
                role="runtime",
            ),
        )
        self.assertIn(
            "database_username_invalid",
            database_credential_problems(
                "mysql+aiomysql://bad-user:strong-runtime-secret@mysql:3306/lottery",
                role="runtime",
            ),
        )

    def test_percent_encoded_default_password_is_rejected(self):
        self.assertIn(
            "database_password_is_built_in_default",
            database_credential_problems(
                "mysql+aiomysql://app:"
                "dpms%2Druntime%2Dlocal%2Donly%2Dchange%2Dme%2D2026"
                "@mysql:3306/lottery",
                role="runtime",
            ),
        )

    def test_reserved_password_characters_are_rejected(self):
        self.assertIn(
            "database_password_characters_invalid",
            database_credential_problems(
                "mysql+aiomysql://app:"
                "strong%40runtime%2Fsecret@mysql:3306/lottery",
                role="runtime",
            ),
        )

    def test_role_swap_is_rejected(self):
        self.assertIn(
            "runtime_uses_default_migration_role",
            database_credential_problems(
                "mysql+aiomysql://dpms_migrate:strong-secret-value@mysql:3306/lottery",
                role="runtime",
            ),
        )

    def test_configured_role_username_must_match_url(self):
        self.assertIn(
            "database_role_username_mismatch",
            database_credential_problems(
                "mysql+aiomysql://wrong_runtime:strong-runtime-secret@mysql:3306/lottery",
                role="runtime",
                expected_username="expected_runtime",
            ),
        )
        self.assertIn(
            "migration_uses_default_runtime_role",
            database_credential_problems(
                "mysql+aiomysql://dpms_runtime:strong-secret-value@mysql:3306/lottery",
                role="migration",
            ),
        )

    def test_production_exception_contains_codes_not_url_or_password(self):
        secret = "dpms-runtime-local-only-change-me-2026"
        with self.assertRaises(RuntimeError) as raised:
            require_production_database_credentials(
                f"mysql+aiomysql://app:{secret}@mysql:3306/lottery",
                deployment_mode="production",
                role="runtime",
            )
        message = str(raised.exception)
        self.assertIn("database_password_is_built_in_default", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("mysql:3306", message)

    def test_development_retains_local_compatibility(self):
        require_production_database_credentials(
            "mysql+aiomysql://user:password@mysql:3306/lottery",
            deployment_mode="dev",
            role="runtime",
        )

    def test_isolated_runtime_rejects_missing_production_encryption_key(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "production_encryption_key_invalid:encryption_key_missing",
        ):
            require_production_encryption_key(
                "",
                deployment_mode="production",
            )
        require_production_encryption_key(
            "",
            deployment_mode="dev",
        )


if __name__ == "__main__":
    unittest.main()
