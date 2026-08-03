import asyncio
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from app import account_profile_context_lease as leases
from app import account_profile_lock as profile_locks
from app.browser_pool import BrowserPool


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeDatabase:
    def __init__(self, *, account, lease=None, affected=1):
        self.account = account
        self.lease = lease
        self.affected = affected
        self.executed = []
        self.queries = []

    def transaction(self):
        return Transaction()

    async def fetch_one(self, query, values=None):
        self.queries.append((query, values or {}))
        if "FROM accounts" in query:
            return self.account
        if "FROM account_profile_context_leases" in query:
            return self.lease
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        self.executed.append((query, values or {}))
        return None


class AccountProfileContextLeaseTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_deleted_account_cannot_acquire_or_renew(self):
        for operation in ("acquire", "renew"):
            with self.subTest(operation=operation):
                database = FakeDatabase(
                    account={
                        "platform": "weibo",
                        "deleted_at": object(),
                    }
                )
                with self.assertRaisesRegex(
                    leases.AccountProfileContextLeaseError,
                    "account_deleted",
                ):
                    if operation == "acquire":
                        await leases.acquire_account_profile_context_lease(
                            7,
                            "weibo",
                            db=database,
                        )
                    else:
                        await leases.renew_account_profile_context_lease(
                            7,
                            "weibo",
                            "2bd774c4-58aa-4756-b197-c831828b5fa4",
                            db=database,
                        )
                self.assertEqual(database.executed, [])
                self.assertIn(
                    "FOR UPDATE",
                    database.queries[0][0],
                )

    async def test_unexpired_other_owner_is_never_stolen(self):
        database = FakeDatabase(
            account={"platform": "weibo", "deleted_at": None},
            lease={
                "platform": "weibo",
                "lease_token": (
                    "2bd774c4-58aa-4756-b197-c831828b5fa4"
                ),
                "owner_id": "other-worker",
                "lease_is_active": 1,
            },
        )
        with self.assertRaisesRegex(
            leases.AccountProfileContextLeaseError,
            "lease_busy",
        ):
            await leases.acquire_account_profile_context_lease(
                7,
                "weibo",
                db=database,
            )
        self.assertEqual(database.executed, [])

    async def test_absent_lease_is_inserted_with_bounded_owner_token(self):
        database = FakeDatabase(
            account={"platform": "weibo", "deleted_at": None},
            lease=None,
        )
        token = await leases.acquire_account_profile_context_lease(
            7,
            "weibo",
            owner_id="worker-a",
            db=database,
        )

        self.assertEqual(str(uuid.UUID(token)), token)
        query, values = database.executed[0]
        self.assertIn(
            "INSERT INTO account_profile_context_leases",
            query,
        )
        self.assertEqual(values["account_id"], 7)
        self.assertEqual(values["platform"], "weibo")
        self.assertEqual(values["owner_id"], "worker-a")
        self.assertGreaterEqual(values["ttl_seconds"], 120)

    async def test_release_is_exact_token_and_owner_scoped(self):
        database = FakeDatabase(
            account=None,
            affected=1,
        )
        released = (
            await leases.release_account_profile_context_lease(
                7,
                "weibo",
                "2bd774c4-58aa-4756-b197-c831828b5fa4",
                owner_id="worker-a",
                db=database,
            )
        )
        self.assertTrue(released)
        query, values = database.executed[0]
        self.assertIn("lease_token = :lease_token", query)
        self.assertIn("owner_id = :owner_id", query)
        self.assertEqual(values["owner_id"], "worker-a")

    async def test_cleanup_refuses_any_unexpired_owner(self):
        database = FakeDatabase(
            account=None,
            lease={
                "platform": "weibo",
                "lease_is_active": 1,
            },
        )
        with self.assertRaisesRegex(
            leases.AccountProfileContextLeaseError,
            "lease_active",
        ):
            await (
                leases.assert_no_active_account_profile_context_lease(
                    7,
                    "weibo",
                    db=database,
                )
            )

    async def test_browser_pool_acquires_reuses_renews_and_releases(self):
        token = "2bd774c4-58aa-4756-b197-c831828b5fa4"
        context = SimpleNamespace(pages=[], close=AsyncMock())
        browser_type = SimpleNamespace(
            launch_persistent_context=AsyncMock(
                return_value=context
            )
        )
        acquire = AsyncMock(return_value=token)
        renew = AsyncMock()
        release = AsyncMock(return_value=True)
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "weibo" / "account_7"
            pool = BrowserPool(
                max_browsers=1,
                profiles_root=root,
            )
            pool._playwright = object()
            pool._browser_type = browser_type
            with (
                patch(
                    "app.browser_pool."
                    "acquire_account_profile_context_lease",
                    acquire,
                ),
                patch(
                    "app.browser_pool."
                    "renew_account_profile_context_lease",
                    renew,
                ),
                patch(
                    "app.browser_pool."
                    "release_account_profile_context_lease",
                    release,
                ),
            ):
                first = await pool.get_account_context(
                    7,
                    str(profile),
                    platform="weibo",
                )
                with self.assertRaisesRegex(
                    profile_locks.AccountProfileLockError,
                    "account_profile_lock_busy",
                ):
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )
                second = await pool.get_account_context(
                    7,
                    str(profile),
                    platform="weibo",
                )
                await pool.prune_persistent_contexts(
                    reason="test-reaper",
                )
                closed = await pool.close_account_context(
                    7,
                    reason="test",
                )
                released_lock = (
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )
                )
                released_lock.release()

        self.assertIs(first, context)
        self.assertIs(second, context)
        self.assertTrue(closed)
        acquire.assert_awaited_once_with(7, "weibo")
        self.assertEqual(
            renew.await_args_list,
            [
                call(7, "weibo", token),
                call(7, "weibo", token),
            ],
        )
        release.assert_awaited_once_with(7, "weibo", token)
        context.close.assert_awaited_once_with()

    async def test_cancelled_launch_quarantines_lock_until_runtime_stops(self):
        token = "2bd774c4-58aa-4756-b197-c831828b5fa4"
        launch_started = asyncio.Event()

        async def launch_persistent_context(**_kwargs):
            launch_started.set()
            await asyncio.Future()

        runtime = SimpleNamespace(stop=AsyncMock())
        browser_type = SimpleNamespace(
            launch_persistent_context=AsyncMock(
                side_effect=launch_persistent_context
            )
        )
        acquire = AsyncMock(return_value=token)
        release = AsyncMock(return_value=True)
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "weibo" / "account_7"
            pool = BrowserPool(
                max_browsers=1,
                profiles_root=root,
            )
            pool._playwright = runtime
            pool._browser_type = browser_type
            with (
                patch(
                    "app.browser_pool."
                    "acquire_account_profile_context_lease",
                    acquire,
                ),
                patch(
                    "app.browser_pool."
                    "release_account_profile_context_lease",
                    release,
                ),
            ):
                task = asyncio.create_task(
                    pool.get_account_context(
                        7,
                        str(profile),
                        platform="weibo",
                    )
                )
                await launch_started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                release.assert_not_awaited()
                self.assertEqual(
                    len(pool._quarantined_profile_locks),
                    1,
                )
                with self.assertRaisesRegex(
                    profile_locks.AccountProfileLockError,
                    "account_profile_lock_busy",
                ):
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )

                await pool.close()
                available = (
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )
                )
                available.release()

        runtime.stop.assert_awaited_once_with()

    async def test_failed_context_close_keeps_profile_lock(self):
        token = "2bd774c4-58aa-4756-b197-c831828b5fa4"
        context = SimpleNamespace(
            pages=[],
            close=AsyncMock(
                side_effect=(RuntimeError("close failed"), None)
            ),
        )
        browser_type = SimpleNamespace(
            launch_persistent_context=AsyncMock(
                return_value=context
            )
        )
        acquire = AsyncMock(return_value=token)
        release = AsyncMock(return_value=True)
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile = root / "weibo" / "account_7"
            pool = BrowserPool(
                max_browsers=1,
                profiles_root=root,
            )
            pool._playwright = object()
            pool._browser_type = browser_type
            with (
                patch(
                    "app.browser_pool."
                    "acquire_account_profile_context_lease",
                    acquire,
                ),
                patch(
                    "app.browser_pool."
                    "release_account_profile_context_lease",
                    release,
                ),
            ):
                await pool.get_account_context(
                    7,
                    str(profile),
                    platform="weibo",
                )
                self.assertFalse(
                    await pool.close_account_context(
                        7,
                        reason="first-close",
                    )
                )
                release.assert_not_awaited()
                with self.assertRaisesRegex(
                    profile_locks.AccountProfileLockError,
                    "account_profile_lock_busy",
                ):
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )

                self.assertTrue(
                    await pool.close_account_context(
                        7,
                        reason="retry-close",
                    )
                )
                available = (
                    profile_locks.acquire_account_profile_lock(
                        7,
                        "weibo",
                        profiles_root=root,
                    )
                )
                available.release()

        release.assert_awaited_once_with(7, "weibo", token)


if __name__ == "__main__":
    unittest.main()
