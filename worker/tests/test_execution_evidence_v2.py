import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "httpx" not in sys.modules and importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.AsyncBaseTransport = object
    httpx.AsyncClient = object
    httpx.TransportError = RuntimeError
    sys.modules["httpx"] = httpx

from app.action_plan import compute_bilibili_api_config_hash  # noqa: E402
from app.bilibili.preflight import (  # noqa: E402
    API_PREFLIGHT_KIND,
    hash_preflight_observation,
)
from app.services.execution_evidence import (  # noqa: E402
    _probe_proves_api_path,
    _released_fresh_source,
    materialize_for_probe,
)


def observation():
    config_hash = compute_bilibili_api_config_hash(4)
    return {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": "bilibili_api_v2",
        "preflight_contract_version": 1,
        "execution_revision": 4,
        "config_hash": config_hash,
        "side_effects": False,
        "account_authenticated": True,
        "api_preflight_complete": True,
        "requested_dynamic_id": "123456789012",
        "observed_dynamic_id": "123456789012",
        "target_type": 4,
        "target_uid": 10086,
        "author_handle": "@ASUS华硕官方UP",
        "follow_target_handle": "@ASUS华硕官方UP",
        "target_identity": {
            "verified": True,
            "dynamic_id": "123456789012",
            "author_uid": 10086,
            "author_handle": "@ASUS华硕官方UP",
        },
        "comment_rid_str": "123456789012",
        "comment_type": 17,
        "required_actions": ["followed", "liked"],
        "api_actions": ["follow", "like"],
        "capability_checks": {"followed": True, "liked": True},
    }


class RejectBeforeTransactionDatabase:
    def __init__(self, row):
        self.row = row
        self.transaction_used = False

    async def fetch_one(self, _query, _values=None):
        return self.row

    def transaction(self):
        self.transaction_used = True
        raise AssertionError("invalid source must not reach materialization transaction")


class ExecutionEvidenceV2Tests(unittest.TestCase):
    def source_row(self):
        value = observation()
        return {
            "probe_id": "probe-1",
            "lottery_id": 7,
            "account_id": 9,
            "platform": "bilibili",
            "rule_snapshot_id": 11,
            "execution_path_id": "bilibili_api_v2",
            "target_hash": "a" * 64,
            "rule_hash": "b" * 64,
            "action_plan_hash": "c" * 64,
            "config_hash": compute_bilibili_api_config_hash(4),
            "status": "succeeded",
            "result": json.dumps(value, ensure_ascii=False),
            "observation_kind": API_PREFLIGHT_KIND,
            "observation_hash": hash_preflight_observation(value),
            "finished_at": "2026-07-21T00:00:00Z",
            "source_fresh": 1,
            "source_lease_released": 1,
            "source_lease_covers_observation": 1,
        }

    def test_source_requires_freshness_release_and_lease_window_coverage(self):
        row = self.source_row()
        self.assertTrue(_released_fresh_source(row))
        for field in (
            "source_fresh",
            "source_lease_released",
            "source_lease_covers_observation",
        ):
            with self.subTest(field=field):
                changed = dict(row)
                changed[field] = 0
                self.assertFalse(_released_fresh_source(changed))

    def test_expired_lease_window_cannot_enter_materializer(self):
        row = self.source_row()
        row["source_lease_covers_observation"] = 0
        db = RejectBeforeTransactionDatabase(row)
        result = asyncio.run(materialize_for_probe(db=db, probe_id="probe-1"))
        self.assertIsNone(result)
        self.assertFalse(db.transaction_used)

    def test_lease_released_before_observation_finished_cannot_materialize(self):
        row = self.source_row()
        # SQL derives both flags as zero when released_at < finished_at.
        row["source_lease_released"] = 0
        row["source_lease_covers_observation"] = 0
        db = RejectBeforeTransactionDatabase(row)
        result = asyncio.run(materialize_for_probe(db=db, probe_id="probe-1"))
        self.assertIsNone(result)
        self.assertFalse(db.transaction_used)

    def test_observation_kind_hash_and_exact_follow_identity_are_immutable(self):
        row = self.source_row()
        self.assertTrue(_probe_proves_api_path(row))
        row["observation_hash"] = "0" * 64
        self.assertFalse(_probe_proves_api_path(row))


if __name__ == "__main__":
    unittest.main(verbosity=2)
