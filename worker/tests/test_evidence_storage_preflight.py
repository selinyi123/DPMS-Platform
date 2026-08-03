import base64
import errno
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app import evidence_storage  # noqa: E402
from app import main as worker_main  # noqa: E402


@unittest.skipUnless(os.name == "posix", "evidence storage is Linux/POSIX-only")
class EvidenceStoragePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp_dir.name)
        self.root.chmod(0o700)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preflight_creates_private_final_directory_and_removes_probe(self):
        directory = self.root / "shadow-runs"

        checked = evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(checked, (str(directory),))
        self.assertTrue(directory.is_dir())
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode) & 0o022, 0)
        self.assertEqual(list(directory.iterdir()), [])

    def test_preflight_never_touches_existing_evidence(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        existing = directory / "existing.png"
        existing.write_bytes(b"existing evidence")

        evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(existing.read_bytes(), b"existing evidence")
        self.assertEqual(list(directory.iterdir()), [existing])

    def test_world_writable_profiles_component_fails_before_creation(self):
        profiles = self.root / "profiles"
        profiles.mkdir(mode=0o700)
        profiles.chmod(0o777)
        directory = profiles / "shadow-runs"

        with self.assertRaises(
            evidence_storage.EvidenceStoragePreflightError
        ) as raised:
            evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(raised.exception.code, "evidence_directory_mode_insecure")
        self.assertEqual(raised.exception.component, str(profiles))
        self.assertFalse(directory.exists())

    def test_symlinked_component_fails_without_writing_target(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(real, target_is_directory=True)

        with self.assertRaises(
            evidence_storage.EvidenceStoragePreflightError
        ) as raised:
            evidence_storage.preflight_evidence_storage((linked,))

        self.assertEqual(raised.exception.code, "evidence_directory_open_failed")
        self.assertEqual(list(real.iterdir()), [])

    def test_file_fsync_failure_is_classified_and_probe_is_removed(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        original_fsync = os.fsync
        failed = False

        def fail_first_file_fsync(fd):
            nonlocal failed
            if not failed and stat.S_ISREG(os.fstat(fd).st_mode):
                failed = True
                raise OSError(errno.EINVAL, "file fsync unsupported")
            return original_fsync(fd)

        with patch.object(evidence_storage.os, "fsync", fail_first_file_fsync):
            with self.assertRaises(
                evidence_storage.EvidenceStoragePreflightError
            ) as raised:
                evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(
            raised.exception.code,
            "evidence_probe_file_fsync_unsupported",
        )
        self.assertEqual(list(directory.iterdir()), [])

    def test_directory_fsync_failure_is_classified_and_probe_is_removed(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        original_fsync = os.fsync
        failed = False

        def fail_first_directory_fsync(fd):
            nonlocal failed
            if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
                failed = True
                raise OSError(errno.EINVAL, "directory fsync unsupported")
            return original_fsync(fd)

        with patch.object(evidence_storage.os, "fsync", fail_first_directory_fsync):
            with self.assertRaises(
                evidence_storage.EvidenceStoragePreflightError
            ) as raised:
                evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(
            raised.exception.code,
            "evidence_probe_directory_fsync_unsupported",
        )
        self.assertEqual(list(directory.iterdir()), [])

    def test_lock_contention_fails_after_bounded_wait(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        held_fd, _ = evidence_storage.open_locked_evidence_directory(directory)
        try:
            with patch.object(
                evidence_storage,
                "PREFLIGHT_LOCK_TIMEOUT_SECONDS",
                0.05,
            ):
                started = time.monotonic()
                with self.assertRaises(
                    evidence_storage.EvidenceStoragePreflightError
                ) as raised:
                    evidence_storage.preflight_evidence_storage((directory,))
                elapsed = time.monotonic() - started
        finally:
            evidence_storage.close_locked_evidence_directory(held_fd)

        self.assertEqual(raised.exception.code, "evidence_directory_lock_contended")
        self.assertLess(elapsed, 0.5)
        self.assertEqual(list(directory.iterdir()), [])

    def test_transient_lock_contention_is_retried_within_bound(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        held_fd, _ = evidence_storage.open_locked_evidence_directory(directory)

        def release_lock():
            time.sleep(0.05)
            evidence_storage.close_locked_evidence_directory(held_fd)

        releaser = threading.Thread(target=release_lock)
        releaser.start()
        try:
            with patch.object(
                evidence_storage,
                "PREFLIGHT_LOCK_TIMEOUT_SECONDS",
                0.5,
            ):
                checked = evidence_storage.preflight_evidence_storage((directory,))
        finally:
            releaser.join(timeout=1)

        self.assertEqual(checked, (str(directory),))
        self.assertFalse(releaser.is_alive())
        self.assertEqual(list(directory.iterdir()), [])

    def test_cleanup_failure_has_stable_code_and_preserves_primary_cause(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        original_fsync = os.fsync
        failed = False

        def fail_first_file_fsync(fd):
            nonlocal failed
            if not failed and stat.S_ISREG(os.fstat(fd).st_mode):
                failed = True
                raise OSError(errno.EINVAL, "file fsync unsupported")
            return original_fsync(fd)

        cleanup_error = evidence_storage.EvidenceStoragePreflightError(
            code="evidence_probe_cleanup_failed",
            directory=str(directory),
            component=str(directory / ".dpms-evidence-preflight-test"),
            operation="unlinkat",
            errno_value=errno.EIO,
            cause_type="OSError",
        )

        with (
            patch.object(evidence_storage.os, "fsync", fail_first_file_fsync),
            patch.object(
                evidence_storage,
                "_unlink_probe_if_identity_matches",
                side_effect=cleanup_error,
            ),
        ):
            with self.assertRaises(
                evidence_storage.EvidenceStoragePreflightError
            ) as raised:
                evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(raised.exception.code, "evidence_probe_cleanup_failed")
        self.assertEqual(
            raised.exception.primary_code,
            "evidence_probe_file_fsync_unsupported",
        )
        self.assertIsInstance(
            raised.exception.__cause__,
            evidence_storage.EvidenceStoragePreflightError,
        )
        self.assertEqual(
            raised.exception.__cause__.code,
            "evidence_probe_file_fsync_unsupported",
        )

    def test_probe_identity_failure_is_visible_and_never_unlinks_by_name(self):
        directory = self.root / "shadow-runs"
        directory.mkdir(mode=0o700)
        original_open = os.open
        original_fstat = os.fstat
        probe_fd = None

        def tracking_open(path, *args, **kwargs):
            nonlocal probe_fd
            fd = original_open(path, *args, **kwargs)
            if str(path).startswith(".dpms-evidence-preflight-"):
                probe_fd = fd
            return fd

        def fail_probe_identity(fd):
            if fd == probe_fd:
                raise OSError(errno.EIO, "probe identity unavailable")
            return original_fstat(fd)

        supported_dir_fd = set(os.supports_dir_fd)
        supported_dir_fd.add(tracking_open)
        with (
            patch.object(evidence_storage.os, "open", tracking_open),
            patch.object(evidence_storage.os, "fstat", fail_probe_identity),
            patch.object(evidence_storage.os, "supports_dir_fd", supported_dir_fd),
        ):
            with self.assertRaises(
                evidence_storage.EvidenceStoragePreflightError
            ) as raised:
                evidence_storage.preflight_evidence_storage((directory,))

        self.assertEqual(
            raised.exception.code,
            "evidence_probe_cleanup_identity_unavailable",
        )
        self.assertEqual(
            raised.exception.primary_code,
            "evidence_probe_identity_failed",
        )
        leftovers = list(directory.iterdir())
        self.assertEqual(len(leftovers), 1)
        self.assertTrue(leftovers[0].name.startswith(".dpms-evidence-preflight-"))
        leftovers[0].unlink()


class WorkerStartupEvidencePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_failure_is_structured_and_re_raised(self):
        error = evidence_storage.EvidenceStoragePreflightError(
            code="evidence_directory_mode_insecure",
            directory="/profiles/shadow-runs",
            component="/profiles",
            operation="mode_check",
        )
        with (
            patch.object(worker_main, "preflight_evidence_storage", side_effect=error),
            patch.object(worker_main, "structured_log") as log,
        ):
            with self.assertRaises(evidence_storage.EvidenceStoragePreflightError):
                await worker_main.ensure_evidence_storage_ready()

        log.assert_called_once_with(
            "critical",
            "worker_evidence_storage_preflight_failed",
            code="evidence_directory_mode_insecure",
            directory="/profiles/shadow-runs",
            component="/profiles",
            operation="mode_check",
            errno=None,
            cause_type=None,
            primary_code=None,
        )

    async def test_main_preflights_before_connecting_external_services(self):
        error = evidence_storage.EvidenceStoragePreflightError(
            code="evidence_directory_mode_insecure",
            directory="/profiles/shadow-runs",
        )
        with (
            patch.object(worker_main, "clear_stale_health_marker"),
            patch.object(
                worker_main,
                "ensure_evidence_storage_ready",
                AsyncMock(side_effect=error),
            ),
            patch.object(worker_main.database, "connect", AsyncMock()) as connect,
        ):
            with self.assertRaises(evidence_storage.EvidenceStoragePreflightError):
                await worker_main.main()

        connect.assert_not_awaited()

    def test_stale_health_marker_is_removed_before_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "worker-health"
            marker.write_text("ok", encoding="utf-8")
            with patch.object(worker_main, "HEALTH_FILE", marker):
                worker_main.clear_stale_health_marker()
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
