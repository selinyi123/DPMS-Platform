import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.account_leases import (  # noqa: E402
    AccountOperationLeaseConflict,
    acquire_account_operation_lease,
)


class FakeLeaseDatabase:
    def __init__(self, *, execution_revision=7, status="ready", platform="bilibili", credential_size=10):
        self.account = {
            "id": 41,
            "execution_revision": execution_revision,
            "status": status,
            "platform": platform,
            "credential_size": credential_size,
        }
        self.active = None
        self.inserted = None

    async def fetch_one(self, query, values=None):
        if "FROM accounts" in query:
            return dict(self.account)
        if "FROM account_operation_leases" in query and "expires_at" in query:
            return self.active
        if "MAX(generation)" in query:
            return {"next_generation": 3}
        raise AssertionError(query)

    async def execute(self, query, values=None):
        if "INSERT INTO account_operation_leases" not in query:
            raise AssertionError(query)
        self.inserted = dict(values or {})


class AccountOperationLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_locks_and_binds_expected_account_revision(self):
        db = FakeLeaseDatabase()

        lease = await acquire_account_operation_lease(
            41,
            operation_kind="shadow_run",
            owner_id="task-1",
            expected_execution_revision=7,
            expected_platform="bilibili",
            db=db,
        )

        self.assertEqual(3, lease.generation)
        self.assertEqual("task-1", lease.owner_id)
        self.assertEqual(lease.lease_id, db.inserted["lease_id"])

    async def test_revision_status_platform_or_credential_drift_fails_before_insert(self):
        cases = (
            {"execution_revision": 8},
            {"status": "warming"},
            {"platform": "weibo"},
            {"credential_size": 0},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                db = FakeLeaseDatabase(**overrides)
                with self.assertRaises(AccountOperationLeaseConflict) as caught:
                    await acquire_account_operation_lease(
                        41,
                        operation_kind="shadow_run",
                        owner_id="task-1",
                        expected_execution_revision=7,
                        expected_platform="bilibili",
                        db=db,
                    )
                self.assertEqual("account_operation_account_changed", caught.exception.code)
                self.assertIsNone(db.inserted)

    async def test_active_lease_still_uses_distinct_conflict_code(self):
        db = FakeLeaseDatabase()
        db.active = {"lease_id": "lease-existing"}

        with self.assertRaises(AccountOperationLeaseConflict) as caught:
            await acquire_account_operation_lease(
                41,
                operation_kind="adapter_probe",
                owner_id="probe-1",
                expected_execution_revision=7,
                expected_platform="bilibili",
                db=db,
            )

        self.assertEqual("account_operation_lease_active", caught.exception.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
