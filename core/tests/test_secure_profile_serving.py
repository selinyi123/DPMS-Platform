import os
from pathlib import Path
import tempfile
import unittest

from app.utils.secure_files import (
    SecureFileError,
    open_bounded_regular_file_beneath_root,
    read_bounded_regular_file_beneath_root,
)


@unittest.skipUnless(
    os.name == "posix",
    "secure volume traversal is a Linux production contract",
)
class SecureProfileServingTests(unittest.TestCase):
    def test_streams_a_descriptor_pinned_file_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "profiles"
            root.mkdir()
            candidate = root / "session.png"
            candidate.write_bytes(b"012345")

            snapshot = open_bounded_regular_file_beneath_root(
                root,
                candidate,
                max_bytes=16,
            )

            self.assertEqual(snapshot.size, 6)
            self.assertEqual(list(snapshot.iter_chunks(2)), [b"01", b"23", b"45"])
            self.assertTrue(snapshot.stream.closed)

    def test_stream_detects_in_place_file_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "profiles"
            root.mkdir()
            candidate = root / "session.png"
            candidate.write_bytes(b"012345")
            snapshot = open_bounded_regular_file_beneath_root(
                root,
                candidate,
                max_bytes=16,
            )
            chunks = snapshot.iter_chunks(2)
            self.assertEqual(next(chunks), b"01")
            candidate.write_bytes(b"changed")

            with self.assertRaises(SecureFileError):
                list(chunks)
            self.assertTrue(snapshot.stream.closed)

    def test_reads_exact_bounded_regular_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "profiles"
            candidate = root / "login-sessions" / "session.png"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"png")

            self.assertEqual(
                read_bounded_regular_file_beneath_root(
                    root,
                    candidate,
                    max_bytes=16,
                ),
                b"png",
            )

    def test_refuses_symlink_outside_root(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            tempfile.TemporaryDirectory() as outside_dir,
        ):
            root = Path(temp_dir) / "profiles"
            root.mkdir()
            outside = Path(outside_dir) / "secret"
            outside.write_bytes(b"secret")
            candidate = root / "session.png"
            candidate.symlink_to(outside)

            with self.assertRaises(SecureFileError):
                read_bounded_regular_file_beneath_root(
                    root,
                    candidate,
                    max_bytes=16,
                )

    def test_refuses_oversized_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "profiles"
            root.mkdir()
            candidate = root / "session.png"
            candidate.write_bytes(b"0123456789")

            with self.assertRaises(SecureFileError):
                read_bounded_regular_file_beneath_root(
                    root,
                    candidate,
                    max_bytes=4,
                )

    def test_refuses_symlink_inside_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "profiles"
            root.mkdir()
            target = root / "target.png"
            target.write_bytes(b"png")
            candidate = root / "alias.png"
            candidate.symlink_to(target)

            with self.assertRaises(SecureFileError):
                read_bounded_regular_file_beneath_root(
                    root,
                    candidate,
                    max_bytes=16,
                )


if __name__ == "__main__":
    unittest.main()
