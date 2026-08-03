import unittest

from app.worker_identity import (
    MAX_WORKER_ID_LENGTH,
    WORKER_ID,
    build_worker_instance_id,
    is_worker_instance_id,
)


class WorkerIdentityTests(unittest.TestCase):
    def test_runtime_identity_uses_a_128_bit_start_nonce(self):
        self.assertLessEqual(len(WORKER_ID), MAX_WORKER_ID_LENGTH)
        self.assertRegex(WORKER_ID, r":[0-9]+:[0-9a-f]{32}\Z")

    def test_sibling_processes_never_share_the_same_consumer_identity(self):
        first = build_worker_instance_id(
            base="worker-host",
            pid=101,
            instance_nonce="run-a",
        )
        second = build_worker_instance_id(
            base="worker-host",
            pid=102,
            instance_nonce="run-b",
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first, "worker-host:101:run-a")
        self.assertEqual(second, "worker-host:102:run-b")

    def test_pid_reuse_does_not_revive_a_stale_worker_identity(self):
        old_process = build_worker_instance_id(
            base="worker-host",
            pid=101,
            instance_nonce="old-run",
        )
        restarted_process = build_worker_instance_id(
            base="worker-host",
            pid=101,
            instance_nonce="new-run",
        )

        self.assertNotEqual(old_process, restarted_process)

    def test_identity_is_sanitized_and_bounded_for_database_storage(self):
        identity = build_worker_instance_id(
            base=("worker host/\n" * 32),
            pid=12345,
            instance_nonce="test-run",
        )

        self.assertLessEqual(len(identity), MAX_WORKER_ID_LENGTH)
        self.assertTrue(identity.endswith(":12345:test-run"))
        self.assertNotIn(" ", identity)
        self.assertNotIn("\n", identity)

    def test_explicit_identity_is_stable_for_nonproduction_tools(self):
        first = build_worker_instance_id(
            configured_id="dpms-worker-weibo"
        )
        second = build_worker_instance_id(
            configured_id="dpms-worker-weibo",
            pid=999,
            instance_nonce="ignored",
        )

        self.assertEqual(first, second)
        self.assertTrue(is_worker_instance_id(first))
        self.assertTrue(
            is_worker_instance_id(
                build_worker_instance_id(
                    base="host",
                    pid=10,
                    instance_nonce="a" * 32,
                )
            )
        )

    def test_configured_identity_requires_reserved_worker_namespace(self):
        for value in ("worker-weibo", "dpms-worker bad", "x" * 129):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "worker_instance_id_invalid",
            ):
                build_worker_instance_id(configured_id=value)


if __name__ == "__main__":
    unittest.main()
