"""Durable, fail-closed fencing for real external action attempts.

An intent is written as ``prepared`` and then changed to ``started`` before the
HTTP request is sent.  A worker that later observes ``started`` cannot know
whether the remote platform received the request, so it converts the intent to
``unknown``, marks the task for reconciliation, and refuses automatic replay.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from app.action_plan import canonical_json_bytes


LEASE_OPERATION_KIND = "real_run"
INTENT_PREPARED = "prepared"
INTENT_STARTED = "started"
INTENT_SUCCEEDED = "succeeded"
INTENT_FAILED = "failed"
INTENT_UNKNOWN = "unknown"
EFFECT_NOT_STARTED = "not_started"
EFFECT_UNKNOWN = "unknown"
EFFECT_CONFIRMED = "confirmed_effect"
EFFECT_CONFIRMED_NONE = "confirmed_no_effect"
EXPECTED_EFFECT_CERTAINTY = {
    INTENT_PREPARED: EFFECT_NOT_STARTED,
    INTENT_STARTED: EFFECT_UNKNOWN,
    INTENT_SUCCEEDED: EFFECT_CONFIRMED,
    INTENT_FAILED: EFFECT_CONFIRMED_NONE,
    INTENT_UNKNOWN: EFFECT_UNKNOWN,
}
KNOWN_INTENT_STATES = frozenset(
    {INTENT_PREPARED, INTENT_STARTED, INTENT_SUCCEEDED, INTENT_FAILED, INTENT_UNKNOWN}
)
CONFIRMED_SUCCESS_OUTCOMES = frozenset({"ok"})
# Only classified Bilibili business responses prove that the mutation did not
# take effect. Transport failures, timeouts, fatal/unrecognized codes and local
# exceptions all retain ``unknown`` certainty and must use
# :func:`mark_action_intent_unknown`.
CONFIRMED_NO_EFFECT_OUTCOMES = frozenset(
    {"retry", "limit", "skip", "captcha", "risk", "auth"}
)


class IntentDatabase(Protocol):
    def transaction(self): ...

    async def fetch_one(self, query: str, values: Mapping[str, Any] | None = None): ...

    async def execute(self, query: str, values: Mapping[str, Any] | None = None): ...


class ExternalActionIntentBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "external_action_intent_blocked")
        super().__init__(f"external_action_intent_blocked:{self.code}")


@dataclass(frozen=True)
class StartedActionIntent:
    intent_id: str
    task_id: str
    account_id: int
    lottery_id: int
    lease_id: str
    lease_generation: int
    action: str
    payload_hash: str
    attempt_no: int


_TASK_LEASE_FOR_UPDATE = """
SELECT
  tr.task_id,
  tr.account_id,
  tr.lottery_id,
  tr.status AS task_status,
  tr.worker_id,
  tr.account_lease_id,
  tr.account_lease_generation,
  tr.reconciliation_required,
  aol.lease_id,
  aol.generation AS lease_generation,
  aol.operation_kind,
  aol.owner_id,
  aol.task_id AS lease_task_id,
  CASE WHEN aol.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
  CASE WHEN aol.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
  CASE WHEN aol.generation = (
    SELECT MAX(newest.generation)
    FROM account_operation_leases newest
    WHERE newest.account_id = tr.account_id
  ) THEN 1 ELSE 0 END AS lease_latest_generation,
  (
    SELECT COUNT(*)
    FROM account_operation_leases live
    WHERE live.account_id = tr.account_id
      AND live.released_at IS NULL
      AND live.expires_at > NOW()
  ) AS active_account_lease_count
FROM task_runs tr
LEFT JOIN account_operation_leases aol
  ON aol.account_id = tr.account_id
 AND aol.lease_id = tr.account_lease_id
WHERE tr.task_id = :task_id
FOR UPDATE
"""


_INTENT_FOR_UPDATE = """
SELECT intent_id, task_id, account_id, lottery_id, lease_id,
       lease_generation, action, payload_hash, status, effect_certainty,
       attempt_no
FROM external_action_intents
WHERE task_id = :task_id AND action = :action
FOR UPDATE
"""


def _row_get(row: Any, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _positive_int(value: Any, *, code: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ExternalActionIntentBlocked(code) from exc
    if result <= 0:
        raise ExternalActionIntentBlocked(code)
    return result


def action_payload_hash(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise ExternalActionIntentBlocked("action_payload_invalid")
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def _validate_task_lease(
    row: Any,
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    worker_id: str,
) -> tuple[str, int]:
    if row is None:
        raise ExternalActionIntentBlocked("task_lease_missing")
    try:
        row_account_id = int(_row_get(row, "account_id"))
        row_lottery_id = int(_row_get(row, "lottery_id"))
    except (TypeError, ValueError) as exc:
        raise ExternalActionIntentBlocked("task_lease_binding_invalid") from exc
    lease_id = str(_row_get(row, "lease_id") or "").strip()
    task_lease_id = str(_row_get(row, "account_lease_id") or "").strip()
    generation = _positive_int(
        _row_get(row, "lease_generation"), code="account_lease_generation_invalid"
    )
    task_generation = _positive_int(
        _row_get(row, "account_lease_generation"),
        code="account_lease_generation_invalid",
    )
    if (
        str(_row_get(row, "task_id") or "").strip() != task_id
        or row_account_id != account_id
        or row_lottery_id != lottery_id
        or str(_row_get(row, "task_status") or "").strip().lower() != "running"
        or str(_row_get(row, "worker_id") or "").strip() != worker_id
        or not lease_id
        or lease_id != task_lease_id
        or generation != task_generation
        or str(_row_get(row, "operation_kind") or "").strip().lower()
        != LEASE_OPERATION_KIND
        or str(_row_get(row, "owner_id") or "").strip() != task_id
        or str(_row_get(row, "lease_task_id") or "").strip() != task_id
        or int(_row_get(row, "lease_active", 0) or 0) != 1
        or int(_row_get(row, "lease_unreleased", 0) or 0) != 1
        or int(_row_get(row, "lease_latest_generation", 0) or 0) != 1
        or int(_row_get(row, "active_account_lease_count", 0) or 0) != 1
    ):
        raise ExternalActionIntentBlocked("task_lease_binding_invalid")
    if int(_row_get(row, "reconciliation_required", 0) or 0) != 0:
        raise ExternalActionIntentBlocked("task_reconciliation_required")
    return lease_id, generation


def _validate_existing_intent(
    row: Any,
    *,
    task_id: str,
    account_id: int,
    lottery_id: int,
    lease_id: str,
    lease_generation: int,
    action: str,
    payload_hash: str,
) -> tuple[str, int]:
    try:
        row_account_id = int(_row_get(row, "account_id"))
        row_lottery_id = int(_row_get(row, "lottery_id"))
        row_generation = int(_row_get(row, "lease_generation"))
        attempt_no = int(_row_get(row, "attempt_no"))
    except (TypeError, ValueError) as exc:
        raise ExternalActionIntentBlocked("intent_binding_invalid") from exc
    status = str(_row_get(row, "status") or "").strip().lower()
    effect_certainty = str(_row_get(row, "effect_certainty") or "").strip().lower()
    if (
        str(_row_get(row, "task_id") or "").strip() != task_id
        or row_account_id != account_id
        or row_lottery_id != lottery_id
        or str(_row_get(row, "lease_id") or "").strip() != lease_id
        or row_generation != lease_generation
        or str(_row_get(row, "action") or "").strip().lower() != action
        or str(_row_get(row, "payload_hash") or "").strip() != payload_hash
        or status not in KNOWN_INTENT_STATES
        or effect_certainty != EXPECTED_EFFECT_CERTAINTY.get(status)
        or attempt_no <= 0
    ):
        raise ExternalActionIntentBlocked("intent_binding_invalid")
    return status, attempt_no


async def _mark_task_reconciliation(
    db: IntentDatabase,
    *,
    task_id: str,
    note: str,
) -> None:
    await db.execute(
        """UPDATE task_runs
           SET reconciliation_required = 1,
               error_message = COALESCE(error_message, :note)
           WHERE task_id = :task_id""",
        {"task_id": task_id, "note": note[:255]},
    )


async def prepare_and_start_action_intent(
    *,
    db: IntentDatabase,
    task_id: str,
    account_id: int,
    lottery_id: int,
    worker_id: str,
    action: str,
    payload: Mapping[str, Any],
) -> StartedActionIntent:
    """Fence one remote attempt before any network mutation can start."""

    task_id = str(task_id or "").strip()
    worker_id = str(worker_id or "").strip()
    action = str(action or "").strip().lower()
    if not task_id or not worker_id or not action:
        raise ExternalActionIntentBlocked("intent_request_invalid")
    payload_hash = action_payload_hash(payload)
    blocked_after_commit: str | None = None
    started: StartedActionIntent | None = None

    async with db.transaction():
        lease_row = await db.fetch_one(_TASK_LEASE_FOR_UPDATE, {"task_id": task_id})
        lease_id, lease_generation = _validate_task_lease(
            lease_row,
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            worker_id=worker_id,
        )
        row = await db.fetch_one(
            _INTENT_FOR_UPDATE, {"task_id": task_id, "action": action}
        )
        if row is None:
            intent_id = str(uuid.uuid4())
            attempt_no = 1
            await db.execute(
                """INSERT INTO external_action_intents
                     (intent_id, task_id, account_id, lottery_id, lease_id,
                      lease_generation, action, payload_hash, status,
                      effect_certainty, attempt_no)
                   VALUES
                     (:intent_id, :task_id, :account_id, :lottery_id, :lease_id,
                      :lease_generation, :action, :payload_hash, 'prepared',
                      'not_started', :attempt_no)""",
                {
                    "intent_id": intent_id,
                    "task_id": task_id,
                    "account_id": account_id,
                    "lottery_id": lottery_id,
                    "lease_id": lease_id,
                    "lease_generation": lease_generation,
                    "action": action,
                    "payload_hash": payload_hash,
                    "attempt_no": attempt_no,
                },
            )
        else:
            intent_id = str(_row_get(row, "intent_id") or "").strip()
            status, attempt_no = _validate_existing_intent(
                row,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                lease_id=lease_id,
                lease_generation=lease_generation,
                action=action,
                payload_hash=payload_hash,
            )
            if status == INTENT_STARTED:
                await db.execute(
                    """UPDATE external_action_intents
                       SET status = 'unknown', effect_certainty = 'unknown',
                           started_at = COALESCE(started_at, NOW()),
                           completed_at = COALESCE(completed_at, NOW()),
                           outcome = 'unknown',
                           reconciliation_note = :note
                       WHERE intent_id = :intent_id AND status = 'started'""",
                    {
                        "intent_id": intent_id,
                        "note": "unsettled started intent observed before automatic replay",
                    },
                )
                await _mark_task_reconciliation(
                    db,
                    task_id=task_id,
                    note=f"external action {action} requires reconciliation",
                )
                blocked_after_commit = "intent_started_requires_reconciliation"
            elif status == INTENT_UNKNOWN:
                await _mark_task_reconciliation(
                    db,
                    task_id=task_id,
                    note=f"external action {action} requires reconciliation",
                )
                blocked_after_commit = "intent_unknown_requires_reconciliation"
            elif status == INTENT_SUCCEEDED:
                # A crash can occur after the remote success was durably
                # journaled but before the phase/ledger settlement committed.
                # Never turn that gap into a fresh full-task retry.
                await _mark_task_reconciliation(
                    db,
                    task_id=task_id,
                    note=f"external action {action} succeeded but local phase requires reconciliation",
                )
                blocked_after_commit = "intent_succeeded_requires_reconciliation"
            elif status == INTENT_FAILED:
                attempt_no += 1
                await db.execute(
                    """UPDATE external_action_intents
                       SET status = 'prepared', effect_certainty = 'not_started',
                           attempt_no = :attempt_no,
                           started_at = NULL, completed_at = NULL, outcome = NULL,
                           remote_ref = NULL, error_message = NULL,
                           reconciliation_note = NULL
                       WHERE intent_id = :intent_id AND status = 'failed'""",
                    {"intent_id": intent_id, "attempt_no": attempt_no},
                )

        if blocked_after_commit is None:
            await db.execute(
                """UPDATE external_action_intents
                   SET status = 'started', effect_certainty = 'unknown',
                       started_at = NOW(), completed_at = NULL
                   WHERE intent_id = :intent_id AND status = 'prepared'
                     AND attempt_no = :attempt_no""",
                {"intent_id": intent_id, "attempt_no": attempt_no},
            )
            persisted = await db.fetch_one(
                """SELECT intent_id, task_id, account_id, lottery_id, lease_id,
                           lease_generation, action, payload_hash, status,
                           effect_certainty, attempt_no
                   FROM external_action_intents
                   WHERE intent_id = :intent_id""",
                {"intent_id": intent_id},
            )
            status, persisted_attempt = _validate_existing_intent(
                persisted,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                lease_id=lease_id,
                lease_generation=lease_generation,
                action=action,
                payload_hash=payload_hash,
            )
            if status != INTENT_STARTED or persisted_attempt != attempt_no:
                raise ExternalActionIntentBlocked("intent_start_not_persisted")
            started = StartedActionIntent(
                intent_id=intent_id,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                lease_id=lease_id,
                lease_generation=lease_generation,
                action=action,
                payload_hash=payload_hash,
                attempt_no=attempt_no,
            )

    if blocked_after_commit is not None:
        raise ExternalActionIntentBlocked(blocked_after_commit)
    if started is None:  # defensive; all non-blocked branches create a value
        raise ExternalActionIntentBlocked("intent_start_not_persisted")
    return started


async def renew_account_operation_lease(
    *,
    db: IntentDatabase,
    task_id: str,
    account_id: int,
    lottery_id: int,
    worker_id: str,
) -> tuple[str, int]:
    """Renew only the exact active fencing generation owned by this task."""

    async with db.transaction():
        row = await db.fetch_one(_TASK_LEASE_FOR_UPDATE, {"task_id": task_id})
        lease_id, generation = _validate_task_lease(
            row,
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            worker_id=worker_id,
        )
        await db.execute(
            """UPDATE account_operation_leases
               SET expires_at = DATE_ADD(NOW(), INTERVAL 900 SECOND)
               WHERE account_id = :account_id AND lease_id = :lease_id
                 AND generation = :generation AND task_id = :task_id
                 AND owner_id = :task_id AND operation_kind = 'real_run'
                 AND released_at IS NULL AND expires_at > NOW()""",
            {
                "account_id": account_id,
                "lease_id": lease_id,
                "generation": generation,
                "task_id": task_id,
            },
        )
        persisted = await db.fetch_one(
            """SELECT lease_id, generation,
                      CASE WHEN expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                      CASE WHEN released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased
               FROM account_operation_leases
               WHERE account_id = :account_id AND lease_id = :lease_id
                 AND generation = :generation""",
            {
                "account_id": account_id,
                "lease_id": lease_id,
                "generation": generation,
            },
        )
        if (
            str(_row_get(persisted, "lease_id") or "").strip() != lease_id
            or int(_row_get(persisted, "generation", 0) or 0) != generation
            or int(_row_get(persisted, "lease_active", 0) or 0) != 1
            or int(_row_get(persisted, "lease_unreleased", 0) or 0) != 1
        ):
            raise ExternalActionIntentBlocked("account_lease_renewal_failed")
    return lease_id, generation


async def settle_action_intent(
    *,
    db: IntentDatabase,
    intent: StartedActionIntent,
    succeeded: bool,
    outcome: str,
    remote_ref: str | None = None,
    error_message: str | None = None,
) -> None:
    """Persist a confirmed remote success or explicit failure exactly once."""

    if type(succeeded) is not bool:
        raise ExternalActionIntentBlocked("intent_result_invalid")
    final_status = INTENT_SUCCEEDED if succeeded else INTENT_FAILED
    effect_certainty = EFFECT_CONFIRMED if succeeded else EFFECT_CONFIRMED_NONE
    normalized_outcome = str(outcome or "").strip().lower()
    allowed_outcomes = (
        CONFIRMED_SUCCESS_OUTCOMES if succeeded else CONFIRMED_NO_EFFECT_OUTCOMES
    )
    if normalized_outcome not in allowed_outcomes:
        raise ExternalActionIntentBlocked("intent_outcome_invalid")
    async with db.transaction():
        row = await db.fetch_one(
            """SELECT intent_id, task_id, account_id, lottery_id, lease_id,
                      lease_generation, action, payload_hash, status,
                      effect_certainty, attempt_no
               FROM external_action_intents
               WHERE intent_id = :intent_id FOR UPDATE""",
            {"intent_id": intent.intent_id},
        )
        status, attempt_no = _validate_existing_intent(
            row,
            task_id=intent.task_id,
            account_id=intent.account_id,
            lottery_id=intent.lottery_id,
            lease_id=intent.lease_id,
            lease_generation=intent.lease_generation,
            action=intent.action,
            payload_hash=intent.payload_hash,
        )
        if status != INTENT_STARTED or attempt_no != intent.attempt_no:
            raise ExternalActionIntentBlocked("intent_not_started")
        await db.execute(
            """UPDATE external_action_intents
               SET status = :status, effect_certainty = :effect_certainty,
                   completed_at = NOW(), outcome = :outcome,
                   remote_ref = :remote_ref, error_message = :error_message
               WHERE intent_id = :intent_id AND status = 'started'
                 AND attempt_no = :attempt_no""",
            {
                "intent_id": intent.intent_id,
                "attempt_no": intent.attempt_no,
                "status": final_status,
                "effect_certainty": effect_certainty,
                "outcome": normalized_outcome,
                "remote_ref": (str(remote_ref)[:512] if remote_ref else None),
                "error_message": (str(error_message)[:4096] if error_message else None),
            },
        )
        persisted = await db.fetch_one(
            "SELECT status, effect_certainty, attempt_no FROM external_action_intents WHERE intent_id = :intent_id",
            {"intent_id": intent.intent_id},
        )
        if (
            str(_row_get(persisted, "status") or "").strip().lower() != final_status
            or str(_row_get(persisted, "effect_certainty") or "").strip().lower()
            != effect_certainty
            or int(_row_get(persisted, "attempt_no", 0) or 0) != intent.attempt_no
        ):
            raise ExternalActionIntentBlocked("intent_settlement_not_persisted")


async def mark_action_intent_unknown(
    *,
    db: IntentDatabase,
    intent: StartedActionIntent,
    reason: str,
) -> None:
    """Quarantine an attempt whose remote outcome cannot be proven."""

    note = str(reason or "").strip()
    if not note:
        note = "external action outcome unknown"
    note = note[:4096]
    async with db.transaction():
        row = await db.fetch_one(
            """SELECT intent_id, task_id, account_id, lottery_id, lease_id,
                      lease_generation, action, payload_hash, status,
                      effect_certainty, attempt_no
               FROM external_action_intents
               WHERE intent_id = :intent_id FOR UPDATE""",
            {"intent_id": intent.intent_id},
        )
        status, attempt_no = _validate_existing_intent(
            row,
            task_id=intent.task_id,
            account_id=intent.account_id,
            lottery_id=intent.lottery_id,
            lease_id=intent.lease_id,
            lease_generation=intent.lease_generation,
            action=intent.action,
            payload_hash=intent.payload_hash,
        )
        if status not in {INTENT_STARTED, INTENT_UNKNOWN} or attempt_no != intent.attempt_no:
            raise ExternalActionIntentBlocked("intent_unknown_transition_invalid")
        await db.execute(
            """UPDATE external_action_intents
               SET status = 'unknown', effect_certainty = 'unknown',
                   started_at = COALESCE(started_at, NOW()),
                   completed_at = COALESCE(completed_at, NOW()),
                   outcome = 'unknown',
                   reconciliation_note = CASE
                     WHEN reconciliation_note IS NULL OR reconciliation_note = ''
                     THEN :note ELSE reconciliation_note
                   END
               WHERE intent_id = :intent_id
                 AND status IN ('started', 'unknown')
                 AND attempt_no = :attempt_no""",
            {
                "intent_id": intent.intent_id,
                "attempt_no": intent.attempt_no,
                "note": note,
            },
        )
        await _mark_task_reconciliation(
            db,
            task_id=intent.task_id,
            note=f"external action {intent.action} requires reconciliation",
        )
        persisted = await db.fetch_one(
            "SELECT status, effect_certainty FROM external_action_intents WHERE intent_id = :intent_id",
            {"intent_id": intent.intent_id},
        )
        if (
            str(_row_get(persisted, "status") or "").strip().lower()
            != INTENT_UNKNOWN
            or str(_row_get(persisted, "effect_certainty") or "").strip().lower()
            != EFFECT_UNKNOWN
        ):
            raise ExternalActionIntentBlocked("intent_unknown_not_persisted")
