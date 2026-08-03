"""Opt-in Redis 7 checks for terminal Stream retention semantics.

Set ``DPMS_REDIS_INTEGRATION=1`` and point ``REDIS_URL`` at a disposable
Redis instance.  The suite creates and removes only UUID-scoped keys/users;
it never flushes a database or touches an application Stream.
"""

from __future__ import annotations

import os
import unittest
import uuid
from urllib.parse import quote, urlsplit, urlunsplit


REDIS_INTEGRATION = os.getenv("DPMS_REDIS_INTEGRATION") == "1"

if REDIS_INTEGRATION:
    from redis.asyncio import from_url
    from redis.exceptions import ResponseError

    from shared.task_streams import (
        MAX_SAFE_TERMINAL_CONSUMER_GROUPS,
        SAFE_FANOUT_RECOVERY_REENQUEUE_LUA,
        SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
    )


@unittest.skipUnless(
    REDIS_INTEGRATION,
    "requires a disposable Redis 7 instance",
)
class RedisStreamRetentionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://redis:6379/15")
        self.redis = from_url(self.redis_url, decode_responses=True)
        await self.redis.ping()
        suffix = uuid.uuid4().hex
        self.key = f"dpms:test:terminal-retention:{suffix}"
        self.marker_key = f"{self.key}:fanout-markers"
        self.acl_users: list[str] = []

    async def asyncTearDown(self):
        for username in self.acl_users:
            await self.redis.execute_command("ACL", "DELUSER", username)
        await self.redis.delete(self.key, self.marker_key)
        await self.redis.aclose()

    async def _create_groups(self):
        await self.redis.xgroup_create(
            self.key,
            "retention-g1",
            id="0-0",
            mkstream=True,
        )
        await self.redis.xgroup_create(
            self.key,
            "retention-g2",
            id="0-0",
        )

    async def _read(self, group: str, consumer: str):
        rows = await self.redis.xreadgroup(
            group,
            consumer,
            {self.key: ">"},
            count=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][1]), 1)
        return rows[0][1][0][0]

    async def _eval(self, group: str, message_id: str):
        return await self.redis.eval(
            SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
            1,
            self.key,
            group,
            message_id,
        )

    def _url_for_acl_user(self, username: str, password: str) -> str:
        parsed = urlsplit(self.redis_url)
        if parsed.scheme != "redis" or not parsed.hostname:
            self.skipTest("ACL integration requires a plain redis:// URL")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        netloc = (
            f"{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{host}:{parsed.port or 6379}"
        )
        return urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        )

    async def _create_acl_client(
        self,
        *,
        allow_eval: bool,
        allow_xdel: bool,
    ):
        username = f"dpms-retention-{uuid.uuid4().hex}"
        password = uuid.uuid4().hex
        rules = [
            "reset",
            "on",
            f">{password}",
            f"~{self.key}",
            "+xack",
            "+xinfo",
            "+xpending",
        ]
        if allow_eval:
            rules.append("+eval")
        if allow_xdel:
            rules.append("+xdel")
        await self.redis.execute_command("ACL", "SETUSER", username, *rules)
        self.acl_users.append(username)
        return from_url(
            self._url_for_acl_user(username, password),
            decode_responses=True,
        )

    async def test_deletes_only_after_every_group_releases_exact_large_id(self):
        await self._create_groups()
        # Greater than JavaScript's exact integer range: the Lua comparator
        # must compare Redis Stream ID components as decimal strings.
        first_id = "9007199254740993-0"
        await self.redis.xadd(self.key, {"kind": "first"}, id=first_id)
        self.assertEqual(
            await self._read("retention-g1", "consumer-g1"),
            first_id,
        )
        self.assertEqual(
            await self._read("retention-g2", "consumer-g2"),
            first_id,
        )
        await self.redis.xack(self.key, "retention-g2", first_id)

        self.assertEqual(
            await self._eval("retention-g1", first_id),
            [1, 1],
        )
        self.assertEqual(await self.redis.xlen(self.key), 0)

        second_id = "9007199254740994-0"
        await self.redis.xadd(self.key, {"kind": "second"}, id=second_id)
        await self._read("retention-g1", "consumer-g1")
        await self._read("retention-g2", "consumer-g2")

        self.assertEqual(
            await self._eval("retention-g1", second_id),
            [1, 0],
        )
        self.assertEqual(await self.redis.xlen(self.key), 1)
        self.assertEqual(
            await self.redis.xpending_range(
                self.key,
                "retention-g2",
                min=second_id,
                max=second_id,
                count=1,
            )
            != [],
            True,
        )

        await self.redis.xack(self.key, "retention-g2", second_id)
        self.assertEqual(
            await self._eval("retention-g1", second_id),
            [0, 1],
        )
        self.assertEqual(await self.redis.xlen(self.key), 0)

    async def test_group_overflow_acknowledges_but_skips_bounded_delete_scan(
        self,
    ):
        group = "retention-overflow-0"
        await self.redis.xgroup_create(
            self.key,
            group,
            id="0-0",
            mkstream=True,
        )
        for index in range(1, MAX_SAFE_TERMINAL_CONSUMER_GROUPS + 1):
            await self.redis.xgroup_create(
                self.key,
                f"retention-overflow-{index}",
                id="0-0",
            )

        message_id = await self.redis.xadd(
            self.key,
            {"kind": "group-overflow"},
        )
        self.assertEqual(
            await self._read(group, "consumer-overflow"),
            message_id,
        )

        self.assertEqual(
            await self._eval(group, message_id),
            [1, 0],
        )
        self.assertEqual(await self.redis.xlen(self.key), 1)
        self.assertEqual(
            await self.redis.xpending_range(
                self.key,
                group,
                min=message_id,
                max=message_id,
                count=1,
            ),
            [],
        )

    async def test_acl_denial_keeps_terminal_message_in_stream(self):
        await self._create_groups()
        message_id = await self.redis.xadd(
            self.key,
            {"kind": "acl"},
        )
        await self._read("retention-g1", "consumer-g1")
        await self._read("retention-g2", "consumer-g2")
        await self.redis.xack(self.key, "retention-g2", message_id)

        no_eval = await self._create_acl_client(
            allow_eval=False,
            allow_xdel=False,
        )
        try:
            with self.assertRaises(ResponseError):
                await no_eval.eval(
                    SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
                    1,
                    self.key,
                    "retention-g1",
                    message_id,
                )
        finally:
            await no_eval.aclose()

        self.assertEqual(await self.redis.xlen(self.key), 1)
        pending = await self.redis.xpending_range(
            self.key,
            "retention-g1",
            min=message_id,
            max=message_id,
            count=1,
        )
        self.assertEqual(len(pending), 1)

        no_delete = await self._create_acl_client(
            allow_eval=True,
            allow_xdel=False,
        )
        try:
            with self.assertRaises(ResponseError):
                await no_delete.eval(
                    SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
                    1,
                    self.key,
                    "retention-g1",
                    message_id,
                )
        finally:
            await no_delete.aclose()

        # Redis scripts are not transactional rollbacks after a runtime ACL
        # error: XACK may already be durable, but XDEL was denied. The terminal
        # message itself must remain available for audit/administrative repair.
        self.assertEqual(await self.redis.xlen(self.key), 1)

    async def test_fanout_recovery_transfers_marker_exactly_once(self):
        group = "fanout-recovery-group"
        await self.redis.xgroup_create(
            self.key,
            group,
            id="0-0",
            mkstream=True,
        )
        source_id = await self.redis.xadd(
            self.key,
            {"task_id": "task-source"},
        )
        await self._read(group, "fanout-recovery-consumer")
        member_prefix = "lane:task-1:"
        await self.redis.sadd(
            self.marker_key,
            f"{member_prefix}{source_id}",
        )

        arguments = (
            SAFE_FANOUT_RECOVERY_REENQUEUE_LUA,
            2,
            self.key,
            self.marker_key,
            group,
            source_id,
            member_prefix,
            "task_id",
            "task-1",
        )
        target_id = await self.redis.eval(*arguments)
        duplicate_result = await self.redis.eval(*arguments)

        self.assertRegex(str(target_id), r"^\d+-\d+$")
        self.assertEqual(duplicate_result, "already_reenqueued")
        self.assertEqual(await self.redis.xlen(self.key), 1)
        self.assertEqual(
            await self.redis.smembers(self.marker_key),
            {f"{member_prefix}{target_id}"},
        )


if __name__ == "__main__":
    unittest.main()
