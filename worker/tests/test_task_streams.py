import unittest

import app.task_streams as compatibility_topology
import shared.task_streams as shared_topology
from app.task_streams import (
    LEGACY_TASK_GROUP_NAME,
    LEGACY_TASK_STREAM_KEY,
    PLATFORM_REPAIR_TASK_STREAM_BINDINGS,
    TASK_STREAM_PLATFORMS,
    repair_task_stream_binding_for_platform,
    task_stream_binding_for_key,
    task_stream_binding_for_platform,
    task_stream_bindings,
)


class WorkerTaskStreamTopologyTests(unittest.TestCase):
    def test_worker_reexports_the_shared_topology_by_identity(self):
        self.assertIs(
            compatibility_topology.TaskStreamBinding,
            shared_topology.TaskStreamBinding,
        )
        self.assertIs(
            compatibility_topology.PLATFORM_TASK_STREAM_BINDINGS,
            shared_topology.PLATFORM_TASK_STREAM_BINDINGS,
        )
        self.assertIs(
            compatibility_topology.TASK_GROUP_NAMES,
            shared_topology.TASK_GROUP_NAMES,
        )

    def test_worker_uses_one_exact_binding_per_platform(self):
        bindings = task_stream_bindings(
            include_legacy=False,
            include_repair=False,
        )

        self.assertEqual(
            [binding.platform for binding in bindings],
            list(TASK_STREAM_PLATFORMS),
        )
        self.assertEqual(
            [binding.stream_key for binding in bindings],
            [f"lottery_tasks:{platform}" for platform in TASK_STREAM_PLATFORMS],
        )
        self.assertEqual(
            [binding.group_name for binding in bindings],
            [f"workers:{platform}" for platform in TASK_STREAM_PLATFORMS],
        )

    def test_legacy_drain_binding_is_not_a_platform_binding(self):
        legacy = task_stream_binding_for_key(LEGACY_TASK_STREAM_KEY)

        self.assertTrue(legacy.legacy)
        self.assertIsNone(legacy.platform)
        self.assertEqual(legacy.group_name, LEGACY_TASK_GROUP_NAME)
        self.assertEqual(
            task_stream_bindings(
                include_legacy=True,
                include_repair=False,
            )[-1],
            legacy,
        )

    def test_unknown_platform_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "task_stream_platform_unsupported",
        ):
            task_stream_binding_for_platform("unknown")

    def test_worker_has_four_additional_versioned_repair_lanes(self):
        bindings = task_stream_bindings(include_legacy=False)

        self.assertEqual(len(bindings), 8)
        self.assertEqual(len(PLATFORM_REPAIR_TASK_STREAM_BINDINGS), 4)
        for platform in TASK_STREAM_PLATFORMS:
            binding = repair_task_stream_binding_for_platform(platform)
            self.assertTrue(binding.repair)
            self.assertEqual(
                binding.stream_key,
                f"lottery_repair_tasks:v1:{platform}",
            )
            self.assertEqual(
                binding.group_name,
                f"repair-workers:v1:{platform}",
            )


if __name__ == "__main__":
    unittest.main()
