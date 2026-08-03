import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app import account_profile_cleanup as cleanup


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class AccountProfileCleanupContractTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_path_is_derived_from_exact_platform_and_account(self):
        path = cleanup.account_profile_path(
            7,
            "weibo",
            profiles_root=Path("/profiles"),
        )
        self.assertEqual(
            path,
            Path("/profiles/weibo/account_7"),
        )
        for account_id, platform in (
            (0, "weibo"),
            (True, "weibo"),
            (7, "../weibo"),
            (7, "weibo/account_8"),
        ):
            with self.subTest(
                account_id=account_id,
                platform=platform,
            ), self.assertRaises(cleanup.AccountProfileCleanupError):
                cleanup.account_profile_path(
                    account_id,
                    platform,
                    profiles_root=Path("/profiles"),
                )

    async def test_claim_query_is_platform_scoped_and_fenced(self):
        database = AsyncMock()
        database.transaction = lambda: Transaction()
        database.fetch_one.side_effect = (
            {
                "id": 11,
                "account_id": 7,
                "platform": "weibo",
                "status": "pending",
                "attempts": 0,
            },
            {"affected": 1},
        )
        with patch.object(cleanup, "database", database):
            claimed = await cleanup.claim_account_profile_cleanup(
                "weibo"
            )

        select_query = database.fetch_one.await_args_list[0].args[0]
        select_values = database.fetch_one.await_args_list[0].args[1]
        update_query = database.execute.await_args.args[0]
        update_values = database.execute.await_args.args[1]
        self.assertIn("WHERE platform = :platform", select_query)
        self.assertIn("FOR UPDATE SKIP LOCKED", select_query)
        self.assertEqual(select_values["platform"], "weibo")
        self.assertEqual(update_values["platform"], "weibo")
        self.assertEqual(update_values["previous_status"], "pending")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(
            claimed["claim_token"],
            update_values["claim_token"],
        )
        self.assertIn("claim_token = :claim_token", update_query)

    async def test_processing_closes_context_before_secure_delete(self):
        order = []
        pool = AsyncMock()

        async def close_context(account_id, *, reason):
            order.append(("close", account_id, reason))
            return True

        async def assert_binding(_intent):
            order.append(("binding",))

        async def assert_no_active_lease(account_id, platform):
            order.append(("lease", account_id, platform))

        class ProfileLock:
            def unlink(self):
                order.append(("lock_unlink",))

            def release(self):
                order.append(("lock_release",))

        def acquire_profile_lock(account_id, platform, **_kwargs):
            order.append(("lock", account_id, platform))
            return ProfileLock()

        def delete_profile(account_id, platform, **_kwargs):
            order.append(("delete", account_id, platform))
            return True

        async def mark_succeeded(_intent):
            order.append(("succeeded",))

        pool.close_account_context.side_effect = close_context
        intent = {
            "id": 11,
            "account_id": 7,
            "platform": "weibo",
            "attempts": 1,
            "claim_token": "1ce64c4f-20cf-4088-8aee-83bd801ff120",
            "worker_id": "worker-platform-weibo",
        }
        with (
            patch.object(
                cleanup,
                "_assert_deleted_account_binding",
                new=AsyncMock(side_effect=assert_binding),
            ),
            patch.object(
                cleanup,
                "securely_delete_account_profile",
                side_effect=delete_profile,
            ),
            patch.object(
                cleanup,
                "assert_no_active_account_profile_context_lease",
                new=AsyncMock(side_effect=assert_no_active_lease),
            ),
            patch.object(
                cleanup,
                "acquire_account_profile_lock",
                side_effect=acquire_profile_lock,
            ),
            patch.object(
                cleanup,
                "mark_account_profile_cleanup_succeeded",
                new=AsyncMock(side_effect=mark_succeeded),
            ),
            patch.object(cleanup, "structured_log"),
        ):
            await cleanup.process_account_profile_cleanup(
                pool,
                intent,
            )

        self.assertEqual(
            order,
            [
                ("binding",),
                ("close", 7, "account_profile_cleanup"),
                ("lease", 7, "weibo"),
                ("lock", 7, "weibo"),
                ("delete", 7, "weibo"),
                ("lock_unlink",),
                ("lock_release",),
                ("succeeded",),
            ],
        )

    async def test_unconfirmed_context_close_never_deletes_profile(self):
        pool = AsyncMock()
        pool.close_account_context.return_value = False
        intent = {
            "id": 11,
            "account_id": 7,
            "platform": "weibo",
            "attempts": 1,
            "claim_token": "1ce64c4f-20cf-4088-8aee-83bd801ff120",
            "worker_id": "worker-platform-weibo",
        }
        with (
            patch.object(
                cleanup,
                "_assert_deleted_account_binding",
                new=AsyncMock(),
            ),
            patch.object(
                cleanup,
                "securely_delete_account_profile",
            ) as delete_profile,
        ):
            with self.assertRaisesRegex(
                cleanup.AccountProfileCleanupError,
                "context_close_unconfirmed",
            ):
                await cleanup.process_account_profile_cleanup(
                    pool,
                    intent,
                )
        delete_profile.assert_not_called()

    async def test_failure_release_returns_same_claim_to_pending(self):
        database = AsyncMock()
        database.transaction = lambda: Transaction()
        database.fetch_one.return_value = {"affected": 1}
        intent = {
            "id": 11,
            "account_id": 7,
            "platform": "weibo",
            "attempts": 3,
            "claim_token": "1ce64c4f-20cf-4088-8aee-83bd801ff120",
            "worker_id": "worker-platform-weibo",
        }
        with patch.object(cleanup, "database", database):
            released = (
                await cleanup.release_account_profile_cleanup_for_retry(
                    intent,
                    error_code="account_profile_cleanup_test_failure",
                )
            )

        self.assertTrue(released)
        query = database.execute.await_args.args[0]
        values = database.execute.await_args.args[1]
        self.assertIn("SET status = 'pending'", query)
        self.assertIn("claim_token = :claim_token", query)
        self.assertEqual(values["platform"], "weibo")
        self.assertEqual(
            values["error_code"],
            "account_profile_cleanup_test_failure",
        )
        self.assertGreater(values["retry_seconds"], 0)

    @unittest.skipUnless(
        os.name == "posix",
        "secure dir_fd deletion is a Linux production contract",
    )
    async def test_secure_delete_unlinks_nested_symlink_not_target(self):
        with tempfile.TemporaryDirectory() as root_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            root = Path(root_dir)
            profile = root / "weibo" / "account_7"
            profile.mkdir(parents=True)
            outside = Path(outside_dir) / "credential.txt"
            outside.write_text("must-survive", encoding="utf-8")
            (profile / "nested").mkdir()
            (profile / "nested" / "cookie.db").write_text(
                "secret",
                encoding="utf-8",
            )
            (profile / "outside-link").symlink_to(outside)

            removed = await asyncio.to_thread(
                cleanup.securely_delete_account_profile,
                7,
                "weibo",
                profiles_root=root,
            )

            self.assertTrue(removed)
            self.assertFalse(profile.exists())
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "must-survive",
            )

    @unittest.skipUnless(
        os.name == "posix",
        "secure dir_fd deletion is a Linux production contract",
    )
    async def test_account_boundary_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as root_dir, (
            tempfile.TemporaryDirectory()
        ) as outside_dir:
            root = Path(root_dir)
            platform_dir = root / "weibo"
            platform_dir.mkdir()
            outside = Path(outside_dir)
            secret = outside / "cookie.db"
            secret.write_text("must-survive", encoding="utf-8")
            (platform_dir / "account_7").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                cleanup.AccountProfileCleanupError,
                "account_path_unsafe",
            ):
                await asyncio.to_thread(
                    cleanup.securely_delete_account_profile,
                    7,
                    "weibo",
                    profiles_root=root,
                )
            self.assertEqual(
                secret.read_text(encoding="utf-8"),
                "must-survive",
            )


if __name__ == "__main__":
    unittest.main()
