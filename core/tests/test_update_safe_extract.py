import base64
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

# Provide a valid key/secret before importing the API module, so importing
# update.py (which pulls in app.config / app.security) never fails on env.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.update import safe_extract  # noqa: E402


def _write_zip(path, entries):
    """entries: list of (arcname, data_bytes)."""
    with zipfile.ZipFile(path, "w") as zf:
        for arcname, data in entries:
            zf.writestr(arcname, data)


class SafeExtractTests(unittest.TestCase):
    def test_benign_archive_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            zip_path = tmp / "u.zip"
            _write_zip(zip_path, [
                ("manifest.json", b"{}"),
                ("app/main.py", b"print('hi')"),
            ])
            target = tmp / "out"
            with zipfile.ZipFile(zip_path) as zf:
                safe_extract(zf, target, allowed_names={"app/main.py"})
            self.assertTrue((target / "app" / "main.py").exists())

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            zip_path = tmp / "u.zip"
            _write_zip(zip_path, [
                ("manifest.json", b"{}"),
                ("../escape.txt", b"pwned"),
            ])
            target = tmp / "out"
            with zipfile.ZipFile(zip_path) as zf:
                with self.assertRaises(ValueError):
                    safe_extract(zf, target, allowed_names={"../escape.txt"})
            self.assertFalse((tmp / "escape.txt").exists())

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            zip_path = tmp / "u.zip"
            _write_zip(zip_path, [("manifest.json", b"{}"), ("/etc/cron.d/evil", b"x")])
            target = tmp / "out"
            with zipfile.ZipFile(zip_path) as zf:
                with self.assertRaises(ValueError):
                    safe_extract(zf, target, allowed_names=set())

    def test_file_not_in_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            zip_path = tmp / "u.zip"
            _write_zip(zip_path, [
                ("manifest.json", b"{}"),
                ("app/main.py", b"ok"),
                ("app/backdoor.py", b"smuggled"),  # not in allowed_names
            ])
            target = tmp / "out"
            with zipfile.ZipFile(zip_path) as zf:
                with self.assertRaises(ValueError):
                    safe_extract(zf, target, allowed_names={"app/main.py"})


if __name__ == "__main__":
    unittest.main()
