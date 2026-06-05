
from enum import StrEnum

from app.db import database

from app.utils.log import structured_log



class AccountStatus(StrEnum):

    COLD = "cold"

    LOGIN_REQUIRED = "login_required"

    WARMING = "warming"

    READY = "ready"

    EXECUTING = "executing"

    COOLING = "cooling"

    FROZEN = "frozen"

    BANNED = "banned"



TRANSITIONS = {

    AccountStatus.COLD: {AccountStatus.LOGIN_REQUIRED, AccountStatus.WARMING, AccountStatus.FROZEN, AccountStatus.BANNED},

    AccountStatus.LOGIN_REQUIRED: {AccountStatus.WARMING, AccountStatus.FROZEN, AccountStatus.BANNED},

    AccountStatus.WARMING: {AccountStatus.READY, AccountStatus.COOLING, AccountStatus.LOGIN_REQUIRED, AccountStatus.FROZEN},

    AccountStatus.READY: {AccountStatus.EXECUTING, AccountStatus.COOLING, AccountStatus.LOGIN_REQUIRED, AccountStatus.FROZEN},

    AccountStatus.EXECUTING: {AccountStatus.READY, AccountStatus.COOLING, AccountStatus.LOGIN_REQUIRED, AccountStatus.BANNED},

    AccountStatus.COOLING: {AccountStatus.READY, AccountStatus.FROZEN, AccountStatus.LOGIN_REQUIRED},

    AccountStatus.FROZEN: {AccountStatus.LOGIN_REQUIRED, AccountStatus.COLD},

    AccountStatus.BANNED: set(),

}



class InvalidTransitionError(Exception): ...



async def transition_account(account_id: int, current_version: int,

                             target: AccountStatus, db=database) -> bool:
    current = await db.fetch_one(
        "SELECT status FROM accounts WHERE id = :account_id AND version = :current_version",
        {"account_id": account_id, "current_version": current_version},
    )
    if not current:
        raise InvalidTransitionError(f"Version conflict for account {account_id}")

    current_status = AccountStatus(current["status"])
    target_status = AccountStatus(target.value if hasattr(target, "value") else target)
    if current_status == target_status:
        return True
    if target_status not in TRANSITIONS[current_status]:
        raise InvalidTransitionError(
            f"Invalid transition for account {account_id}: {current_status.value} -> {target_status.value}"
        )

    query = """

        UPDATE accounts

        SET status = :status, version = version + 1, updated_at = NOW()

        WHERE id = :account_id AND version = :current_version

    """

    res = await db.execute(query, {

        "status": target_status.value,

        "account_id": account_id,

        "current_version": current_version

    })

    if res == 0:

        raise InvalidTransitionError(f"Version conflict for account {account_id}")

    structured_log("info", "state_transition", account_id=account_id, to=target_status.value)

    return True
