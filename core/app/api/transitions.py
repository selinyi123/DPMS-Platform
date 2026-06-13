"""Transition Runtime API (V8 / stage S7).

Read-only views over ``policy_transitions``: the append-only audit trail of
how a policy lineage moved from version to version (why, what changed, what
to do if it needs to be undone). Publishing and activating new versions are
governance actions and live in ``app.api.governance``; this module only
reads the resulting lineage.
"""

from fastapi import APIRouter

from app.api.lotteries import parse_json_field
from app.db import database
from app.governance.policy import REAL_RUN_GATE_POLICY_KEY


router = APIRouter()


@router.get("/")
async def list_transitions(policy_key: str = REAL_RUN_GATE_POLICY_KEY):
    rows = await database.fetch_all(
        """SELECT id, policy_key, from_version, to_version, reason_code, classification,
                  trigger_event, experiment_id, diff, impact_scope, rollback_condition,
                  loosening_justification, requires_extra_review, activated_at, created_by, created_at
           FROM policy_transitions WHERE policy_key = :pk ORDER BY to_version ASC""",
        {"pk": policy_key},
    )
    items = [_serialize_transition(row) for row in rows]
    return {"items": items, "count": len(items)}


@router.get("/{policy_key}/lineage")
async def policy_lineage(policy_key: str):
    active_row = await database.fetch_one(
        "SELECT version FROM policy_versions WHERE policy_key = :pk AND active = 1 ORDER BY version DESC LIMIT 1",
        {"pk": policy_key},
    )
    active_version = active_row["version"] if active_row else None

    rows = await database.fetch_all(
        """SELECT id, policy_key, from_version, to_version, reason_code, classification,
                  trigger_event, experiment_id, diff, impact_scope, rollback_condition,
                  loosening_justification, requires_extra_review, activated_at, created_by, created_at
           FROM policy_transitions WHERE policy_key = :pk ORDER BY to_version ASC""",
        {"pk": policy_key},
    )
    chain = [_serialize_transition(row) for row in rows]
    return {"policy_key": policy_key, "active_version": active_version, "chain": chain}


def _serialize_transition(row) -> dict:
    item = dict(row)
    item["diff"] = parse_json_field(item.get("diff")) or {}
    item["impact_scope"] = parse_json_field(item.get("impact_scope")) or {}
    item["requires_extra_review"] = bool(item.get("requires_extra_review"))
    return item
