import asyncio
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from app import account_profile_cleanup as cleanup
from app import account_profile_lock as profile_locks


class AccountProfileLockPortabilityTests(unittest.TestCase):
    def test_unsupported_platform_fails_closed(self):
        with patch.object(profile_locks, "fcntl", None):
            with self.assertRaisesRegex(
                profile_locks.AccountProfileLockError,
                "account_profile_lock_unsupported",
            ):
                profile_locks.acquire_account_profile_lock(
                    7,
                    "weibo",
                    profiles_root=Path("/profiles"),
                )

    def test_identity_cannot_escape_platform_namespace(self):
        for account_id, platform, root in (
            (7, "../weibo", Path("/profiles")),
            (7, "weibo/account_8", Path("/profiles")),
            (True, "weibo", Path("/profiles")),
            (7, "weibo", Path("relative")),
        ):
            with self.subTest(
                account_id=account_id,
                platform=platform,
                root=root,
            ), self.assertRaises(RuntimeError):
                profile_locks.account_profile_lock_path(
                    account_id,
                    platform,
                    profiles_root=root,
                )


def _hold_profile_lock(root: str, connection) -> None:
    lock = None
    try:
        lock = profile_locks.acquire_account_profile_lock(
            7,
            "weibo",
            profiles_root=Path(root),
        )
        connection.send(("locked", os.getpid()))
        connection.recv()
    except BaseException as exc:
        connection.send(("error", type(exc).__name__, str(exc)))
    finally:
        if lock is not None:
            lock.release()
        connection.close()


@unittest.skipUnless(
    os.name == "posix",
    "cross-process flock is a Linux production contract",
)
class AccountProfileLockTests(unittest.IsolatedAsyncioTestCase):
    def _start_lock_owner(self, root: Path):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_hold_profile_lock,
            args=(str(root), child),
        )
        process.start()
        child.close()
        self.assertTrue(
            parent.poll(10),
            "profile lock owner did not start",
        )
        message = parent.recv()
        self.assertEqual(message[0], "locked", message)
        return process, parent

    def _stop_lock_owner(self, process, connection):
        if process.is_alive():
            connection.send("release")
            process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        connection.close()
        self.assertEqual(process.exitcode, 0)

    async def test_expired_db_lease_cannot_delete_live_locked_profile(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            profile = root / "weibo" / "account_7"
            profile.mkdir(parents=True)
            credential = profile / "Cookies"
            credential.write_text("secret", encoding="utf-8")
            process, connection = self._start_lock_owner(root)
            pool = AsyncMock()
            pool.close_account_context.return_value = True
            intent = {
                "id": 11,
                "account_id": 7,
                "platform": "weibo",
                "attempts": 1,
                "claim_token": (
                    "1ce64c4f-20cf-4088-8aee-83bd801ff120"
                ),
                "worker_id": "worker-platform-weibo",
            }
            mark_succeeded = AsyncMock()
            try:
                with (
                    patch.object(cleanup, "PROFILES_ROOT", root),
                    patch.object(
                        cleanup,
                        "_assert_deleted_account_binding",
                        new=AsyncMock(),
                    ),
                    patch.object(
                        cleanup,
                        "assert_no_active_account_profile_context_lease",
                        new=AsyncMock(),
                    ),
                    patch.object(
                        cleanup,
                        "mark_account_profile_cleanup_succeeded",
                        new=mark_succeeded,
                    ),
                    patch.object(cleanup, "structured_log"),
                ):
                    with self.assertRaisesRegex(
                        profile_locks.AccountProfileLockError,
                        "account_profile_lock_busy",
                    ):
                        await cleanup.process_account_profile_cleanup(
                            pool,
                            intent,
                        )
                    self.assertEqual(
                        credential.read_text(encoding="utf-8"),
                        "secret",
                    )
                    mark_succeeded.assert_not_awaited()

                    self._stop_lock_owner(process, connection)
                    await cleanup.process_account_profile_cleanup(
                        pool,
                        intent,
                    )
            finally:
                if process.is_alive():
                    self._stop_lock_owner(process, connection)

            self.assertFalse(profile.exists())
            mark_succeeded.assert_awaited_once_with(intent)

    async def test_lock_path_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            root = Path(root_dir)
            platform = root / "weibo"
            platform.mkdir()
            outside = Path(outside_dir) / "outside.lock"
            outside.write_text("safe", encoding="utf-8")
            lock_path = profile_locks.account_profile_lock_path(
                7,
                "weibo",
                profiles_root=root,
            )
            lock_path.symlink_to(outside)

            with self.assertRaisesRegex(
                profile_locks.AccountProfileLockError,
                "account_profile_lock_path_unsafe",
            ):
                profile_locks.acquire_account_profile_lock(
                    7,
                    "weibo",
                    profiles_root=root,
                )
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "safe",
            )

    async def test_platform_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            root = Path(root_dir)
            (root / "weibo").symlink_to(
                Path(outside_dir),
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                profile_locks.AccountProfileLockError,
                "account_profile_lock_platform_unsafe",
            ):
                profile_locks.acquire_account_profile_lock(
                    7,
                    "weibo",
                    profiles_root=root,
                )
            self.assertEqual(list(Path(outside_dir).iterdir()), [])

    async def test_profile_root_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as parent_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            linked_root = Path(parent_dir) / "profiles"
            linked_root.symlink_to(
                Path(outside_dir),
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                profile_locks.AccountProfileLockError,
                "account_profile_lock_root_unsafe",
            ):
                profile_locks.acquire_account_profile_lock(
                    7,
                    "weibo",
                    profiles_root=linked_root,
                )
            self.assertEqual(list(Path(outside_dir).iterdir()), [])

    async def test_cancelled_cleanup_keeps_lock_until_delete_thread_ends(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            profile = root / "weibo" / "account_7"
            profile.mkdir(parents=True)
            started = threading.Event()
            allow_finish = threading.Event()
            released = threading.Event()
            real_acquire = (
                profile_locks.acquire_account_profile_lock
            )

            class ObservedLock:
                def __init__(self, inner):
                    self.inner = inner

                def unlink(self):
                    self.inner.unlink()

                def release(self):
                    try:
                        self.inner.release()
                    finally:
                        released.set()

            def acquire_observed(*args, **kwargs):
                return ObservedLock(
                    real_acquire(*args, **kwargs)
                )

            def blocking_delete(*_args, **_kwargs):
                started.set()
                if not allow_finish.wait(10):
                    raise RuntimeError("delete_test_timeout")
                return True

            pool = AsyncMock()
            pool.close_account_context.return_value = True
            mark_succeeded = AsyncMock()
            intent = {
                "id": 11,
                "account_id": 7,
                "platform": "weibo",
                "attempts": 1,
                "claim_token": (
                    "1ce64c4f-20cf-4088-8aee-83bd801ff120"
                ),
                "worker_id": "worker-platform-weibo",
            }
            with (
                patch.object(cleanup, "PROFILES_ROOT", root),
                patch.object(
                    cleanup,
                    "_assert_deleted_account_binding",
                    new=AsyncMock(),
                ),
                patch.object(
                    cleanup,
                    "assert_no_active_account_profile_context_lease",
                    new=AsyncMock(),
                ),
                patch.object(
                    cleanup,
                    "acquire_account_profile_lock",
                    side_effect=acquire_observed,
                ),
                patch.object(
                    cleanup,
                    "securely_delete_account_profile",
                    side_effect=blocking_delete,
                ),
                patch.object(
                    cleanup,
                    "mark_account_profile_cleanup_succeeded",
                    new=mark_succeeded,
                ),
            ):
                task = asyncio.create_task(
                    cleanup.process_account_profile_cleanup(
                        pool,
                        intent,
                    )
                )
                self.assertTrue(
                    await asyncio.to_thread(started.wait, 5)
                )
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaisesRegex(
                    profile_locks.AccountProfileLockError,
                    "account_profile_lock_busy",
                ):
                    real_acquire(
                        7,
                        "weibo",
                        profiles_root=root,
                    )

                allow_finish.set()
                self.assertTrue(
                    await asyncio.to_thread(released.wait, 5)
                )

            lock = real_acquire(
                7,
                "weibo",
                profiles_root=root,
            )
            lock.release()
            mark_succeeded.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
