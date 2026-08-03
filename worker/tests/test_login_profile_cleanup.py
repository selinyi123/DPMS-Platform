import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import login_profile_cleanup as cleanup


SESSION_ID = "60dc9ca4-a25f-4ec0-89c1-29e3702a22a6"


class LoginProfileCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_paths_require_one_canonical_uuid(self):
        profile, image = cleanup.login_profile_paths(
            SESSION_ID,
            profile_root=Path("/profiles/login-sessions"),
        )
        self.assertEqual(
            profile,
            Path(
                "/profiles/login-sessions/"
                f"{SESSION_ID}/profile"
            ),
        )
        self.assertEqual(
            image,
            Path(f"/profiles/login-sessions/{SESSION_ID}.png"),
        )
        for invalid in (
            "../session",
            SESSION_ID.upper(),
            f"{SESSION_ID}/profile",
            "",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                cleanup.LoginProfileCleanupError
            ):
                cleanup.login_profile_paths(invalid)

    async def test_processing_closes_exact_profile_before_delete(self):
        order = []
        pool = AsyncMock()

        async def close_profile(path, *, reason):
            order.append(("close", path, reason))
            return True

        def delete_profile(session_id):
            order.append(("delete", session_id))
            return True, True

        pool.close_transient_contexts_for_profile.side_effect = (
            close_profile
        )
        intent = {
            "id": 2,
            "session_id": SESSION_ID,
            "attempts": 1,
            "claim_token": "2bd774c4-58aa-4756-b197-c831828b5fa4",
            "worker_id": "worker-control",
        }
        with (
            patch.object(
                cleanup,
                "_assert_terminal_login_session",
                new=AsyncMock(
                    side_effect=lambda _intent: order.append(
                        ("terminal",)
                    )
                ),
            ),
            patch.object(
                cleanup,
                "securely_delete_login_profile",
                side_effect=delete_profile,
            ),
            patch.object(
                cleanup,
                "mark_login_profile_cleanup_succeeded",
                new=AsyncMock(
                    side_effect=lambda _intent: order.append(
                        ("succeeded",)
                    )
                ),
            ),
            patch.object(cleanup, "structured_log"),
        ):
            await cleanup.process_login_profile_cleanup(pool, intent)

        self.assertEqual(
            order,
            [
                ("terminal",),
                (
                    "close",
                    (
                        "/profiles/login-sessions/"
                        f"{SESSION_ID}/profile"
                    ),
                    "login_profile_cleanup",
                ),
                ("delete", SESSION_ID),
                ("succeeded",),
            ],
        )

    async def test_unconfirmed_context_close_never_deletes(self):
        pool = AsyncMock()
        pool.close_transient_contexts_for_profile.return_value = False
        intent = {
            "id": 2,
            "session_id": SESSION_ID,
            "attempts": 1,
            "claim_token": "2bd774c4-58aa-4756-b197-c831828b5fa4",
            "worker_id": "worker-control",
        }
        with (
            patch.object(
                cleanup,
                "_assert_terminal_login_session",
                new=AsyncMock(),
            ),
            patch.object(
                cleanup,
                "securely_delete_login_profile",
            ) as delete_profile,
        ):
            with self.assertRaisesRegex(
                cleanup.LoginProfileCleanupError,
                "context_close_unconfirmed",
            ):
                await cleanup.process_login_profile_cleanup(
                    pool,
                    intent,
                )
        delete_profile.assert_not_called()

    @unittest.skipUnless(
        os.name == "posix",
        "secure dir_fd deletion is a Linux production contract",
    )
    async def test_secure_delete_removes_only_exact_uuid_targets(self):
        with tempfile.TemporaryDirectory() as root_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            root = Path(root_dir)
            profile = root / SESSION_ID / "profile"
            profile.mkdir(parents=True)
            (profile / "cookie.db").write_text(
                "secret",
                encoding="utf-8",
            )
            image = root / f"{SESSION_ID}.png"
            image.write_bytes(b"png")
            unknown_sibling = root / SESSION_ID / "keep.txt"
            unknown_sibling.write_text("keep", encoding="utf-8")
            outside = Path(outside_dir) / "must-survive"
            outside.write_text("safe", encoding="utf-8")
            (profile / "outside-link").symlink_to(outside)

            result = await asyncio.to_thread(
                cleanup.securely_delete_login_profile,
                SESSION_ID,
                profile_root=root,
            )

            self.assertEqual(result, (True, True))
            self.assertTrue((root / SESSION_ID).is_dir())
            self.assertEqual(
                unknown_sibling.read_text(encoding="utf-8"),
                "keep",
            )
            self.assertFalse(profile.exists())
            self.assertFalse(image.exists())
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "safe",
            )


if __name__ == "__main__":
    unittest.main()
