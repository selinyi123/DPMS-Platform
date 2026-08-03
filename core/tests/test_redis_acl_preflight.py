import unittest

from shared.redis_acl import (
    DEFAULT_DEV_REDIS_PASSWORDS,
    REQUIRED_REDIS_COMMANDS_BY_ROLE,
    RedisACLPreflightError,
    forbidden_redis_commands_for_scope,
    required_redis_commands_for_scope,
    validate_redis_acl_credentials,
    verify_redis_acl,
)
from shared.redis_consumer_groups import expected_consumer_group_names


class FakeACLRedis:
    def __init__(
        self,
        *,
        username="core",
        allow_command=None,
        deny_required_command=None,
        required_commands=None,
    ):
        self.username = username
        self.allow_command = tuple(allow_command or ())
        self.deny_required_command = tuple(deny_required_command or ())
        self.required_commands = set(
            required_redis_commands_for_scope(
                username,
                platforms=None,
                include_shared=True,
            )
            if required_commands is None
            else required_commands
        )
        self.calls = []
        self.xinfo_calls = []

    async def xinfo_groups(self, stream_key):
        self.xinfo_calls.append(stream_key)
        return [
            {"name": group_name}
            for group_name in expected_consumer_group_names(stream_key)
        ]

    async def execute_command(self, *args):
        self.calls.append(args)
        if args == ("ACL", "WHOAMI"):
            return self.username
        if args[:3] == ("ACL", "DRYRUN", self.username):
            command = tuple(args[3:])
            if (
                command
                in self.required_commands
                and command != self.deny_required_command
            ):
                return "OK"
            if command == self.allow_command:
                return "OK"
            return (
                f"User {self.username} has no permissions to run "
                f"the '{command[0].lower()}' command"
            )
        raise AssertionError(args)


class RedisACLPreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_production_rejects_shipped_development_passwords(self):
        for password in DEFAULT_DEV_REDIS_PASSWORDS:
            with self.subTest(password=password), self.assertRaisesRegex(
                RedisACLPreflightError,
                "redis_acl_password_insecure",
            ):
                validate_redis_acl_credentials(
                    "redis://redis:6379/0",
                    expected_username="core",
                    configured_username="core",
                    configured_password=password,
                    reject_development_passwords=True,
                )

    def test_separate_credentials_support_url_punctuation(self):
        identity = validate_redis_acl_credentials(
            "redis://redis:6379/0",
            expected_username="core",
            configured_username="core",
            configured_password="A!long:@/password#with?punctuation",
            reject_development_passwords=True,
        )
        self.assertEqual(identity.username, "core")
        self.assertEqual(
            identity.password,
            "A!long:@/password#with?punctuation",
        )

    async def test_exact_role_with_all_destructive_commands_denied(self):
        redis = FakeACLRedis()
        required = required_redis_commands_for_scope(
            "core",
            platforms=None,
            include_shared=True,
        )
        await verify_redis_acl(
            redis,
            redis_url="redis://redis:6379/0",
            expected_username="core",
            configured_username="core",
            configured_password="core-production-password-123456789",
            reject_development_passwords=True,
        )
        self.assertEqual(
            len(redis.calls),
            1
            + len(required)
            + len(
                forbidden_redis_commands_for_scope(
                    platforms=None,
                    include_shared=True,
                )
            ),
        )

    async def test_any_denied_required_command_fails_closed(self):
        command = REQUIRED_REDIS_COMMANDS_BY_ROLE["core"][3]
        redis = FakeACLRedis(deny_required_command=command)
        with self.assertRaisesRegex(
            RedisACLPreflightError,
            "redis_acl_required_command_denied",
        ):
            await verify_redis_acl(
                redis,
                redis_url="redis://redis:6379/0",
                expected_username="core",
                configured_username="core",
                configured_password=(
                    "core-production-password-123456789"
                ),
                reject_development_passwords=True,
            )

    async def test_worker_role_requires_its_complete_command_contract(self):
        redis = FakeACLRedis(username="worker")
        required = required_redis_commands_for_scope(
            "worker",
            platforms=None,
            include_shared=True,
        )
        await verify_redis_acl(
            redis,
            redis_url="redis://redis:6379/0",
            expected_username="worker",
            configured_username="worker",
            configured_password="worker-production-password-123456789",
            reject_development_passwords=True,
        )
        self.assertEqual(
            len(redis.calls),
            1
            + len(required)
            + len(
                forbidden_redis_commands_for_scope(
                    platforms=None,
                    include_shared=True,
                )
            ),
        )

    async def test_exact_platform_scope_never_probes_peer_keys(self):
        required = required_redis_commands_for_scope(
            "core",
            platforms=("weibo",),
            include_shared=False,
        )
        forbidden = forbidden_redis_commands_for_scope(
            platforms=("weibo",),
            include_shared=False,
        )
        redis = FakeACLRedis(required_commands=required)

        await verify_redis_acl(
            redis,
            redis_url="redis://redis:6379/0",
            expected_username="core",
            configured_username="core",
            configured_password="core-production-password-123456789",
            reject_development_passwords=True,
            platforms=("weibo",),
            include_shared=False,
        )

        dryrun_commands = tuple(
            tuple(call[3:]) for call in redis.calls[1:]
        )
        self.assertEqual(
            dryrun_commands,
            (*required, *forbidden),
        )
        flattened = "\n".join(
            " ".join(command) for command in dryrun_commands
        )
        self.assertIn("weibo", flattened)
        self.assertNotIn("bilibili", flattened)
        self.assertNotIn("xiaohongshu", flattened)
        self.assertNotIn("douyin", flattened)

    async def test_platform_identity_uses_base_role_contract(self):
        required = required_redis_commands_for_scope(
            "core",
            platforms=("bilibili",),
            include_shared=False,
        )
        redis = FakeACLRedis(
            username="core-bilibili",
            required_commands=required,
        )

        await verify_redis_acl(
            redis,
            redis_url="redis://redis:6379/0",
            expected_username="core-bilibili",
            role="core",
            configured_username="core-bilibili",
            configured_password="core-platform-production-password-123456789",
            reject_development_passwords=True,
            platforms=("bilibili",),
            include_shared=False,
        )

    async def test_control_scope_probes_only_shared_keys(self):
        required = required_redis_commands_for_scope(
            "worker",
            platforms=(),
            include_shared=True,
        )
        forbidden = forbidden_redis_commands_for_scope(
            platforms=(),
            include_shared=True,
        )
        redis = FakeACLRedis(
            username="worker",
            required_commands=required,
        )

        await verify_redis_acl(
            redis,
            redis_url="redis://redis:6379/0",
            expected_username="worker",
            configured_username="worker",
            configured_password=(
                "worker-production-password-123456789"
            ),
            reject_development_passwords=True,
            platforms=(),
            include_shared=True,
        )

        dryrun_commands = tuple(
            tuple(call[3:]) for call in redis.calls[1:]
        )
        self.assertEqual(
            dryrun_commands,
            (*required, *forbidden),
        )
        flattened = "\n".join(
            " ".join(command) for command in dryrun_commands
        )
        for platform in (
            "bilibili",
            "weibo",
            "xiaohongshu",
            "douyin",
        ):
            self.assertNotIn(platform, flattened)
        self.assertEqual(
            set(redis.xinfo_calls),
            {
                "adapter_probe_requests",
                "account_calibration_requests",
                "login_requests",
            },
        )

    async def test_missing_bootstrap_group_fails_preflight_closed(self):
        redis = FakeACLRedis()

        async def missing_group(_stream_key):
            return []

        redis.xinfo_groups = missing_group
        with self.assertRaisesRegex(
            RedisACLPreflightError,
            "redis_consumer_group_expected_missing",
        ):
            await verify_redis_acl(
                redis,
                redis_url="redis://redis:6379/0",
                expected_username="core",
                configured_username="core",
                configured_password=(
                    "core-production-password-123456789"
                ),
                reject_development_passwords=True,
            )

    def test_runtime_contract_denies_create_but_worker_keeps_delconsumer(self):
        for role in ("core", "worker"):
            required = required_redis_commands_for_scope(
                role,
                platforms=("weibo",),
                include_shared=False,
            )
            forbidden = forbidden_redis_commands_for_scope(
                platforms=("weibo",),
                include_shared=False,
            )
            self.assertFalse(
                any(command[:2] == ("XGROUP", "CREATE") for command in required)
            )
            self.assertTrue(
                any(command[:2] == ("XGROUP", "CREATE") for command in forbidden)
            )
            delconsumer_commands = tuple(
                command
                for command in required
                if command[:2] == ("XGROUP", "DELCONSUMER")
            )
            self.assertTrue(delconsumer_commands)
            if role == "core":
                self.assertTrue(
                    all(
                        command[2].startswith(
                            "discovery_scan_requests:v1:"
                        )
                        for command in delconsumer_commands
                    )
                )
        core_control = required_redis_commands_for_scope(
            "core",
            platforms=(),
            include_shared=True,
        )
        self.assertIn(
            ("XADD", "login_requests", "*", "preflight", "1"),
            core_control,
        )
        self.assertIn(
            (
                "XGROUP",
                "DELCONSUMER",
                "notify_events",
                "notify-dispatchers",
                "acl-preflight-stale",
            ),
            core_control,
        )

    async def test_any_allowed_destructive_command_fails_closed(self):
        redis = FakeACLRedis(allow_command=("FLUSHDB",))
        with self.assertRaisesRegex(
            RedisACLPreflightError,
            "redis_acl_destructive_command_allowed",
        ):
            await verify_redis_acl(
                redis,
                redis_url="redis://redis:6379/0",
                expected_username="core",
                configured_username="core",
                configured_password=(
                    "core-production-password-123456789"
                ),
                reject_development_passwords=True,
            )

    async def test_authenticated_role_mismatch_fails_closed(self):
        redis = FakeACLRedis(username="worker")
        with self.assertRaisesRegex(
            RedisACLPreflightError,
            "redis_acl_authenticated_user_mismatch",
        ):
            await verify_redis_acl(
                redis,
                redis_url="redis://redis:6379/0",
                expected_username="core",
                configured_username="core",
                configured_password=(
                    "core-production-password-123456789"
                ),
                reject_development_passwords=True,
            )


if __name__ == "__main__":
    unittest.main()
