"""Recovery must authorize a Probe with its committed outbox payload."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app import adapter_probe  # noqa: E402
from app.action_plan import compute_target_hash  # noqa: E402
from app.platform_modules import bilibili as bilibili_module  # noqa: E402


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def exact_message() -> dict[str, str]:
    canonical_url = "canonical://douyin/video/1234567890123456789"
    return {
        "probe_id": "probe-1",
        "platform": "douyin",
        "account_id": "7",
        "lottery_id": "11",
        "target_url": "https://www.douyin.com/video/1234567890123456789",
        "canonical_url": canonical_url,
        "execution_path_id": "douyin_selector_v1",
        "target_hash": compute_target_hash(canonical_url),
        "rule_snapshot_id": "",
        "rule_hash": "",
        "action_plan_hash": "",
        "config_hash": "a" * 64,
        "execution_revision": "3",
        "account_lease_id": "lease-probe-1",
        "account_lease_generation": "4",
    }


class FakeDatabase:
    def __init__(
        self,
        *,
        status: str = "queued",
        stale_running: int = 0,
        has_outbox: bool = True,
    ):
        self.message = exact_message()
        self.row = {
            "probe_id": "probe-1",
            "platform": "douyin",
            "account_id": 7,
            "lottery_id": 11,
            "target_url": self.message["target_url"],
            "status": status,
            "execution_path_id": "douyin_selector_v1",
            "target_hash": self.message["target_hash"],
            "rule_snapshot_id": None,
            "rule_hash": None,
            "action_plan_hash": None,
            "config_hash": "a" * 64,
            "account_lease_id": "lease-probe-1",
            "account_lease_generation": 4,
            "stale_running": stale_running,
            "canonical_url": self.message["canonical_url"],
            "execution_revision": 3,
        }
        self.affected = 0
        self.lease_released = False
        self.failure_updates = 0
        self.outbox_events: list[dict] = []
        self.transport_outbox = (
            {
                "stream_key": adapter_probe.STREAM_KEY,
                "payload": json.dumps(self.message),
            }
            if has_outbox
            else None
        )

    def transaction(self):
        return Tx()

    async def fetch_one(self, query, values=None):
        if "FROM outbox_events" in query:
            return self.transport_outbox
        if "SELECT probe_id FROM adapter_calibrations" in query:
            return {"probe_id": self.row["probe_id"]}
        if "SELECT ac.status" in query:
            return {
                "status": self.row["status"],
                "started_at": "started",
                "acquired_at": "acquired",
                "lease_active": 1,
                "lease_unreleased": 1,
                "lease_latest_generation": 1,
                "active_account_lease_count": 1,
            }
        if "FROM adapter_calibrations" in query:
            return dict(self.row)
        if "FROM account_operation_leases" in query:
            return {"released_at": "now" if self.lease_released else None}
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        self.affected = 0
        if "UPDATE adapter_calibrations" in query and "SET status = 'succeeded'" in query:
            if self.row["status"] == "running":
                self.row["status"] = "succeeded"
                self.affected = 1
            return
        if "UPDATE adapter_calibrations" in query and "SET status = 'failed'" in query:
            expected_status = str((values or {}).get("status") or "queued")
            if self.row["status"] == expected_status:
                self.row["status"] = "failed"
                self.failure_updates += 1
                self.affected = 1
            return
        if "UPDATE account_operation_leases" in query:
            self.lease_released = True
            return
        if "INSERT INTO task_outbox_events" in query:
            self.outbox_events.append(dict(values or {}))
            return
        if "INSERT INTO outbox_events" in query:
            self.transport_outbox = {
                "stream_key": (values or {})["stream_key"],
                "payload": (values or {})["payload"],
            }
            return
        raise AssertionError(f"unexpected execute: {query}")


class FakeRedis:
    def __init__(self, message: dict[str, str]):
        self.message = message
        self.acks: list[str] = []
        self.pending_kwargs: dict = {}
        self.eval_calls: list[tuple] = []

    async def xpending_range(self, *_args, **kwargs):
        self.pending_kwargs = dict(kwargs)
        return [{"message_id": "1-0", "time_since_delivered": 999_999}]

    async def xclaim(self, *_args, **_kwargs):
        return [("1-0", dict(self.message))]

    async def xack(self, _stream, _group, message_id):
        self.acks.append(str(message_id))
        return 1

    async def eval(self, *args):
        self.eval_calls.append(tuple(args))
        return "9-0"


class AdapterProbeRecoveryAuthorityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def binding() -> dict:
        return {
            "probe_id": "probe-1",
            "platform": "douyin",
            "account_id": 7,
            "lottery_id": 11,
            "target_url": exact_message()["target_url"],
            "account_lease_id": "lease-probe-1",
            "account_lease_generation": 4,
        }

    async def test_success_settlement_commits_durable_event_before_release(self):
        fake = FakeDatabase(status="running")

        with patch.object(adapter_probe, "database", fake):
            await adapter_probe.settle_probe_success(
                self.binding(),
                result={"selector_observation_complete": True},
                success_event_type="AdapterProbeSucceeded",
                success_event_payload={"side_effects": False},
            )

        self.assertEqual(fake.row["status"], "succeeded")
        self.assertTrue(fake.lease_released)
        self.assertEqual(len(fake.outbox_events), 1)
        self.assertEqual(
            fake.outbox_events[0]["dedup_key"],
            "adapter-probe-success:probe-1",
        )

    async def test_forged_stale_message_cannot_fail_canonical_probe(self):
        fake = FakeDatabase()
        forged = exact_message()
        forged["platform"] = "weibo"

        with patch.object(adapter_probe, "database", fake):
            state = await adapter_probe.settle_stale_probe(forged)

        self.assertEqual(state, "binding_mismatch")
        self.assertEqual(fake.row["status"], "queued")
        self.assertEqual(fake.failure_updates, 0)
        self.assertFalse(fake.lease_released)
        self.assertEqual(fake.outbox_events, [])

    async def test_exact_stale_queued_message_fails_and_releases_lease(self):
        fake = FakeDatabase()

        with patch.object(adapter_probe, "database", fake):
            state = await adapter_probe.settle_stale_probe(exact_message())

        self.assertEqual(state, "failed")
        self.assertEqual(fake.row["status"], "failed")
        self.assertEqual(fake.failure_updates, 1)
        self.assertTrue(fake.lease_released)
        self.assertEqual(len(fake.outbox_events), 1)
        self.assertEqual(
            fake.outbox_events[0]["dedup_key"],
            "adapter-probe-failed:probe-1",
        )

    async def test_exact_active_running_message_is_retained(self):
        fake = FakeDatabase(status="running", stale_running=0)

        with patch.object(adapter_probe, "database", fake):
            state = await adapter_probe.settle_stale_probe(exact_message())

        self.assertEqual(state, "active")
        self.assertFalse(fake.lease_released)

    async def test_terminal_duplicate_releases_residual_lease(self):
        fake = FakeDatabase(status="succeeded")

        with patch.object(adapter_probe, "database", fake):
            state = await adapter_probe.settle_stale_probe(exact_message())

        self.assertEqual(state, "terminal")
        self.assertTrue(fake.lease_released)

    async def test_rejected_forged_message_is_discarded_without_db_mutation(self):
        fake = FakeDatabase()
        forged = exact_message()
        forged["config_hash"] = "b" * 64

        with patch.object(adapter_probe, "database", fake):
            acknowledge = await adapter_probe.settle_rejected_probe_claim(
                forged,
                ValueError("adapter_probe_selector_binding_mismatch"),
            )

        self.assertTrue(acknowledge)
        self.assertEqual(fake.row["status"], "queued")
        self.assertEqual(fake.failure_updates, 0)
        self.assertFalse(fake.lease_released)

    async def test_rejected_terminal_duplicate_releases_residual_lease(self):
        fake = FakeDatabase(status="failed")

        with patch.object(adapter_probe, "database", fake):
            acknowledge = await adapter_probe.settle_rejected_probe_claim(
                exact_message(),
                ValueError("adapter_probe_not_queued"),
            )

        self.assertTrue(acknowledge)
        self.assertTrue(fake.lease_released)

    async def test_succeeded_bilibili_duplicate_repairs_evidence_before_ack(
        self,
    ):
        fake = FakeDatabase(status="succeeded")
        binding = {
            **self.binding(),
            "platform": "bilibili",
        }
        row = {**fake.row, "status": "succeeded"}
        materialize = AsyncMock()

        with patch.object(adapter_probe, "database", fake), patch.object(
            adapter_probe,
            "_lock_authoritative_probe_stream_binding",
            AsyncMock(return_value=("exact", row, binding)),
        ), patch.object(
            adapter_probe,
            "_release_probe_lease",
            AsyncMock(),
        ), patch.object(
            bilibili_module,
            "materialize_for_probe",
            materialize,
        ):
            acknowledge = await adapter_probe.settle_rejected_probe_claim(
                exact_message(),
                ValueError("adapter_probe_not_queued"),
            )

        self.assertTrue(acknowledge)
        materialize.assert_awaited_once_with(
            db=fake,
            probe_id="probe-1",
        )

    async def test_succeeded_bilibili_duplicate_retains_when_repair_fails(
        self,
    ):
        fake = FakeDatabase(status="succeeded")
        binding = {
            **self.binding(),
            "platform": "bilibili",
        }
        row = {**fake.row, "status": "succeeded"}

        with patch.object(adapter_probe, "database", fake), patch.object(
            adapter_probe,
            "_lock_authoritative_probe_stream_binding",
            AsyncMock(return_value=("exact", row, binding)),
        ), patch.object(
            adapter_probe,
            "_release_probe_lease",
            AsyncMock(),
        ), patch.object(
            bilibili_module,
            "materialize_for_probe",
            AsyncMock(side_effect=RuntimeError("db unavailable")),
        ), patch.object(adapter_probe, "structured_log"):
            acknowledge = await adapter_probe.settle_rejected_probe_claim(
                exact_message(),
                ValueError("adapter_probe_not_queued"),
            )

        self.assertFalse(acknowledge)

    async def test_reclaimer_acks_only_the_forged_delivery(self):
        fake_db = FakeDatabase()
        forged = exact_message()
        forged["account_id"] = "99"
        fake_redis = FakeRedis(forged)

        with patch.object(adapter_probe, "database", fake_db), patch.object(
            adapter_probe, "redis", fake_redis
        ), patch.object(adapter_probe, "structured_log"):
            settled = await adapter_probe.reclaim_stale_probe_messages()

        self.assertEqual(settled, 1)
        self.assertEqual(fake_redis.acks, ["1-0"])
        self.assertEqual(
            fake_redis.pending_kwargs["idle"],
            adapter_probe.PROBE_IDLE_THRESHOLD_MS,
        )
        self.assertEqual(fake_db.row["status"], "queued")
        self.assertFalse(fake_db.lease_released)

    async def test_legacy_direct_xadd_is_migrated_before_atomic_fanout(self):
        fake_db = FakeDatabase(has_outbox=False)
        legacy_message = {
            key: exact_message()[key]
            for key in (
                "probe_id",
                "platform",
                "account_id",
                "lottery_id",
                "target_url",
                "account_lease_id",
                "account_lease_generation",
            )
        }
        fake_redis = FakeRedis(legacy_message)

        with patch.object(adapter_probe, "database", fake_db), patch.object(
            adapter_probe, "redis", fake_redis
        ), patch.object(adapter_probe, "structured_log"):
            transferred = await adapter_probe._fanout_legacy_probe_message(
                "1-0",
                legacy_message,
            )

        self.assertTrue(transferred)
        self.assertIsNotNone(fake_db.transport_outbox)
        migrated = json.loads(fake_db.transport_outbox["payload"])
        self.assertEqual(migrated, exact_message())
        self.assertEqual(len(fake_redis.eval_calls), 1)
        eval_call = fake_redis.eval_calls[0]
        self.assertIn("XADD", eval_call[0])
        self.assertIn("XACK", eval_call[0])
        self.assertEqual(
            eval_call[2],
            adapter_probe.LEGACY_ADAPTER_PROBE_STREAM_KEY,
        )
        self.assertEqual(
            eval_call[3],
            adapter_probe.adapter_probe_stream_binding_for_platform(
                "douyin"
            ).stream_key,
        )

    async def test_legacy_running_probe_moves_to_lane_recovery(self):
        fake_db = FakeDatabase(status="running", stale_running=1)
        fake_redis = FakeRedis(exact_message())
        target_stream = (
            adapter_probe.adapter_probe_stream_binding_for_platform(
                "douyin"
            ).stream_key
        )

        with patch.object(adapter_probe, "database", fake_db), patch.object(
            adapter_probe, "redis", fake_redis
        ), patch.object(adapter_probe, "structured_log"):
            transferred = await adapter_probe._fanout_legacy_probe_message(
                "1-0",
                exact_message(),
            )
            state = await adapter_probe.settle_stale_probe(
                exact_message(),
                source_stream_key=target_stream,
            )

        self.assertTrue(transferred)
        self.assertEqual(state, "failed")
        self.assertEqual(fake_db.row["status"], "failed")
        self.assertTrue(fake_db.lease_released)
        self.assertEqual(len(fake_redis.eval_calls), 1)

    async def test_post_settlement_materialization_failure_retains_delivery(self):
        observation = adapter_probe.ProbeObservation(
            result={"ok": True},
            success_event_type="AdapterApiProbeSucceeded",
            success_event_payload={"side_effects": False},
            materialize_execution_evidence=True,
        )
        platform_module = SimpleNamespace(
            execute_probe=AsyncMock(return_value=observation),
            materialize_terminal_probe=AsyncMock(
                side_effect=RuntimeError("db")
            ),
        )
        success = AsyncMock()
        failure = AsyncMock()

        with patch.object(
            adapter_probe, "claim_probe", AsyncMock(return_value=self.binding())
        ), patch.object(
            adapter_probe, "get_platform_module", return_value=platform_module
        ), patch.object(
            adapter_probe, "settle_probe_success", success
        ), patch.object(
            adapter_probe, "settle_probe_failure", failure
        ), patch.object(adapter_probe, "record_event", AsyncMock()), patch.object(
            adapter_probe, "structured_log"
        ):
            acknowledge = await adapter_probe.handle_probe(object(), exact_message())

        self.assertFalse(acknowledge)
        success.assert_awaited_once()
        failure.assert_not_awaited()

    async def test_execution_deadline_cancels_handler_before_stale_window(self):
        async def never_finishes(_binding, _pool):
            await asyncio.Event().wait()

        platform_module = SimpleNamespace(execute_probe=never_finishes)
        failure = AsyncMock(return_value=True)
        fake_redis = SimpleNamespace(xack=AsyncMock(return_value=1))
        dispatched = adapter_probe._DispatchedProbeMessage(
            message_id="1-0",
            probe=exact_message(),
            platform="douyin",
        )
        with patch.object(
            adapter_probe, "claim_probe", AsyncMock(return_value=self.binding())
        ), patch.object(
            adapter_probe, "get_platform_module", return_value=platform_module
        ), patch.object(
            adapter_probe, "settle_probe_failure", failure
        ), patch.object(
            adapter_probe, "PROBE_EXECUTION_TIMEOUT_SECONDS", 0.01
        ), patch.object(
            adapter_probe, "redis", fake_redis
        ), patch.object(adapter_probe, "record_event", AsyncMock()), patch.object(
            adapter_probe, "structured_log"
        ):
            await adapter_probe._execute_dispatched_probe(
                dispatched,
                asyncio.Lock(),
                object(),
            )

        failure.assert_awaited_once()
        fake_redis.xack.assert_awaited_once_with(
            adapter_probe.STREAM_KEY,
            adapter_probe.GROUP_NAME,
            "1-0",
        )


if __name__ == "__main__":
    unittest.main()
