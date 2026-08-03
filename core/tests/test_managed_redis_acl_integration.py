import os
import unittest

from redis.asyncio import Redis
from redis.exceptions import AuthenticationError, ResponseError

from shared.redis_acl import verify_redis_acl


MANAGED_ACL_INTEGRATION = (
    os.getenv("DPMS_MANAGED_REDIS_ACL_INTEGRATION") == "1"
)


@unittest.skipUnless(
    MANAGED_ACL_INTEGRATION,
    "managed Redis ACL integration is disabled",
)
class ManagedRedisACLIntegrationTests(
    unittest.IsolatedAsyncioTestCase
):
    def _client(self, username: str, password_env: str) -> Redis:
        password = os.environ[password_env]
        return Redis(
            host=os.getenv("DPMS_MANAGED_REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("DPMS_MANAGED_REDIS_PORT", "6380")),
            db=0,
            username=username,
            password=password,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )

    async def asyncSetUp(self):
        self.clients: list[Redis] = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def _tracked_client(
        self,
        username: str,
        password_env: str,
    ) -> Redis:
        client = self._client(username, password_env)
        self.clients.append(client)
        return client

    async def test_core_and_worker_scopes_match_managed_acl(self):
        base_url = (
            "redis://"
            f"{os.getenv('DPMS_MANAGED_REDIS_HOST', '127.0.0.1')}:"
            f"{int(os.getenv('DPMS_MANAGED_REDIS_PORT', '6380'))}/0"
        )
        contracts = (
            (
                "core",
                "DPMS_MANAGED_REDIS_CORE_PASSWORD",
                ("bilibili",),
                False,
            ),
            (
                "core",
                "DPMS_MANAGED_REDIS_CORE_PASSWORD",
                (),
                True,
            ),
            (
                "worker",
                "DPMS_MANAGED_REDIS_WORKER_PASSWORD",
                ("weibo",),
                False,
            ),
            (
                "worker",
                "DPMS_MANAGED_REDIS_WORKER_PASSWORD",
                (),
                True,
            ),
        )
        for role, password_env, platforms, include_shared in contracts:
            with self.subTest(
                role=role,
                platforms=platforms,
                include_shared=include_shared,
            ):
                password = os.environ[password_env]
                client = self._tracked_client(role, password_env)
                await verify_redis_acl(
                    client,
                    redis_url=base_url,
                    expected_username=role,
                    configured_username=role,
                    configured_password=password,
                    reject_development_passwords=True,
                    platforms=platforms,
                    include_shared=include_shared,
                )

    async def test_bootstrap_is_disabled_and_auxiliary_roles_are_narrow(self):
        host = os.getenv("DPMS_MANAGED_REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("DPMS_MANAGED_REDIS_PORT", "6380"))

        default_client = Redis(
            host=host,
            port=port,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        self.clients.append(default_client)
        with self.assertRaises(AuthenticationError):
            await default_client.ping()

        health = self._tracked_client(
            "health",
            "DPMS_MANAGED_REDIS_HEALTH_PASSWORD",
        )
        self.assertTrue(await health.ping())
        with self.assertRaises(ResponseError):
            await health.info()

        group_admin = self._tracked_client(
            "group-admin",
            "DPMS_MANAGED_REDIS_GROUP_ADMIN_PASSWORD",
        )
        self.assertEqual(
            await group_admin.execute_command("ACL", "WHOAMI"),
            "group-admin",
        )
        self.assertEqual(
            await group_admin.execute_command(
                "ACL",
                "DRYRUN",
                "group-admin",
                "XGROUP",
                "DESTROY",
                "lottery_tasks:bilibili",
                "workers:bilibili",
            ),
            "OK",
        )
        with self.assertRaises(ResponseError):
            await group_admin.xadd(
                "lottery_tasks:bilibili",
                {"forbidden": "1"},
            )


if __name__ == "__main__":
    unittest.main()
