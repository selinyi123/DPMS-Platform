"""Pure cross-process contracts for durable lottery execution intents.

Core and Worker intentionally validate the full intent payload independently,
but the lease fencing operation must have one exact meaning on both sides.
Keeping this tiny mapping in ``shared`` prevents a repair task from being
created with one lease kind and consumed or released as another.
"""

from __future__ import annotations

from types import MappingProxyType


FULL_EXECUTION_INTENT_KIND = "full"
REPAIR_EXECUTION_INTENT_KIND = "repair"
LEGACY_FULL_EXECUTION_INTENT_KIND = "legacy_full"

EXECUTION_INTENT_LEASE_OPERATION_KINDS = MappingProxyType(
    {
        FULL_EXECUTION_INTENT_KIND: "real_run",
        REPAIR_EXECUTION_INTENT_KIND: "repair_run",
        LEGACY_FULL_EXECUTION_INTENT_KIND: "real_run",
    }
)


def lease_operation_kind_for_execution_intent(
    execution_intent_kind: str,
) -> str:
    """Return the only lease operation authorized by an intent kind.

    Values are deliberately not normalized. Persisted and message contracts
    must already contain the exact canonical token; accepting whitespace or
    case variants here would weaken the binding at a fencing boundary.
    """

    if not isinstance(execution_intent_kind, str):
        raise ValueError("execution_intent_kind_invalid")
    operation_kind = EXECUTION_INTENT_LEASE_OPERATION_KINDS.get(
        execution_intent_kind
    )
    if operation_kind is None:
        raise ValueError("execution_intent_kind_invalid")
    return operation_kind


__all__ = (
    "EXECUTION_INTENT_LEASE_OPERATION_KINDS",
    "FULL_EXECUTION_INTENT_KIND",
    "LEGACY_FULL_EXECUTION_INTENT_KIND",
    "REPAIR_EXECUTION_INTENT_KIND",
    "lease_operation_kind_for_execution_intent",
)
