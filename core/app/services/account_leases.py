"""Append-only account-operation leases used across task and probe paths."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.db import database


LEASE_MINUTES = 30


class AccountOperationLeaseConflict(RuntimeError):
    def __init__(
        self,
        account_id: int,
        operation_kind: str,
        code: str = "account_operation_lease_active",
    ) -> None:
        self.account_id = int(account_id)
        self.operation_kind = str(operation_kind)
        self.code = str(code or "account_operation_lease_active")
        super().__init__(self.code)


@dataclass(frozen=True)
class AccountOperationLease:
    account_id: int
    lease_id: str
    generation: int
    operation_kind: str
    owner_id: str


async def acquire_account_operation_lease(
    account_id: int,
    *,
    operation_kind: str,
    owner_id: str,
    expected_execution_revision: int | None = None,
    expected_platform: str | None = None,
    db=database,
) -> AccountOperationLease:
    """Acquire inside the caller's transaction after locking the account.

    Lease rows are append-only.  Updating one row per account would either be
    blocked by historical foreign keys or rewrite the identity observed by old
    tasks.  The account row serialises generation allocation and the active
    lease check.
    """

    normalized_account_id = int(account_id)
    normalized_kind = str(operation_kind or "").strip().lower()
    normalized_owner = str(owner_id or "").strip()
    if normalized_account_id <= 0 or not normalized_kind or not normalized_owner:
        raise ValueError("invalid_account_operation_lease_request")

    if expected_execution_revision is not None and (
        type(expected_execution_revision) is not int or expected_execution_revision <= 0
    ):
        raise ValueError("invalid_expected_execution_revision")
    normalized_platform = str(expected_platform or "").strip().lower()

    account = await db.fetch_one(
        """SELECT id, platform, status, execution_revision,
                  OCTET_LENGTH(encrypted_credential) AS credential_size
           FROM accounts WHERE id = :account_id FOR UPDATE""",
        {"account_id": normalized_account_id},
    )
    if not account:
        raise ValueError("account_not_found")
    if (
        str(account["status"] or "").strip().lower() != "ready"
        or int(account["credential_size"] or 0) <= 0
        or (normalized_platform and str(account["platform"] or "").strip().lower() != normalized_platform)
        or (
            expected_execution_revision is not None
            and int(account["execution_revision"] or 0) != expected_execution_revision
        )
    ):
        raise AccountOperationLeaseConflict(
            normalized_account_id,
            normalized_kind,
            "account_operation_account_changed",
        )
    active = await db.fetch_one(
        """SELECT lease_id, operation_kind, owner_id, expires_at
           FROM account_operation_leases
           WHERE account_id = :account_id
             AND released_at IS NULL
             AND expires_at > NOW()
           ORDER BY generation DESC
           LIMIT 1
           FOR UPDATE""",
        {"account_id": normalized_account_id},
    )
    if active:
        raise AccountOperationLeaseConflict(normalized_account_id, normalized_kind)
    generation_row = await db.fetch_one(
        """SELECT COALESCE(MAX(generation), 0) + 1 AS next_generation
           FROM account_operation_leases
           WHERE account_id = :account_id""",
        {"account_id": normalized_account_id},
    )
    generation = int(generation_row["next_generation"] or 1) if generation_row else 1
    lease_id = str(uuid.uuid4())
    await db.execute(
        f"""INSERT INTO account_operation_leases
               (lease_id, account_id, generation, operation_kind, owner_id,
                task_id, acquired_at, expires_at, released_at)
            VALUES
               (:lease_id, :account_id, :generation, :operation_kind, :owner_id,
                NULL, NOW(), DATE_ADD(NOW(), INTERVAL {LEASE_MINUTES} MINUTE), NULL)""",
        {
            "lease_id": lease_id,
            "account_id": normalized_account_id,
            "generation": generation,
            "operation_kind": normalized_kind,
            "owner_id": normalized_owner,
        },
    )
    return AccountOperationLease(
        account_id=normalized_account_id,
        lease_id=lease_id,
        generation=generation,
        operation_kind=normalized_kind,
        owner_id=normalized_owner,
    )


async def bind_lease_to_task(
    lease: AccountOperationLease,
    task_id: str,
    *,
    db=database,
) -> None:
    if str(task_id) != lease.owner_id:
        raise ValueError("account_lease_owner_task_mismatch")
    await db.execute(
        """UPDATE account_operation_leases
           SET task_id = :task_id
           WHERE lease_id = :lease_id
             AND account_id = :account_id
             AND generation = :generation
             AND task_id IS NULL
             AND released_at IS NULL""",
        {
            "task_id": str(task_id),
            "lease_id": lease.lease_id,
            "account_id": lease.account_id,
            "generation": lease.generation,
        },
    )
    affected = await db.fetch_one("SELECT ROW_COUNT() AS affected")
    if not affected or int(affected["affected"] or 0) != 1:
        raise RuntimeError("account_lease_task_binding_lost")
