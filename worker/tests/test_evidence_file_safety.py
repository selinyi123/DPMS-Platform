import base64
import asyncio
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app import task_runner  # noqa: E402


class FakeScreenshotPage:
    async def evaluate(self, script):
        return {"width": 800, "height": 1200}

    async def screenshot(self, **kwargs):
        return b"\x89PNG\r\n\x1a\nexclusive-evidence"


class TransactionalFakeDatabase:
    def __init__(self):
        self.writes = []

    async def execute(self, query, values=None):
        self.writes.append((query, dict(values or {})))

    async def fetch_one(self, query, values=None):
        return {"account_id": 7, "lottery_id": 11}

    def transaction(self):
        database = self

        class Transaction:
            async def __aenter__(self):
                self.start = len(database.writes)
                return database

            async def __aexit__(self, exc_type, exc, traceback):
                if exc_type is not None:
                    del database.writes[self.start:]
                return False

        return Transaction()


class EvidenceFileSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_exclusive_writer_never_overwrites_existing_path(self):
        path = self.root / "task-1.png"
        path.write_bytes(b"existing evidence")

        with self.assertRaises(FileExistsError):
            task_runner.write_evidence_file_exclusive(path, b"replacement")

        self.assertEqual(path.read_bytes(), b"existing evidence")

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-only")
    def test_writer_fsyncs_file_before_directory_entry(self):
        path = self.root / "task-1.png"
        real_fsync = os.fsync
        fsync_order = []

        def recording_fsync(fd):
            mode = os.fstat(fd).st_mode
            fsync_order.append("directory" if stat.S_ISDIR(mode) else "file")
            return real_fsync(fd)

        with patch.object(task_runner.os, "fsync", side_effect=recording_fsync):
            task_runner.write_evidence_file_exclusive(path, b"durable evidence")

        self.assertEqual(fsync_order, ["file", "directory"])
        self.assertEqual(path.read_bytes(), b"durable evidence")

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-only")
    def test_directory_fsync_failure_fails_closed_and_durably_unlinks_file(self):
        path = self.root / "task-1.png"
        real_fsync = os.fsync
        fsync_order = []
        failed_create_directory_fsync = False

        def fail_first_directory_fsync(fd):
            nonlocal failed_create_directory_fsync
            mode = os.fstat(fd).st_mode
            entry_type = "directory" if stat.S_ISDIR(mode) else "file"
            fsync_order.append(entry_type)
            if entry_type == "directory" and not failed_create_directory_fsync:
                failed_create_directory_fsync = True
                raise OSError("simulated directory fsync failure")
            return real_fsync(fd)

        with (
            patch.object(task_runner.os, "fsync", side_effect=fail_first_directory_fsync),
            self.assertRaisesRegex(RuntimeError, "evidence_screenshot_directory_fsync_failed"),
        ):
            task_runner.write_evidence_file_exclusive(path, b"uncommitted evidence")

        # The third fsync makes the compensating unlink durable.
        self.assertEqual(fsync_order, ["file", "directory", "directory"])
        self.assertFalse(path.exists())

    def test_cleanup_does_not_delete_replaced_file(self):
        path = self.root / "task-1.png"
        _, identity = task_runner.write_evidence_file_exclusive(path, b"owned evidence")
        replacement = self.root / "replacement.png"
        replacement.write_bytes(b"replacement evidence")
        os.replace(replacement, path)

        removed = task_runner.unlink_evidence_if_identity_matches(path, identity)

        self.assertFalse(removed)
        self.assertEqual(path.read_bytes(), b"replacement evidence")

    @unittest.skipUnless(os.name == "posix", "secure directory traversal is POSIX-only")
    def test_writer_rejects_symlinked_parent_directory(self):
        real_directory = self.root / "real"
        real_directory.mkdir()
        linked_directory = self.root / "linked"
        linked_directory.symlink_to(real_directory, target_is_directory=True)

        with self.assertRaises(OSError):
            task_runner.write_evidence_file_exclusive(
                linked_directory / "task-1.png",
                b"evidence",
            )

        self.assertEqual(list(real_directory.iterdir()), [])

    async def test_cancellation_waits_for_writer_and_removes_owned_file(self):
        path = self.root / "task-1.png"
        started = threading.Event()
        release = threading.Event()
        original_writer = task_runner.write_evidence_file_exclusive

        def slow_writer(target, payload, cancellation_requested=None):
            # Create the file first so cancellation exercises identity-bound
            # cleanup instead of merely preventing the write from starting.
            result = original_writer(target, payload)
            started.set()
            release.wait(timeout=2)
            return result

        with patch.object(task_runner, "write_evidence_file_exclusive", slow_writer):
            write_task = asyncio.create_task(
                task_runner.write_evidence_file_cancellation_safe(path, b"evidence")
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            write_task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await write_task

        self.assertFalse(path.exists())

    async def test_repeated_cancellation_cannot_abandon_owned_file(self):
        path = self.root / "task-1.png"
        started = threading.Event()
        release = threading.Event()
        original_writer = task_runner.write_evidence_file_exclusive

        def slow_writer(target, payload, cancellation_requested=None):
            result = original_writer(target, payload)
            started.set()
            release.wait(timeout=2)
            return result

        with patch.object(task_runner, "write_evidence_file_exclusive", slow_writer):
            write_task = asyncio.create_task(
                task_runner.write_evidence_file_cancellation_safe(path, b"evidence")
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            write_task.cancel()
            await asyncio.sleep(0)
            write_task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await write_task

        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-only")
    async def test_shutdown_cancellation_of_writer_task_self_cleans_owned_file(self):
        path = self.root / "task-1.png"
        directory_fsync_started = threading.Event()
        release_directory_fsync = threading.Event()
        cleanup_fsync_finished = threading.Event()
        real_directory_fsync = task_runner.fsync_evidence_directory
        directory_fsync_calls = 0

        def blocking_directory_fsync(directory_fd):
            nonlocal directory_fsync_calls
            real_directory_fsync(directory_fd)
            directory_fsync_calls += 1
            if directory_fsync_calls == 1:
                directory_fsync_started.set()
                release_directory_fsync.wait(timeout=2)
            else:
                cleanup_fsync_finished.set()

        with patch.object(
            task_runner,
            "fsync_evidence_directory",
            blocking_directory_fsync,
        ):
            outer_task = asyncio.create_task(
                task_runner.write_evidence_file_cancellation_safe(path, b"evidence")
            )
            self.assertTrue(await asyncio.to_thread(directory_fsync_started.wait, 3))
            writer_task = next(
                task
                for task in asyncio.all_tasks()
                if task.get_name() == f"evidence-writer:{path.name}"
            )
            writer_task.cancel()
            await asyncio.sleep(0)
            release_directory_fsync.set()
            with self.assertRaises(asyncio.CancelledError):
                await outer_task
            self.assertTrue(await asyncio.to_thread(cleanup_fsync_finished.wait, 3))

        self.assertFalse(path.exists())

    async def test_shutdown_after_identity_publish_before_task_delivery_cleans_file(self):
        path = self.root / "task-1.png"
        identity_published = threading.Event()
        release_thread_return = threading.Event()
        original_publish = task_runner.EvidenceWriteHandoff.publish_result

        def publish_then_block(handoff, result):
            # The synchronous writer has returned a durable identity and the
            # thread-side handoff owns it, but asyncio.to_thread cannot deliver
            # its Task result until this function returns.
            original_publish(handoff, result)
            identity_published.set()
            release_thread_return.wait(timeout=3)

        with patch.object(
            task_runner.EvidenceWriteHandoff,
            "publish_result",
            publish_then_block,
        ):
            outer_task = asyncio.create_task(
                task_runner.write_evidence_file_cancellation_safe(path, b"evidence")
            )
            self.assertTrue(await asyncio.to_thread(identity_published.wait, 3))
            self.assertTrue(path.exists())
            writer_task = next(
                task
                for task in asyncio.all_tasks()
                if task.get_name() == f"evidence-writer:{path.name}"
            )
            writer_task.cancel()
            await asyncio.sleep(0)
            release_thread_return.set()
            with self.assertRaises(asyncio.CancelledError):
                await outer_task

        self.assertFalse(path.exists())

    async def test_event_failure_rolls_back_rows_and_removes_owned_file(self):
        database = TransactionalFakeDatabase()
        screenshot_root = self.root / "shadow-runs"
        screenshot_root.mkdir()

        with (
            patch.object(task_runner, "database", database),
            patch.object(task_runner, "record_event", AsyncMock(return_value=None)),
            patch.object(task_runner, "SHADOW_SCREENSHOT_DIR", screenshot_root),
        ):
            result = await task_runner.capture_shadow_screenshot(
                FakeScreenshotPage(),
                "task-1",
                7,
                11,
                {"liked": "button.like"},
            )

        self.assertIsNone(result)
        self.assertEqual(database.writes, [])
        self.assertEqual(list(screenshot_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
