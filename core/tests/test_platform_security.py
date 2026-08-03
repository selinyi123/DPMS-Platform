import base64
import os
import unittest

from shared.platform_security import (
    expected_platform_database_username,
    expected_platform_redis_username,
    require_platform_runtime_identity,
    scoped_encryption_key,
    scoped_env_name,
)


GOOD_KEY = base64.b64encode(b"p" * 32).decode("ascii")


class PlatformSecurityTests(unittest.TestCase):
    def test_scoped_names_are_stable(self):
        environment = {
            "MYSQL_RUNTIME_USER_BILIBILI": "lane_bilibili",
            "REDIS_CORE_BILIBILI_USERNAME": "core-bilibili-canonical",
            "REDIS_CORE_USERNAME_BILIBILI": "core-bilibili-custom",
            "ENCRYPTION_KEY_BILIBILI": GOOD_KEY,
        }
        self.assertEqual(
            scoped_env_name("encryption_key", "bilibili"),
            "ENCRYPTION_KEY_BILIBILI",
        )
        self.assertEqual(
            expected_platform_database_username(
                "bilibili", environment=environment
            ),
            "lane_bilibili",
        )
        self.assertEqual(
            expected_platform_redis_username(
                "core", "bilibili", environment=environment
            ),
            "core-bilibili-canonical",
        )
        self.assertEqual(
            scoped_encryption_key("bilibili", environment=environment),
            GOOD_KEY,
        )

    def test_strict_production_rejects_shared_identities(self):
        environment = {"ENCRYPTION_KEY_BILIBILI": GOOD_KEY}
        with self.assertRaisesRegex(
            RuntimeError, "platform_database_identity_mismatch"
        ):
            require_platform_runtime_identity(
                platform="bilibili",
                role="worker",
                deployment_mode="production",
                security_mode="strict",
                database_username="dpms_runtime",
                redis_username="worker",
                encryption_key=GOOD_KEY,
                environment=environment,
            )

    def test_strict_production_accepts_bound_scoped_identity(self):
        environment = {
            "MYSQL_RUNTIME_USER_BILIBILI": "dpms_runtime_bilibili",
            "REDIS_WORKER_USERNAME_BILIBILI": "worker-bilibili",
            "ENCRYPTION_KEY_BILIBILI": GOOD_KEY,
        }
        require_platform_runtime_identity(
            platform="bilibili",
            role="worker",
            deployment_mode="production",
            security_mode="strict",
            database_username="dpms_runtime_bilibili",
            redis_username="worker-bilibili",
            encryption_key=GOOD_KEY,
            environment=environment,
        )

    def test_compat_dev_keeps_rolling_upgrade_non_blocking(self):
        require_platform_runtime_identity(
            platform="weibo",
            role="core",
            deployment_mode="dev",
            security_mode="compat",
            database_username="dpms_runtime",
            redis_username="core",
            encryption_key="",
            environment={},
        )


if __name__ == "__main__":
    unittest.main()
