import unittest

import app.task_streams as compatibility_topology
import shared.task_streams as shared_topology
from app.task_streams import (
    LEGACY_TASK_GROUP_NAME,
    LEGACY_TASK_STREAM_KEY,
    MAX_SAFE_TERMINAL_CONSUMER_GROUPS,
    PLATFORM_REPAIR_TASK_STREAM_BINDINGS,
    REPAIR_TASK_PROTOCOL_VERSION,
    SAFE_FANOUT_RECOVERY_REENQUEUE_LUA,
    SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA,
    SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
    SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
    TASK_STREAM_PLATFORMS,
    repair_task_stream_binding_for_platform,
    task_stream_binding_for_key,
    task_stream_binding_for_platform,
    task_stream_bindings,
    validate_task_stream_message,
)


class TaskStreamTopologyTests(unittest.TestCase):
    def test_terminal_retention_waits_for_every_consumer_group(self):
        for script in (
            SAFE_TERMINAL_TASK_ACK_DELETE_LUA,
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
        ):
            with self.subTest(script=script[-80:]):
                self.assertIn("XACK", script)
                self.assertIn("XINFO", script)
                self.assertIn("XPENDING", script)
                self.assertIn("XDEL", script)
                self.assertNotIn("tonumber", script)
                self.assertLess(script.index("XACK"), script.rindex("XDEL"))

        self.assertIn("SISMEMBER", SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA)
        self.assertIn("SREM", SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA)
        self.assertIn("SCARD", SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA)
        self.assertIn("redis.call('DEL'", SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA)
        self.assertLess(
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA.rindex("XDEL"),
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA.rindex("SREM"),
        )
        self.assertIn(
            f"if #groups > {MAX_SAFE_TERMINAL_CONSUMER_GROUPS} then",
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
        )
        self.assertIn(
            "if #groups == 0 then",
            SAFE_TERMINAL_FANOUT_ACK_DELETE_LUA,
        )
        self.assertIn("XDEL", SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA)
        self.assertNotIn("XACK", SAFE_CONFIRMED_STREAM_ENTRY_DELETE_LUA)

    def test_fanout_recovery_retires_only_the_superseded_binding(self):
        script = SAFE_FANOUT_RECOVERY_REENQUEUE_LUA
        for command in (
            "SISMEMBER",
            "XADD",
            "SADD",
            "XACK",
            "XINFO",
            "XPENDING",
            "XDEL",
            "SREM",
        ):
            self.assertIn(command, script)
        self.assertNotIn("redis.call('DEL'", script)
        self.assertLess(script.index("SISMEMBER"), script.index("XADD"))
        self.assertLess(script.index("XADD"), script.index("SADD"))
        self.assertLess(script.index("SADD"), script.index("SREM"))
        self.assertLess(script.index("SREM"), script.index("XACK"))
        self.assertLess(script.index("XACK"), script.index("XDEL"))

    def test_core_reexports_the_shared_topology_by_identity(self):
        self.assertIs(
            compatibility_topology.TaskStreamBinding,
            shared_topology.TaskStreamBinding,
        )
        self.assertIs(
            compatibility_topology.PLATFORM_TASK_STREAM_BINDINGS,
            shared_topology.PLATFORM_TASK_STREAM_BINDINGS,
        )
        self.assertIs(
            compatibility_topology.TASK_STREAM_KEYS,
            shared_topology.TASK_STREAM_KEYS,
        )

    def test_each_platform_has_an_exact_independent_stream_and_group(self):
        bindings = task_stream_bindings(
            include_legacy=False,
            include_repair=False,
        )

        self.assertEqual(
            [binding.platform for binding in bindings],
            list(TASK_STREAM_PLATFORMS),
        )
        self.assertEqual(
            {binding.stream_key for binding in bindings},
            {f"lottery_tasks:{platform}" for platform in TASK_STREAM_PLATFORMS},
        )
        self.assertEqual(
            {binding.group_name for binding in bindings},
            {f"workers:{platform}" for platform in TASK_STREAM_PLATFORMS},
        )

    def test_legacy_binding_is_explicit_and_optional(self):
        without_legacy = task_stream_bindings(
            include_legacy=False,
            include_repair=False,
        )
        with_legacy = task_stream_bindings(
            include_legacy=True,
            include_repair=False,
        )

        self.assertEqual(len(with_legacy), len(without_legacy) + 1)
        legacy = task_stream_binding_for_key(LEGACY_TASK_STREAM_KEY)
        self.assertTrue(legacy.legacy)
        self.assertEqual(legacy.group_name, LEGACY_TASK_GROUP_NAME)
        self.assertIsNone(legacy.platform)

    def test_unknown_platform_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "task_stream_platform_unsupported",
        ):
            task_stream_binding_for_platform("unknown")

    def test_each_platform_has_a_versioned_repair_only_lane(self):
        bindings = task_stream_bindings(include_legacy=False)

        self.assertEqual(len(bindings), 8)
        self.assertEqual(len(PLATFORM_REPAIR_TASK_STREAM_BINDINGS), 4)
        for platform in TASK_STREAM_PLATFORMS:
            binding = repair_task_stream_binding_for_platform(platform)
            self.assertEqual(
                binding.stream_key,
                f"lottery_repair_tasks:v1:{platform}",
            )
            self.assertEqual(
                binding.group_name,
                f"repair-workers:v1:{platform}",
            )
            self.assertTrue(binding.repair)
            self.assertEqual(
                binding.protocol_version,
                REPAIR_TASK_PROTOCOL_VERSION,
            )

    def test_standard_and_repair_lanes_reject_cross_protocol_messages(self):
        standard = task_stream_binding_for_platform("weibo")
        repair = repair_task_stream_binding_for_platform("weibo")
        legacy = task_stream_binding_for_key(LEGACY_TASK_STREAM_KEY)
        repair_message = {
            "platform": "weibo",
            "mode": "real_run",
            "execution_intent_kind": "repair",
        }

        with self.assertRaisesRegex(
            ValueError,
            "standard_task_stream_repair_forbidden",
        ):
            validate_task_stream_message(standard, repair_message)
        with self.assertRaisesRegex(
            ValueError,
            "legacy_task_stream_repair_forbidden",
        ):
            validate_task_stream_message(legacy, repair_message)
        validate_task_stream_message(repair, repair_message)
        with self.assertRaisesRegex(
            ValueError,
            "repair_task_stream_contract_mismatch",
        ):
            validate_task_stream_message(
                repair,
                {
                    "platform": "weibo",
                    "mode": "shadow_run",
                    "execution_intent_kind": "repair",
                },
            )


if __name__ == "__main__":
    unittest.main()
