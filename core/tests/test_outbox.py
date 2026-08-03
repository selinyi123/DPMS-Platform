import base64
import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.outbox import (  # noqa: E402
    LOTTERY_TASK_FIELDS,
    OUTBOX_MAX_ATTEMPTS,
    _deliver_claimed,
    _outbox_delivery_loop,
    _outbox_lane_initial_delay,
    _outbox_reclaim_loop,
    _outbox_reconciliation_loop,
    _reject_plaintext_weibo_rip,
    _settle_terminal_delivery_failure,
    _validate_task_stream_binding,
    build_lottery_task_message,
    flush_pending_outbox,
    read_redis_task_stream_epoch,
    reconcile_redis_task_stream_epoch,
    reconcile_orphaned_locks,
    RedisTaskStreamEpochUnavailable,
    should_retry,
    terminal_status,
)
from app.task_streams import task_stream_bindings  # noqa: E402
from app.utils.crypto import decrypt_weibo_rip, encrypt_weibo_rip  # noqa: E402


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _TerminalFailureDatabase:
    def __init__(
        self,
        task_status="queued",
        stream_key="lottery_tasks",
        *,
        outbox_status="sending",
        outbox_attempts=OUTBOX_MAX_ATTEMPTS,
        probe_status="queued",
        task_mode="dry_run",
        execution_intent_kind=None,
    ):
        self.task_status = task_status
        self.stream_key = stream_key
        self.outbox_status = outbox_status
        self.outbox_attempts = outbox_attempts
        self.probe_status = probe_status
        self.task_mode = task_mode
        self.execution_intent_kind = execution_intent_kind
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM outbox_events" in query:
            if self.stream_key == "adapter_probe_requests":
                probe_message = {
                    "probe_id": "probe-1",
                    "platform": "bilibili",
                    "account_id": "9",
                    "lottery_id": "42",
                    "target_url": "https://www.bilibili.com/opus/123",
                    "canonical_url": "canonical://bilibili/opus/123",
                    "execution_path_id": "bilibili_api_v1",
                    "target_hash": "t" * 64,
                    "rule_snapshot_id": "7",
                    "rule_hash": "r" * 64,
                    "action_plan_hash": "a" * 64,
                    "config_hash": "c" * 64,
                    "execution_revision": "2",
                    "account_lease_id": "lease-probe",
                    "account_lease_generation": "3",
                }
                return {
                    "id": 1,
                    "stream_key": self.stream_key,
                    "dedup_key": "adapter-probe:probe-1",
                    "payload": json.dumps(probe_message),
                    "status": self.outbox_status,
                    "attempts": self.outbox_attempts,
                }
            return {
                "id": 1,
                "stream_key": self.stream_key,
                "dedup_key": "task-1",
                "payload": json.dumps(
                    {
                        "task_id": "task-1",
                        "platform": "bilibili",
                        "execution_intent_kind": (
                            self.execution_intent_kind or ""
                        ),
                    }
                ),
                "status": self.outbox_status,
                "attempts": self.outbox_attempts,
            }
        if "FROM task_runs" in query:
            return {
                "task_id": "task-1",
                "account_id": 9,
                "lottery_id": 42,
                "status": self.task_status,
                "task_mode": self.task_mode,
                "execution_intent_kind": (
                    self.execution_intent_kind
                ),
                "account_lease_id": "lease-task",
                "account_lease_generation": 4,
            }
        if "FROM adapter_calibrations" in query:
            return {
                "account_id": 9,
                "platform": "bilibili",
                "lottery_id": 42,
                "target_url": "https://www.bilibili.com/opus/123",
                "status": self.probe_status,
                "execution_path_id": "bilibili_api_v1",
                "target_hash": "t" * 64,
                "rule_snapshot_id": 7,
                "rule_hash": "r" * 64,
                "action_plan_hash": "a" * 64,
                "config_hash": "c" * 64,
                "account_lease_id": "lease-probe",
                "account_lease_generation": 3,
                "canonical_url": "canonical://bilibili/opus/123",
                "execution_revision": 2,
            }
        if "FROM accounts" in query:
            return {"id": 9}
        if "FROM lotteries" in query:
            return {"id": 42, "status": "claimed", "execution_lock": "task-1"}
        raise AssertionError(query)

    async def execute(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        return 1


class BuildLotteryTaskMessageTests(unittest.TestCase):
    def _msg(self, **overrides):
        params = dict(
            task_id="t-1",
            account_id=7,
            lottery_id=42,
            platform="bilibili",
            raw_url="https://www.bilibili.com/x",
            canonical_url="https://www.bilibili.com/x?clean",
            task_mode="dry_run",
            dry_run=True,
            platform_selectors={"follow": ".btn"},
            action_plan={"required_actions": ["followed"]},
        )
        params.update(overrides)
        return build_lottery_task_message(**params)

    def test_field_set_is_exactly_the_contract(self):
        msg = self._msg()
        self.assertEqual(set(msg.keys()), set(LOTTERY_TASK_FIELDS))

    def test_all_values_are_strings(self):
        msg = self._msg()
        for key, value in msg.items():
            self.assertIsInstance(value, str, f"{key} must be a str for Redis xadd")

    def test_ids_are_stringified(self):
        msg = self._msg(account_id=7, lottery_id=42, execution_revision=9)
        self.assertEqual(msg["account_id"], "7")
        self.assertEqual(msg["lottery_id"], "42")
        self.assertEqual(msg["execution_revision"], "9")

    def test_dry_run_flag_matches_mode(self):
        self.assertEqual(self._msg(task_mode="dry_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="shadow_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["dry_run"], "0")

    def test_mode_is_preserved(self):
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["mode"], "real_run")

    def test_selectors_and_plan_are_json_encoded(self):
        msg = self._msg(platform_selectors={"a": 1}, action_plan={"b": 2})
        self.assertEqual(json.loads(msg["selector_config"]), {"a": 1})
        self.assertEqual(json.loads(msg["action_plan"]), {"b": 2})

    def test_execution_intent_subset_fields_are_preserved(self):
        msg = self._msg(
            execution_intent_id="intent-1",
            execution_intent_hash="a" * 64,
            execution_intent_kind="repair",
            execution_intent_binding_hash="b" * 64,
            requested_actions=["commented", "reposted"],
            requested_actions_hash="c" * 64,
            requested_action_plan_hash="d" * 64,
            execution_evidence_kind="oauth_account_calibration",
            oauth_calibration_id="evidence-1",
        )

        self.assertEqual(msg["execution_intent_id"], "intent-1")
        self.assertEqual(msg["execution_intent_kind"], "repair")
        self.assertEqual(
            json.loads(msg["requested_actions"]),
            ["commented", "reposted"],
        )
        self.assertEqual(msg["requested_actions_hash"], "c" * 64)
        self.assertEqual(msg["requested_action_plan_hash"], "d" * 64)
        self.assertEqual(
            msg["execution_evidence_kind"],
            "oauth_account_calibration",
        )
        self.assertEqual(msg["exact_execution_evidence_id"], "")
        self.assertEqual(msg["oauth_calibration_id"], "evidence-1")

    def test_non_dict_selectors_and_plan_default_to_empty(self):
        msg = self._msg(platform_selectors=None, action_plan="not-a-dict")
        self.assertEqual(msg["selector_config"], "{}")
        self.assertEqual(msg["action_plan"], "{}")

    def test_missing_execution_intent_uses_non_authorizing_empty_values(self):
        msg = self._msg()

        self.assertEqual(msg["execution_intent_id"], "")
        self.assertEqual(msg["execution_intent_hash"], "")
        self.assertEqual(msg["execution_intent_kind"], "")
        self.assertEqual(msg["execution_intent_binding_hash"], "")
        self.assertEqual(msg["requested_actions"], "[]")
        self.assertEqual(msg["requested_actions_hash"], "")
        self.assertEqual(msg["requested_action_plan_hash"], "")
        self.assertEqual(msg["execution_evidence_kind"], "")
        self.assertEqual(msg["exact_execution_evidence_id"], "")
        self.assertEqual(msg["oauth_calibration_id"], "")

    def test_none_urls_become_empty_strings(self):
        msg = self._msg(raw_url=None, canonical_url=None)
        self.assertEqual(msg["raw_url"], "")
        self.assertEqual(msg["canonical_url"], "")

    def test_weibo_rip_is_encrypted_before_durable_serialization(self):
        raw_rip = "8.8.8.8"
        encrypted = encrypt_weibo_rip(raw_rip)
        msg = self._msg(
            platform="weibo",
            weibo_rip_encrypted=encrypted,
        )
        serialized = json.dumps(msg)

        self.assertEqual(decrypt_weibo_rip(msg["weibo_rip_encrypted"]), raw_rip)
        self.assertNotIn(raw_rip, serialized)
        self.assertNotIn('"weibo_rip":', serialized)

    def test_plaintext_weibo_rip_is_rejected_from_durable_handoff(self):
        with self.assertRaisesRegex(ValueError, "plaintext_weibo_rip_forbidden"):
            _reject_plaintext_weibo_rip(
                {"task_id": "t-1", "weibo_rip": "8.8.8.8"},
                "lottery_tasks",
            )

    def test_plaintext_weibo_rip_is_rejected_from_platform_stream(self):
        with self.assertRaisesRegex(
            ValueError,
            "plaintext_weibo_rip_forbidden",
        ):
            _reject_plaintext_weibo_rip(
                {"task_id": "t-1", "weibo_rip": "8.8.8.8"},
                "lottery_tasks:weibo",
            )

    def test_platform_stream_rejects_cross_platform_payload(self):
        with self.assertRaisesRegex(
            ValueError,
            "task_stream_platform_mismatch",
        ):
            _validate_task_stream_binding(
                {"task_id": "t-1", "platform": "weibo"},
                "lottery_tasks:bilibili",
            )

    def test_platform_stream_accepts_exact_platform_payload(self):
        _validate_task_stream_binding(
            {"task_id": "t-1", "platform": "xiaohongshu"},
            "lottery_tasks:xiaohongshu",
        )

    def test_standard_stream_rejects_repair_intent(self):
        with self.assertRaisesRegex(
            ValueError,
            "standard_task_stream_repair_forbidden",
        ):
            _validate_task_stream_binding(
                {
                    "task_id": "t-1",
                    "platform": "bilibili",
                    "mode": "real_run",
                    "execution_intent_kind": "repair",
                },
                "lottery_tasks:bilibili",
            )

    def test_repair_stream_accepts_only_exact_real_repair_envelope(self):
        stream_key = "lottery_repair_tasks:v1:weibo"
        _validate_task_stream_binding(
            {
                "task_id": "t-1",
                "platform": "weibo",
                "mode": "real_run",
                "execution_intent_kind": "repair",
            },
            stream_key,
        )
        for invalid in (
            {
                "platform": "weibo",
                "mode": "shadow_run",
                "execution_intent_kind": "repair",
            },
            {
                "platform": "weibo",
                "mode": "real_run",
                "execution_intent_kind": "full",
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "repair_task_stream_contract_mismatch",
            ):
                _validate_task_stream_binding(invalid, stream_key)


class RetryPredicateTests(unittest.TestCase):
    def test_should_retry_below_cap(self):
        self.assertTrue(should_retry(0))
        self.assertTrue(should_retry(OUTBOX_MAX_ATTEMPTS - 1))

    def test_should_not_retry_at_or_above_cap(self):
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS))
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS + 3))

    def test_terminal_status_keeps_pending_until_cap(self):
        self.assertEqual(terminal_status(1), "pending")
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS - 1), "pending")

    def test_terminal_status_failed_at_cap(self):
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS), "failed")


class OutboxLaneIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_failure_does_not_block_db_orphan_reconciliation(self):
        from app.services import outbox

        orphan_reconciled = asyncio.Event()
        login_reconciliation_attempted = asyncio.Event()
        never_sleep = asyncio.Event()

        async def fake_login_reconcile():
            login_reconciliation_attempted.set()
            raise RuntimeError("redis unavailable")

        async def fake_orphan_reconcile():
            orphan_reconciled.set()
            return 0

        async def fake_sleep(_seconds):
            await never_sleep.wait()

        with patch.object(
            outbox,
            "reconcile_redis_task_stream_epoch",
            new=AsyncMock(return_value=0),
        ), patch.object(
            outbox,
            "reconcile_redis_adapter_probe_stream_epochs",
            new=AsyncMock(return_value=0),
        ), patch.object(
            outbox,
            "reconcile_redis_account_calibration_stream_epochs",
            new=AsyncMock(return_value=0),
        ), patch.object(
            outbox,
            "reconcile_redis_login_request_stream_epoch",
            side_effect=fake_login_reconcile,
        ), patch.object(
            outbox,
            "reconcile_orphaned_locks",
            side_effect=fake_orphan_reconcile,
        ), patch.object(
            outbox,
            "_outbox_lane_initial_delay",
            return_value=0,
        ), patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ):
            loop = asyncio.create_task(
                _outbox_reconciliation_loop(
                    platforms=(),
                    include_shared=True,
                    fail_closed=True,
                )
            )
            try:
                await asyncio.wait_for(
                    login_reconciliation_attempted.wait(),
                    timeout=1,
                )
                await asyncio.wait_for(orphan_reconciled.wait(), timeout=1)
                self.assertFalse(loop.done())
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_isolated_platform_reconciliation_failure_is_fatal(self):
        from app.services import outbox

        never_sleep = asyncio.Event()

        async def fake_task_reconcile(**_kwargs):
            raise RuntimeError("bilibili continuity unavailable")

        async def fake_sleep(_seconds):
            await never_sleep.wait()

        with patch.object(
            outbox,
            "reconcile_redis_task_stream_epoch",
            side_effect=fake_task_reconcile,
        ), patch.object(
            outbox,
            "reconcile_redis_adapter_probe_stream_epochs",
            new=AsyncMock(return_value=0),
        ), patch.object(
            outbox,
            "reconcile_redis_account_calibration_stream_epochs",
            new=AsyncMock(return_value=0),
        ), patch.object(
            outbox,
            "_outbox_lane_initial_delay",
            return_value=0,
        ), patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ), self.assertRaisesRegex(
            RuntimeError,
            "bilibili continuity unavailable",
        ):
            await asyncio.wait_for(
                _outbox_reconciliation_loop(
                    platforms=("bilibili",),
                    include_shared=False,
                    fail_closed=True,
                ),
                timeout=1,
            )

    async def test_blocked_platform_relay_does_not_block_sibling_lane(self):
        from app.services import outbox

        bilibili_started = asyncio.Event()
        release_bilibili = asyncio.Event()
        weibo_completed = asyncio.Event()
        never_sleep = asyncio.Event()

        async def fake_flush(*, stream_key=None, **_kwargs):
            if stream_key == "lottery_tasks:bilibili":
                bilibili_started.set()
                await release_bilibili.wait()
            if stream_key == "lottery_tasks:weibo":
                weibo_completed.set()
            return {"scanned": 0, "sent": 0}

        async def fake_sleep(_seconds):
            await never_sleep.wait()

        with patch.object(
            outbox,
            "flush_pending_outbox",
            side_effect=fake_flush,
        ), patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ):
            bilibili_loop = asyncio.create_task(
                _outbox_delivery_loop(
                    lane="bilibili",
                    stream_key="lottery_tasks:bilibili",
                    initial_delay_seconds=0,
                )
            )
            weibo_loop = asyncio.create_task(
                _outbox_delivery_loop(
                    lane="weibo",
                    stream_key="lottery_tasks:weibo",
                    initial_delay_seconds=0,
                )
            )
            try:
                await asyncio.wait_for(bilibili_started.wait(), timeout=1)
                await asyncio.wait_for(weibo_completed.wait(), timeout=1)
                self.assertFalse(bilibili_loop.done())
            finally:
                bilibili_loop.cancel()
                weibo_loop.cancel()
                await asyncio.gather(
                    bilibili_loop,
                    weibo_loop,
                    return_exceptions=True,
                )

    def test_lane_initial_delay_is_stable_bounded_and_lane_specific(self):
        identity = "test-replica-a"
        bilibili = _outbox_lane_initial_delay(
            "lottery_tasks:bilibili",
            instance_identity=identity,
        )
        weibo = _outbox_lane_initial_delay(
            "lottery_tasks:weibo",
            instance_identity=identity,
        )

        self.assertEqual(
            bilibili,
            _outbox_lane_initial_delay(
                "lottery_tasks:bilibili",
                instance_identity=identity,
            ),
        )
        self.assertGreaterEqual(bilibili, 0)
        self.assertLess(bilibili, 5)
        self.assertGreaterEqual(weibo, 0)
        self.assertLess(weibo, 5)
        self.assertNotEqual(bilibili, weibo)

    def test_lane_phase_and_period_include_instance_identity(self):
        from app.services import outbox

        lane = "lottery_tasks:bilibili"
        replica_a_phase = _outbox_lane_initial_delay(
            lane,
            instance_identity="replica-a",
        )
        replica_b_phase = _outbox_lane_initial_delay(
            lane,
            instance_identity="replica-b",
        )
        self.assertNotEqual(replica_a_phase, replica_b_phase)
        self.assertNotEqual(
            tuple(
                outbox._outbox_reconciliation_period_seconds(
                    lane,
                    cycle,
                    instance_identity="replica-a",
                )
                for cycle in range(8)
            ),
            tuple(
                outbox._outbox_reconciliation_period_seconds(
                    lane,
                    cycle,
                    instance_identity="replica-b",
                )
                for cycle in range(8)
            ),
        )

    def test_instance_identity_prefers_explicit_and_separates_processes(self):
        from app.services import outbox

        environment = {
            "DPMS_OUTBOX_INSTANCE_ID": "outbox-replica",
            "DPMS_CORE_RUNNER_INSTANCE_ID": "core-runner-replica",
        }
        self.assertEqual(
            outbox._resolve_outbox_instance_identity(
                environment=environment,
                hostname="host-a",
                process_id=10,
            ),
            "outbox:outbox-replica",
        )
        self.assertEqual(
            outbox._resolve_outbox_instance_identity(
                environment={
                    "DPMS_CORE_RUNNER_INSTANCE_ID": "core-runner-replica"
                },
                hostname="host-a",
                process_id=10,
            ),
            "core-runner:core-runner-replica",
        )
        self.assertNotEqual(
            outbox._resolve_outbox_instance_identity(
                environment={},
                hostname="host-a",
                process_id=10,
            ),
            outbox._resolve_outbox_instance_identity(
                environment={},
                hostname="host-a",
                process_id=11,
            ),
        )

    def test_instance_identity_cache_refreshes_after_process_fork(self):
        from app.services import outbox

        with patch.object(
            outbox,
            "_OUTBOX_INSTANCE_IDENTITY_CACHE",
            None,
        ), patch.object(
            outbox.os,
            "getpid",
            side_effect=(10, 10, 11),
        ), patch.object(
            outbox,
            "_resolve_outbox_instance_identity",
            side_effect=lambda *, process_id: f"process:{process_id}",
        ) as resolve_identity:
            first = outbox._current_outbox_instance_identity()
            cached = outbox._current_outbox_instance_identity()
            forked = outbox._current_outbox_instance_identity()

        self.assertEqual(first, "process:10")
        self.assertEqual(cached, first)
        self.assertEqual(forked, "process:11")
        self.assertEqual(resolve_identity.call_count, 2)

    def test_reconciliation_phase_and_period_are_stable_and_dispersed(self):
        from app.services import outbox

        lanes = (
            "reconciliation:bilibili:task-stream",
            "reconciliation:weibo:task-stream",
            "reconciliation:xiaohongshu:adapter-probe",
            "reconciliation:douyin:account-calibration",
        )
        phases = tuple(
            _outbox_lane_initial_delay(
                lane,
                instance_identity="test-replica-a",
            )
            for lane in lanes
        )
        self.assertEqual(
            phases,
            tuple(
                _outbox_lane_initial_delay(
                    lane,
                    instance_identity="test-replica-a",
                )
                for lane in lanes
            ),
        )
        self.assertTrue(all(0 <= phase < 5 for phase in phases))
        self.assertEqual(len(set(phases)), len(phases))

        bilibili_periods = tuple(
            outbox._outbox_reconciliation_period_seconds(
                lanes[0],
                cycle,
                instance_identity="test-replica-a",
            )
            for cycle in range(16)
        )
        weibo_periods = tuple(
            outbox._outbox_reconciliation_period_seconds(
                lanes[1],
                cycle,
                instance_identity="test-replica-a",
            )
            for cycle in range(16)
        )
        self.assertEqual(
            bilibili_periods,
            tuple(
                outbox._outbox_reconciliation_period_seconds(
                    lanes[0],
                    cycle,
                    instance_identity="test-replica-a",
                )
                for cycle in range(16)
            ),
        )
        self.assertTrue(
            all(4.5 <= period <= 5.5 for period in bilibili_periods)
        )
        self.assertNotEqual(bilibili_periods, weibo_periods)

    def test_reconciliation_specs_keep_platform_ownership_exact(self):
        from app.services import outbox

        isolated = outbox._outbox_reconciliation_lane_specs(
            platforms=("weibo",),
            include_shared=False,
        )
        self.assertEqual(
            {spec["family"] for spec in isolated},
            {
                "task-stream",
                "adapter-probe",
                "account-calibration",
            },
        )
        self.assertTrue(
            all(spec["platforms"] == ("weibo",) for spec in isolated)
        )
        self.assertTrue(
            all(not spec["include_shared"] for spec in isolated)
        )

        shared = outbox._outbox_reconciliation_lane_specs(
            platforms=(),
            include_shared=True,
        )
        self.assertTrue(shared)
        self.assertTrue(
            all(spec["platforms"] == () for spec in shared)
        )
        self.assertTrue(
            all(spec["include_shared"] for spec in shared)
        )

    async def test_reconciliation_lane_uses_initial_phase_and_is_cancellable(self):
        from app.services import outbox

        reconciled = asyncio.Event()
        period_sleep_started = asyncio.Event()
        hold_period_sleep = asyncio.Event()
        observed_sleeps = []

        async def fake_reconcile(**_kwargs):
            reconciled.set()

        async def fake_sleep(seconds):
            observed_sleeps.append(seconds)
            if len(observed_sleeps) == 1:
                return
            period_sleep_started.set()
            await hold_period_sleep.wait()

        lane = "bilibili:task-stream"
        with patch.object(
            outbox,
            "_reconcile_outbox_lane_once",
            side_effect=fake_reconcile,
        ), patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ):
            loop = asyncio.create_task(
                outbox._outbox_reconciliation_lane_loop(
                    lane=lane,
                    family="task-stream",
                    platforms=("bilibili",),
                    include_shared=False,
                    require_all_owned_lanes=True,
                    initial_delay_seconds=0.321,
                )
            )
            try:
                await asyncio.wait_for(reconciled.wait(), timeout=1)
                await asyncio.wait_for(
                    period_sleep_started.wait(),
                    timeout=1,
                )
                self.assertEqual(observed_sleeps[0], 0.321)
                self.assertEqual(
                    observed_sleeps[1],
                    outbox._outbox_reconciliation_period_seconds(
                        lane,
                        0,
                    ),
                )
            finally:
                loop.cancel()
                await asyncio.wait_for(
                    asyncio.gather(loop, return_exceptions=True),
                    timeout=1,
                )

    async def test_global_reclaim_loop_runs_one_unscoped_pass(self):
        from app.services import outbox

        reclaimed = asyncio.Event()
        never_sleep = asyncio.Event()

        async def fake_reclaim(**kwargs):
            self.assertEqual(kwargs, {})
            reclaimed.set()
            return 3

        async def fake_sleep(seconds):
            self.assertEqual(seconds, outbox.OUTBOX_RECLAIM_POLL_SECONDS)
            await never_sleep.wait()

        with patch.object(
            outbox,
            "reclaim_stale_sending",
            side_effect=fake_reclaim,
        ) as reclaim, patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ):
            loop = asyncio.create_task(_outbox_reclaim_loop())
            try:
                await asyncio.wait_for(reclaimed.wait(), timeout=1)
                reclaim.assert_awaited_once_with()
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_global_reclaim_pass_is_database_bounded(self):
        from app.services import outbox

        with patch.object(
            outbox,
            "execute_affected_rows",
            new=AsyncMock(return_value=17),
        ) as execute:
            reclaimed = await outbox.reclaim_stale_sending(limit=17)

        self.assertEqual(reclaimed, 17)
        query, values = execute.await_args.args
        self.assertIn("ORDER BY id LIMIT :limit", query)
        self.assertEqual(values["limit"], 17)
        self.assertIs(execute.await_args.kwargs["db"], outbox.database)

    async def test_platform_reclaim_pass_is_stream_scoped_and_bounded(self):
        from app.services import outbox

        streams = (
            "lottery_tasks:bilibili",
            "lottery_repair_tasks:v1:bilibili",
        )
        with patch.object(
            outbox,
            "execute_affected_rows",
            new=AsyncMock(return_value=2),
        ) as execute:
            reclaimed = await outbox.reclaim_stale_sending(
                stream_keys=streams,
                limit=17,
            )

        self.assertEqual(reclaimed, 2)
        query, values = execute.await_args.args
        self.assertIn(
            "stream_key IN (:selected_stream_0, :selected_stream_1)",
            query,
        )
        self.assertEqual(values["selected_stream_0"], streams[0])
        self.assertEqual(values["selected_stream_1"], streams[1])
        self.assertEqual(values["limit"], 17)

    def test_reclaim_specs_keep_platform_and_shared_ownership_disjoint(self):
        from app.services import outbox

        all_platform_streams = (
            "lottery_tasks:bilibili",
            "lottery_tasks:weibo",
        )
        platform = outbox._outbox_reclaim_lane_specs(
            platform_stream_keys=(all_platform_streams[0],),
            all_platform_stream_keys=all_platform_streams,
            include_shared=False,
            isolated_ownership=True,
        )
        self.assertEqual(len(platform), 1)
        self.assertEqual(
            platform[0]["stream_keys"],
            (all_platform_streams[0],),
        )
        self.assertTrue(platform[0]["fail_closed"])

        shared = outbox._outbox_reclaim_lane_specs(
            platform_stream_keys=(),
            all_platform_stream_keys=all_platform_streams,
            include_shared=True,
            isolated_ownership=True,
        )
        self.assertEqual(len(shared), 1)
        self.assertEqual(
            shared[0]["exclude_stream_keys"],
            all_platform_streams,
        )
        self.assertFalse(shared[0]["fail_closed"])

        compatibility_monolith = outbox._outbox_reclaim_lane_specs(
            platform_stream_keys=all_platform_streams,
            all_platform_stream_keys=all_platform_streams,
            include_shared=True,
            isolated_ownership=False,
        )
        self.assertEqual(len(compatibility_monolith), 1)
        self.assertEqual(
            compatibility_monolith[0]["exclude_stream_keys"],
            (),
        )

    async def test_platform_reclaim_failure_is_fatal_and_lane_scoped(self):
        from app.services import outbox

        streams = ("lottery_tasks:bilibili",)
        with patch.object(
            outbox,
            "reclaim_stale_sending",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ) as reclaim, self.assertRaisesRegex(
            RuntimeError,
            "database unavailable",
        ):
            await _outbox_reclaim_loop(
                lane="platform",
                stream_keys=streams,
                fail_closed=True,
            )

        reclaim.assert_awaited_once_with(stream_keys=streams)

    async def test_lane_relay_does_not_run_reclaim_maintenance(self):
        from app.services import outbox

        flushed = asyncio.Event()
        never_sleep = asyncio.Event()

        async def fake_flush(**_kwargs):
            flushed.set()
            return {"scanned": 0, "sent": 0}

        async def fake_sleep(_seconds):
            await never_sleep.wait()

        with patch.object(
            outbox,
            "flush_pending_outbox",
            side_effect=fake_flush,
        ), patch.object(
            outbox,
            "reclaim_stale_sending",
            new=AsyncMock(),
        ) as reclaim, patch.object(
            outbox.asyncio,
            "sleep",
            side_effect=fake_sleep,
        ):
            loop = asyncio.create_task(
                _outbox_delivery_loop(
                    lane="bilibili",
                    stream_key="lottery_tasks:bilibili",
                    initial_delay_seconds=0,
                )
            )
            try:
                await asyncio.wait_for(flushed.wait(), timeout=1)
                reclaim.assert_not_awaited()
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_isolated_lane_does_not_fail_on_lost_claim_race(self):
        from app.services import outbox

        flushed = asyncio.Event()
        never_sleep = asyncio.Event()

        async def fake_flush(**kwargs):
            self.assertTrue(kwargs["include_delivery_failures"])
            flushed.set()
            return {
                "scanned": 1,
                "sent": 0,
                "delivery_failures": 0,
            }

        async def fake_sleep(_seconds):
            await never_sleep.wait()

        with (
            patch.object(
                outbox,
                "flush_pending_outbox",
                side_effect=fake_flush,
            ),
            patch.object(
                outbox.asyncio,
                "sleep",
                side_effect=fake_sleep,
            ),
        ):
            loop = asyncio.create_task(
                _outbox_delivery_loop(
                    lane="bilibili",
                    stream_key="lottery_tasks:bilibili",
                    fail_closed=True,
                )
            )
            try:
                await asyncio.wait_for(flushed.wait(), timeout=1)
                self.assertFalse(loop.done())
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_flush_reports_only_claimed_delivery_failures(self):
        from app.services import outbox

        fake_database = AsyncMock()
        fake_database.fetch_all.return_value = [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]
        with (
            patch.object(outbox, "database", fake_database),
            patch.object(
                outbox,
                "_relay_by_id_outcome",
                new=AsyncMock(
                    side_effect=("unclaimed", "failed", "sent")
                ),
            ),
        ):
            result = await flush_pending_outbox(
                stream_key="lottery_tasks:bilibili",
                include_delivery_failures=True,
            )

        self.assertEqual(
            result,
            {
                "scanned": 3,
                "sent": 1,
                "delivery_failures": 1,
            },
        )

    async def test_stream_scoped_flush_cannot_be_starved_by_other_stream(self):
        from app.services import outbox

        fake_database = AsyncMock()
        fake_database.fetch_all.return_value = []
        with patch.object(outbox, "database", fake_database):
            result = await flush_pending_outbox(
                stream_key="lottery_tasks:weibo",
            )

        self.assertEqual(result, {"scanned": 0, "sent": 0})
        query, values = fake_database.fetch_all.await_args.args
        self.assertIn("stream_key = :stream_key", " ".join(query.split()))
        self.assertEqual(values["stream_key"], "lottery_tasks:weibo")

    async def test_shared_lane_excludes_every_task_stream(self):
        from app.services import outbox

        excluded = tuple(
            binding.stream_key
            for binding in task_stream_bindings(include_legacy=True)
        )
        fake_database = AsyncMock()
        fake_database.fetch_all.return_value = []
        with patch.object(outbox, "database", fake_database):
            await flush_pending_outbox(exclude_stream_keys=excluded)

        query, values = fake_database.fetch_all.await_args.args
        self.assertIn("stream_key NOT IN", " ".join(query.split()))
        self.assertEqual(
            {
                values[f"excluded_stream_{index}"]
                for index in range(len(excluded))
            },
            set(excluded),
        )


class TerminalDeliverySettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_lottery_task_is_failed_and_claim_is_released(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase()
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertIn("UPDATE task_runs", statements)
        self.assertIn("UPDATE lotteries SET status = 'pending'", statements)

    async def test_exhausted_platform_stream_task_is_also_settled(self):
        from app.services import outbox

        stream_key = "lottery_tasks:bilibili"
        fake = _TerminalFailureDatabase(stream_key=stream_key)
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": stream_key,
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("UPDATE task_runs", statements)
        self.assertIn("UPDATE lotteries SET status = 'pending'", statements)

    async def test_exhausted_repair_stream_task_is_also_settled(self):
        from app.services import outbox

        stream_key = "lottery_repair_tasks:v1:bilibili"
        fake = _TerminalFailureDatabase(
            stream_key=stream_key,
            task_mode="real_run",
            execution_intent_kind="repair",
        )
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": stream_key,
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("UPDATE task_runs", statements)
        self.assertIn("UPDATE lotteries SET status = 'pending'", statements)
        lease_values = next(
            values
            for query, values in fake.executions
            if "UPDATE account_operation_leases" in query
        )
        self.assertEqual(
            lease_values["operation_kind"],
            "repair_run",
        )

    async def test_running_task_is_not_reversed_after_ambiguous_delivery(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(task_status="running")
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "dedup_key": "task-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis response lost"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertNotIn("UPDATE task_runs", statements)
        self.assertNotIn("UPDATE lotteries", statements)

    async def test_exhausted_probe_delivery_marks_queued_calibration_failed(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(stream_key="adapter_probe_requests")
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "adapter_probe_requests",
                    "dedup_key": "adapter-probe:probe-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis unavailable"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertIn("SET status = 'failed'", statements)
        self.assertIn("UPDATE adapter_calibrations", statements)
        self.assertIn("operation_kind = 'adapter_probe'", statements)
        self.assertNotIn("UPDATE task_runs", statements)

    async def test_ambiguous_probe_delivery_does_not_release_running_probe(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(
            stream_key="adapter_probe_requests",
            probe_status="running",
        )
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {
                    "id": 1,
                    "stream_key": "adapter_probe_requests",
                    "dedup_key": "adapter-probe:probe-1",
                },
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("redis response lost"),
            )
        finally:
            outbox.database = original

        statements = "\n".join(query for query, _ in fake.executions)
        self.assertNotIn("UPDATE adapter_calibrations", statements)
        self.assertNotIn("UPDATE account_operation_leases", statements)

    async def test_stale_terminal_failure_cannot_overwrite_newer_claim(self):
        from app.services import outbox

        fake = _TerminalFailureDatabase(outbox_attempts=OUTBOX_MAX_ATTEMPTS + 1)
        original = outbox.database
        outbox.database = fake
        try:
            await _settle_terminal_delivery_failure(
                {"id": 1, "stream_key": "lottery_tasks", "dedup_key": "task-1"},
                OUTBOX_MAX_ATTEMPTS,
                RuntimeError("late failure"),
            )
        finally:
            outbox.database = original

        self.assertEqual(fake.executions, [])


class _StaleReceiptDatabase:
    def __init__(self, affected=0):
        self.affected = affected
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        return 0

    async def fetch_one(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        if "ROW_COUNT()" in str(query):
            return {"affected": self.affected}
        raise AssertionError(query)


class _SuccessfulRedis:
    async def info(self, **_kwargs):
        return {"run_id": "a" * 40}

    async def eval(self, _script, key_count, *_args, **_kwargs):
        if key_count == 1:
            return "b" * 64
        return ["b" * 64, "c" * 64, "0"]

    async def xadd(self, *_args, **_kwargs):
        return "1-0"


class DeliveryFencingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_success_receipt_does_not_report_newer_claim_as_sent(self):
        from app.services import outbox

        fake_database = _StaleReceiptDatabase()
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = _SuccessfulRedis()
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps({"task_id": "task-1"}),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertFalse(delivered)
        query, values = fake_database.executions[0]
        self.assertIn("status = 'sending'", query)
        self.assertIn("attempts = :attempts", query)
        self.assertEqual(values["attempts"], 2)

    async def test_success_receipt_uses_row_count_even_when_execute_returns_zero(self):
        from app.services import outbox

        fake_database = _StaleReceiptDatabase(affected=1)
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = _SuccessfulRedis()
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps({"task_id": "task-1"}),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertTrue(delivered)
        self.assertTrue(any("ROW_COUNT()" in query for query, _ in fake_database.executions))

    async def test_epoch_change_during_delivery_requeues_the_ambiguous_receipt(self):
        from app.services import outbox

        class _RestartingRedis:
            def __init__(self):
                self.run_ids = iter(("a" * 40, "b" * 40))

            async def info(self, **_kwargs):
                return {"run_id": next(self.run_ids)}

            async def eval(self, _script, key_count, *_args, **_kwargs):
                if key_count == 1:
                    return "c" * 64
                return ["c" * 64, "d" * 64, "0"]

            async def xadd(self, *_args, **_kwargs):
                return "1-0"

        fake_database = _StaleReceiptDatabase(affected=1)
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = _RestartingRedis()
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 1,
                    "stream_key": "lottery_tasks:bilibili",
                    "payload": json.dumps(
                        {"task_id": "task-1", "platform": "bilibili"}
                    ),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertFalse(delivered)
        statements = "\n".join(
            query for query, _ in fake_database.executions
        )
        self.assertIn("redis_delivery_epoch = :delivery_epoch", statements)
        self.assertIn("SET status = 'pending'", statements)
        self.assertIn(
            "redis_epoch_changed_during_delivery",
            {
                values.get("reason")
                for _, values in fake_database.executions
            },
        )


class _EpochReplayDatabase:
    def __init__(self, *, stored_epoch, rows=None, task_statuses=None):
        self.stored_epoch = stored_epoch
        self.rows = rows or []
        self.task_statuses = task_statuses or {}
        self.executions = []
        self.fetches = []
        self.affected = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        query = str(query)
        values = dict(values or {})
        self.executions.append((query, values))
        compact = " ".join(query.split())
        if compact.startswith("UPDATE task_runs AS tr"):
            task_epochs = {
                values[f"task_stream_{index}"]: values[f"task_epoch_{index}"]
                for index in range(
                    len(
                        [
                            key
                            for key in values
                            if key.startswith("task_stream_")
                        ]
                    )
                )
            }
            self.affected = 0
            for row in self.rows:
                if (
                    row["status"] == "sent"
                    and self.task_statuses.get(row["dedup_key"]) == "queued"
                    and row["stream_key"] in task_epochs
                    and row.get("redis_delivery_epoch")
                    != task_epochs[row["stream_key"]]
                ):
                    row.update(
                        status="pending",
                        attempts=0,
                        sent_at=None,
                        redis_delivery_epoch=None,
                    )
                    self.affected += 1
        elif compact.startswith("UPDATE runtime_settings"):
            self.stored_epoch = values["observed_epoch"]
        return 1

    async def fetch_one(self, query, values=None):
        query = str(query)
        self.fetches.append((query, dict(values or {})))
        if "FROM runtime_settings" in query:
            return {"setting_value": self.stored_epoch}
        if "ROW_COUNT()" in query:
            return {"affected": self.affected}
        raise AssertionError(query)


class _LaneContinuityRedis:
    """Small executable model of the lane-continuity Lua contract."""

    def __init__(self):
        self.run_id = "a" * 40
        self.global_token = None
        self.lane_tokens = {}
        self.lane_states = {}
        self.stream_lengths = {}

    async def info(self, **_kwargs):
        return {"run_id": self.run_id}

    async def eval(self, _script, key_count, *args):
        if key_count == 1:
            _global_key, global_candidate = args
            if self.global_token is None:
                self.global_token = global_candidate
            return self.global_token

        (
            _global_key,
            lane_token_key,
            stream_key,
            lane_state_key,
            global_candidate,
            lane_candidate,
            prepare_delivery,
        ) = args
        if self.global_token is None:
            self.global_token = global_candidate
        lane_token_preexisting = lane_token_key in self.lane_tokens
        if not lane_token_preexisting:
            self.lane_tokens[lane_token_key] = lane_candidate
        lane_token = self.lane_tokens[lane_token_key]
        lane_length = int(self.stream_lengths.get(stream_key, 0))
        lane_state = self.lane_states.get(lane_state_key)
        if lane_length == 0:
            if lane_state == "nonempty" or (
                lane_state is None and lane_token_preexisting
            ):
                lane_token = lane_candidate
                self.lane_tokens[lane_token_key] = lane_token
            self.lane_states[lane_state_key] = (
                "nonempty" if prepare_delivery == "1" else "empty"
            )
        else:
            self.lane_states[lane_state_key] = "nonempty"
        return [self.global_token, lane_token, str(lane_length)]


class RedisEpochReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_lane_epoch_rotates_once_when_only_that_stream_disappears(self):
        from app.services import outbox

        standard = "lottery_tasks:bilibili"
        repair = "lottery_repair_tasks:v1:bilibili"
        other_platform = "lottery_tasks:weibo"
        fake_redis = _LaneContinuityRedis()
        fake_redis.stream_lengths.update(
            {
                standard: 3,
                repair: 2,
                other_platform: 4,
            }
        )
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            standard_before = await read_redis_task_stream_epoch(standard)
            repair_before = await read_redis_task_stream_epoch(repair)
            other_before = await read_redis_task_stream_epoch(other_platform)

            fake_redis.stream_lengths[standard] = 0
            standard_after = await read_redis_task_stream_epoch(standard)
            standard_after_again = await read_redis_task_stream_epoch(standard)
            repair_after = await read_redis_task_stream_epoch(repair)
            other_after = await read_redis_task_stream_epoch(other_platform)
        finally:
            outbox.redis = original_redis

        self.assertNotEqual(standard_before, standard_after)
        self.assertEqual(standard_after, standard_after_again)
        self.assertEqual(repair_before, repair_after)
        self.assertEqual(other_before, other_after)

    async def test_empty_lane_is_stable_but_failed_delivery_arm_rotates_it(self):
        from app.services import outbox

        stream_key = "lottery_tasks:douyin"
        fake_redis = _LaneContinuityRedis()
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            initial = await read_redis_task_stream_epoch(stream_key)
            stable_empty = await read_redis_task_stream_epoch(stream_key)
            armed = await read_redis_task_stream_epoch(
                stream_key,
                prepare_delivery=True,
            )
            detected_missing = await read_redis_task_stream_epoch(stream_key)
            stable_missing = await read_redis_task_stream_epoch(stream_key)
        finally:
            outbox.redis = original_redis

        self.assertEqual(initial, stable_empty)
        self.assertEqual(stable_empty, armed)
        self.assertNotEqual(armed, detected_missing)
        self.assertEqual(detected_missing, stable_missing)

    async def test_lane_epoch_rejects_non_task_stream(self):
        with self.assertRaisesRegex(
            RedisTaskStreamEpochUnavailable,
            "redis_task_stream_lane_unknown",
        ):
            await read_redis_task_stream_epoch("notify_events")

    async def test_reconcile_replays_only_lost_lane_sent_queued_rows(self):
        from app.services import outbox

        lost_stream = "lottery_tasks:bilibili"
        repair_stream = "lottery_repair_tasks:v1:bilibili"
        other_stream = "lottery_tasks:weibo"
        old_lost_epoch = "redis:v3:" + ("1" * 64)
        current_epochs = {
            binding.stream_key: "redis:v3:" + format(index + 2, "064x")
            for index, binding in enumerate(
                task_stream_bindings(include_legacy=True)
            )
        }
        rows = [
            {
                "id": 1,
                "stream_key": lost_stream,
                "dedup_key": "lost-queued",
                "status": "sent",
                "redis_delivery_epoch": old_lost_epoch,
            },
            {
                "id": 2,
                "stream_key": lost_stream,
                "dedup_key": "lost-pre-epoch",
                "status": "sent",
                "redis_delivery_epoch": None,
            },
            {
                "id": 3,
                "stream_key": repair_stream,
                "dedup_key": "repair-current",
                "status": "sent",
                "redis_delivery_epoch": current_epochs[repair_stream],
            },
            {
                "id": 4,
                "stream_key": other_stream,
                "dedup_key": "other-current",
                "status": "sent",
                "redis_delivery_epoch": current_epochs[other_stream],
            },
            {
                "id": 5,
                "stream_key": lost_stream,
                "dedup_key": "lost-running",
                "status": "sent",
                "redis_delivery_epoch": old_lost_epoch,
            },
            {
                "id": 6,
                "stream_key": lost_stream,
                "dedup_key": "lost-terminal",
                "status": "sent",
                "redis_delivery_epoch": old_lost_epoch,
            },
            {
                "id": 7,
                "stream_key": lost_stream,
                "dedup_key": "already-pending",
                "status": "pending",
                "redis_delivery_epoch": old_lost_epoch,
            },
        ]
        fake_database = _EpochReplayDatabase(
            stored_epoch="redis:v2:" + ("a" * 105),
            rows=rows,
            task_statuses={
                "lost-queued": "queued",
                "lost-pre-epoch": "queued",
                "repair-current": "queued",
                "other-current": "queued",
                "lost-running": "running",
                "lost-terminal": "succeeded",
                "already-pending": "queued",
            },
        )

        async def observed_epoch(stream_key=None, **_kwargs):
            if stream_key is None:
                return "redis:v2:" + ("b" * 105)
            return current_epochs[stream_key]

        original_database = outbox.database
        outbox.database = fake_database
        try:
            with patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=observed_epoch,
            ):
                affected = await reconcile_redis_task_stream_epoch()
        finally:
            outbox.database = original_database

        self.assertEqual(affected, 2)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[1]["status"], "pending")
        self.assertEqual(rows[2]["status"], "sent")
        self.assertEqual(rows[3]["status"], "sent")
        self.assertEqual(rows[4]["status"], "sent")
        self.assertEqual(rows[5]["status"], "sent")
        self.assertEqual(rows[6]["status"], "pending")

    async def test_first_deploy_conservatively_replays_pre_epoch_queued_receipt(self):
        from app.services import outbox

        current_epoch = "redis-run-id:v1:" + ("b" * 40)
        rows = [
            {
                "id": 1,
                "stream_key": "lottery_tasks",
                "dedup_key": "legacy-pre-epoch",
                "status": "sent",
                "redis_delivery_epoch": None,
            }
        ]
        fake_database = _EpochReplayDatabase(
            stored_epoch="",
            rows=rows,
            task_statuses={"legacy-pre-epoch": "queued"},
        )
        original_database = outbox.database
        outbox.database = fake_database
        try:
            with patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=AsyncMock(return_value=current_epoch),
            ):
                affected = await reconcile_redis_task_stream_epoch()
        finally:
            outbox.database = original_database

        self.assertEqual(affected, 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(fake_database.stored_epoch, current_epoch)

    async def test_epoch_change_replays_only_sent_queued_known_task_streams(self):
        from app.services import outbox

        old_epoch = "redis-run-id:v1:" + ("a" * 40)
        current_epoch = "redis-run-id:v1:" + ("b" * 40)
        rows = [
            {
                "id": 1,
                "stream_key": "lottery_tasks:bilibili",
                "dedup_key": "queued-old",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 2,
                "stream_key": "lottery_tasks:weibo",
                "dedup_key": "queued-pre-upgrade",
                "status": "sent",
                "redis_delivery_epoch": None,
            },
            {
                "id": 3,
                "stream_key": "lottery_tasks:xiaohongshu",
                "dedup_key": "running",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 4,
                "stream_key": "lottery_tasks:douyin",
                "dedup_key": "terminal",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 5,
                "stream_key": "notify_events",
                "dedup_key": "non-task",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 6,
                "stream_key": "lottery_tasks:future",
                "dedup_key": "unknown-stream",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 7,
                "stream_key": "lottery_tasks:bilibili",
                "dedup_key": "already-pending",
                "status": "pending",
                "redis_delivery_epoch": old_epoch,
            },
            {
                "id": 8,
                "stream_key": "lottery_tasks:bilibili",
                "dedup_key": "queued-current",
                "status": "sent",
                "redis_delivery_epoch": current_epoch,
            },
            {
                "id": 9,
                "stream_key": "lottery_repair_tasks:v1:weibo",
                "dedup_key": "queued-repair-old",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            },
        ]
        task_statuses = {
            "queued-old": "queued",
            "queued-pre-upgrade": "queued",
            "running": "running",
            "terminal": "succeeded",
            "non-task": "queued",
            "unknown-stream": "queued",
            "already-pending": "queued",
            "queued-current": "queued",
            "queued-repair-old": "queued",
        }
        fake_database = _EpochReplayDatabase(
            stored_epoch=old_epoch,
            rows=rows,
            task_statuses=task_statuses,
        )
        original_database = outbox.database
        outbox.database = fake_database
        try:
            with patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=AsyncMock(return_value=current_epoch),
            ):
                affected = await reconcile_redis_task_stream_epoch()
        finally:
            outbox.database = original_database

        self.assertEqual(affected, 3)
        self.assertEqual(
            {row["id"] for row in rows if row["status"] == "pending"},
            {1, 2, 7, 9},
        )
        self.assertEqual(fake_database.stored_epoch, current_epoch)
        replay_sql = next(
            query
            for query, _ in fake_database.executions
            if "UPDATE task_runs AS tr" in query
        )
        compact = " ".join(replay_sql.split())
        self.assertIn("tr.status = 'queued'", compact)
        self.assertIn("o.status = 'sent'", compact)
        self.assertIn("o.stream_key IN", compact)
        self.assertIn(
            "task_runs AS tr FORCE INDEX (idx_task_run_status) "
            "STRAIGHT_JOIN outbox_events AS o "
            "FORCE INDEX (uk_outbox_dedup)",
            compact,
        )
        lock_sql = next(
            query
            for query, _ in fake_database.fetches
            if "FROM runtime_settings" in query
        )
        self.assertIn("FOR UPDATE", " ".join(lock_sql.split()))

    async def test_same_epoch_repairs_mismatches_but_does_not_replay_current_rows(self):
        from app.services import outbox

        current_epoch = "redis-run-id:v1:" + ("b" * 40)
        fake_database = _EpochReplayDatabase(
            stored_epoch=current_epoch,
        )
        original_database = outbox.database
        outbox.database = fake_database
        try:
            with patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=AsyncMock(return_value=current_epoch),
            ):
                affected = await reconcile_redis_task_stream_epoch()
        finally:
            outbox.database = original_database

        self.assertEqual(affected, 0)
        self.assertTrue(
            any(
                "UPDATE task_runs AS tr" in query
                for query, _ in fake_database.executions
            )
        )
        self.assertFalse(
            any(
                "UPDATE runtime_settings" in query
                for query, _ in fake_database.executions
            )
        )

    async def test_same_global_epoch_repairs_a_late_old_epoch_receipt(self):
        from app.services import outbox

        old_epoch = "redis-run-id:v1:" + ("a" * 40)
        current_epoch = "redis-run-id:v1:" + ("b" * 40)
        rows = [
            {
                "id": 1,
                "stream_key": "lottery_tasks:douyin",
                "dedup_key": "late-receipt",
                "status": "sent",
                "redis_delivery_epoch": old_epoch,
            }
        ]
        fake_database = _EpochReplayDatabase(
            stored_epoch=current_epoch,
            rows=rows,
            task_statuses={"late-receipt": "queued"},
        )
        original_database = outbox.database
        outbox.database = fake_database
        try:
            with patch.object(
                outbox,
                "read_redis_task_stream_epoch",
                new=AsyncMock(return_value=current_epoch),
            ):
                affected = await reconcile_redis_task_stream_epoch()
        finally:
            outbox.database = original_database

        self.assertEqual(affected, 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(fake_database.stored_epoch, current_epoch)

    async def test_info_acl_failure_is_explicit_and_has_no_identity_fallback(self):
        from app.services import outbox

        fake_redis = AsyncMock()
        fake_redis.info.side_effect = PermissionError(
            "NOPERM this user has no permissions to run INFO"
        )
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            with self.assertRaisesRegex(
                RedisTaskStreamEpochUnavailable,
                "redis_server_info_unavailable",
            ):
                await read_redis_task_stream_epoch()
        finally:
            outbox.redis = original_redis

        fake_redis.info.assert_awaited_once_with(section="server")
        fake_redis.eval.assert_not_awaited()

    async def test_sentinel_acl_failure_is_explicit_and_fails_closed(self):
        from app.services import outbox

        fake_redis = AsyncMock()
        fake_redis.info.return_value = {"run_id": "a" * 40}
        fake_redis.eval.side_effect = PermissionError("NOPERM EVAL")
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            with self.assertRaisesRegex(
                RedisTaskStreamEpochUnavailable,
                "redis_continuity_sentinel_unavailable",
            ):
                await read_redis_task_stream_epoch()
        finally:
            outbox.redis = original_redis

        fake_redis.eval.assert_awaited_once()

    async def test_same_process_sentinel_loss_changes_epoch(self):
        from app.services import outbox

        class SentinelRedis:
            def __init__(self):
                self.token = None

            async def info(self, **_kwargs):
                return {"run_id": "a" * 40}

            async def eval(self, _script, _key_count, _key, candidate):
                if self.token is None:
                    self.token = candidate
                return self.token

        fake_redis = SentinelRedis()
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            first = await read_redis_task_stream_epoch()
            # Models same-process FLUSHDB or exact sentinel deletion.
            fake_redis.token = None
            second = await read_redis_task_stream_epoch()
        finally:
            outbox.redis = original_redis

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("redis:v2:" + ("a" * 40) + ":"))
        self.assertTrue(second.startswith("redis:v2:" + ("a" * 40) + ":"))

    async def test_concurrent_core_instances_converge_on_one_sentinel(self):
        from app.services import outbox

        class AtomicSentinelRedis:
            def __init__(self):
                self.token = None
                self.lock = asyncio.Lock()

            async def info(self, **_kwargs):
                return {"run_id": "a" * 40}

            async def eval(self, _script, _key_count, _key, candidate):
                async with self.lock:
                    if self.token is None:
                        await asyncio.sleep(0)
                        self.token = candidate
                    return self.token

        fake_redis = AtomicSentinelRedis()
        original_redis = outbox.redis
        outbox.redis = fake_redis
        try:
            epochs = await asyncio.gather(
                *(read_redis_task_stream_epoch() for _ in range(16))
            )
        finally:
            outbox.redis = original_redis

        self.assertEqual(len(set(epochs)), 1)

    async def test_same_process_flush_during_delivery_requeues_receipt(self):
        from app.services import outbox

        class FlushingRedis:
            def __init__(self):
                self.token = None
                self.lane_token = None

            async def info(self, **_kwargs):
                return {"run_id": "a" * 40}

            async def eval(self, _script, key_count, *args):
                if key_count == 1:
                    candidate = args[1]
                    if self.token is None:
                        self.token = candidate
                    return self.token
                global_candidate = args[4]
                lane_candidate = args[5]
                if self.token is None:
                    self.token = global_candidate
                if self.lane_token is None:
                    self.lane_token = lane_candidate
                return [self.token, self.lane_token, "0"]

            async def xadd(self, *_args, **_kwargs):
                # Redis acknowledges the append, then the same process loses
                # both stream data and its continuity sentinel.
                self.token = None
                return "1-0"

        fake_database = _StaleReceiptDatabase(affected=1)
        fake_redis = FlushingRedis()
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = fake_redis
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 11,
                    "stream_key": "lottery_tasks:bilibili",
                    "payload": json.dumps(
                        {"task_id": "task-11", "platform": "bilibili"}
                    ),
                    "attempts": 2,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertFalse(delivered)
        reset_query, reset_values = next(
            (query, values)
            for query, values in fake_database.executions
            if "SET status = 'pending'" in query
        )
        compact = " ".join(reset_query.split())
        self.assertIn("status = 'sent'", compact)
        self.assertIn(
            "redis_delivery_epoch = :delivery_epoch",
            compact,
        )
        self.assertEqual(
            reset_values["reason"],
            "redis_epoch_changed_during_delivery",
        )

    async def test_info_acl_failure_keeps_task_pending_without_spending_retry(self):
        from app.services import outbox

        fake_database = _StaleReceiptDatabase(affected=1)
        fake_redis = AsyncMock()
        fake_redis.info.side_effect = PermissionError("NOPERM INFO")
        original_database = outbox.database
        original_redis = outbox.redis
        outbox.database = fake_database
        outbox.redis = fake_redis
        try:
            delivered = await _deliver_claimed(
                {
                    "id": 9,
                    "stream_key": "lottery_tasks:weibo",
                    "payload": json.dumps(
                        {"task_id": "task-9", "platform": "weibo"}
                    ),
                    "attempts": 4,
                }
            )
        finally:
            outbox.database = original_database
            outbox.redis = original_redis

        self.assertFalse(delivered)
        fake_redis.xadd.assert_not_awaited()
        query, values = fake_database.executions[-1]
        compact = " ".join(query.split())
        self.assertIn("status = 'pending'", compact)
        self.assertIn("attempts = GREATEST(attempts - 1, 0)", compact)
        self.assertEqual(values["attempts"], 4)


class _AffectedRowsDatabase:
    def __init__(self):
        self.query = None
        self.values = None

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.query = str(query)
        self.values = dict(values or {})
        return 3

    async def fetch_one(self, query, values=None):
        self.row_count_query = str(query)
        return {"affected": 3}


class OrphanLockSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_partial_commit_is_not_unlocked_for_replay(self):
        from app.services import outbox

        fake = _AffectedRowsDatabase()
        original = outbox.database
        outbox.database = fake
        try:
            affected = await reconcile_orphaned_locks(grace_minutes=17)
        finally:
            outbox.database = original

        self.assertEqual(affected, 3)
        self.assertEqual(fake.values["grace"], 17)
        compact = " ".join(fake.query.split())
        self.assertIn("tr.reconciliation_required = 0", compact)
        self.assertIn("eai.status IN ('started', 'unknown', 'succeeded')", compact)
        self.assertIn("eai.effect_certainty IN ('unknown', 'confirmed_effect')", compact)
        self.assertIn("eai.effect_certainty <> 'confirmed_no_effect'", compact)
        self.assertIn("THEN 'participated'", compact)


if __name__ == "__main__":
    unittest.main()
