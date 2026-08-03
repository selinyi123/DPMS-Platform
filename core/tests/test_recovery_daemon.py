import base64
import asyncio
import json
import os
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.recovery_daemon import (  # noqa: E402
    IDLE_THRESHOLD_MS,
    RealRunRecoveryBlocked,
    TaskRecoveryBlocked,
    LEGACY_SOURCE_MESSAGE_ID_FIELD,
    LEGACY_SOURCE_STREAM_FIELD,
    _ack_converged_stream_message,
    _ensure_task_stream_group,
    _fanout_legacy_claimed_message,
    _legacy_fanout_loop,
    _mark_recovery_blocked,
    _mark_recovery_exhausted,
    _prepare_task_for_recovery,
    _reenqueue_fanned_out_task,
    _rebuild_task_payload,
    _recovery_stream_authority,
    _validate_recovery_execution_intent_authority,
    pending_idle_ms,
)
from app.services.execution_intents import (  # noqa: E402
    TaskExecutionIntentBinding,
)
from app.action_plan import (  # noqa: E402
    WEIBO_OAUTH_EXECUTION_PATH,
    compute_config_hash,
    weibo_runtime_capability_requirements,
)
from app.task_streams import (  # noqa: E402
    LEGACY_TASK_STREAM_BINDING,
    repair_task_stream_binding_for_platform,
    task_stream_binding_for_platform,
)


CONFIG_HASH_REVISION_7 = "7ea85ddf973664b5825f8a065c5866706176969209e4800060f2f390d5e67fc0"


def _task_row(*, mode="dry_run", canonical_url="https://example.test/lottery", action_plan="{}"):
    return {
        "id": 7,
        "account_id": 11,
        "lottery_id": 7,
        "task_mode": mode,
        "dry_run": 0 if mode == "real_run" else 1,
        "platform": "bilibili",
        "raw_url": "https://example.test/lottery",
        "canonical_url": canonical_url,
        "action_plan": action_plan,
        "task_rule_snapshot_id": 91,
        "task_rule_hash": "r" * 64,
        "task_action_plan_hash": "p" * 64,
        "execution_evidence_id": "evidence-1" if mode == "real_run" else None,
        "execution_path_id": "bilibili_api_v2",
        "target_hash": "t" * 64,
        "config_hash": CONFIG_HASH_REVISION_7,
        "account_lease_id": "lease-1",
        "account_lease_generation": 3,
        "current_execution_revision": 7,
        "reconciliation_required": 0,
        "authoritative_rule_snapshot_id": 91,
        "rule_hash": "r" * 64,
        "action_plan_hash": "p" * 64,
    }


def _task_payload(task_id, *, mode="dry_run", canonical_url="https://example.test/lottery", action_plan="{}"):
    return {
        "task_id": task_id,
        "account_id": "11",
        "lottery_id": "7",
        "platform": "bilibili",
        "raw_url": "https://example.test/lottery",
        "canonical_url": canonical_url,
        "dry_run": "0" if mode == "real_run" else "1",
        "mode": mode,
        "selector_config": "{}",
        "action_plan": action_plan,
        "rule_snapshot_id": "91",
        "rule_hash": "r" * 64,
        "action_plan_hash": "p" * 64,
        "execution_evidence_id": "evidence-1" if mode == "real_run" else "",
        "execution_path_id": "bilibili_api_v2",
        "target_hash": "t" * 64,
        "config_hash": CONFIG_HASH_REVISION_7,
        "execution_revision": "7",
        "account_lease_id": "lease-1",
        "account_lease_generation": "3",
        "execution_intent_id": "",
        "execution_intent_hash": "",
        "execution_intent_kind": "",
        "execution_intent_binding_hash": "",
        "requested_actions": "[]",
        "requested_actions_hash": "",
        "requested_action_plan_hash": "",
        "execution_evidence_kind": "",
        "exact_execution_evidence_id": "",
        "oauth_calibration_id": "",
        "weibo_rip_encrypted": "",
    }


def _pending_entry(time_since_delivered):
    """A redis-py xpending_range entry as the parser actually shapes it."""
    return {
        "message_id": "1700000000000-0",
        "consumer": "worker-1",
        "time_since_delivered": time_since_delivered,
        "times_delivered": 1,
    }


def _legacy_real_run_intent_context(task_id: str):
    return {
        "bound_task_id": task_id,
        "bound_lottery_id": 7,
        "bound_account_id": 11,
        "bound_task_mode": "real_run",
        "root_contract_version": None,
        "root_intent_id": None,
        "root_intent_hash": None,
        "binding_contract_version": None,
        "binding_task_id": None,
        "binding_intent_id": None,
        "binding_hash": None,
    }


def _fanout_task_authority(payload: dict):
    platform = str(payload.get("platform") or "bilibili")
    return {
        "task_id": payload["task_id"],
        "account_id": int(payload["account_id"]),
        "lottery_id": int(payload["lottery_id"]),
        "task_mode": payload["mode"],
        "status": "queued",
        "lottery_platform": platform,
        "account_platform": platform,
    }


class PendingIdleMsTests(unittest.TestCase):
    def test_reads_time_since_delivered(self):
        """redis-py exposes idle time as ``time_since_delivered`` (ms), not ``idle``.

        Reading a non-existent ``idle`` key raised KeyError every cycle, which the
        daemon's broad except swallowed — so it never recovered a stuck task.
        """
        self.assertEqual(pending_idle_ms(_pending_entry(130_000)), 130_000)

    def test_no_idle_key_required(self):
        # The pre-fix code did `now_ms - msg["idle"]`; ensure we never touch it.
        entry = _pending_entry(5_000)
        self.assertNotIn("idle", entry)
        self.assertEqual(pending_idle_ms(entry), 5_000)

    def test_threshold_comparison(self):
        stale = pending_idle_ms(_pending_entry(IDLE_THRESHOLD_MS + 1))
        fresh = pending_idle_ms(_pending_entry(IDLE_THRESHOLD_MS - 1))
        self.assertGreaterEqual(stale, IDLE_THRESHOLD_MS)
        self.assertLess(fresh, IDLE_THRESHOLD_MS)

    def test_missing_value_is_zero_not_crash(self):
        self.assertEqual(pending_idle_ms({}), 0)
        self.assertEqual(pending_idle_ms({"time_since_delivered": None}), 0)


class LegacyStreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_plaintext_entry_is_deleted_before_ack_after_convergence(self):
        fake_redis = AsyncMock()
        with patch("app.services.recovery_daemon.redis", fake_redis):
            await _ack_converged_stream_message(
                "1700000000000-0",
                {"task_id": "task-1", "weibo_rip": "8.8.8.8"},
            )

        fake_redis.xdel.assert_awaited_once_with(
            "lottery_tasks", "1700000000000-0"
        )
        fake_redis.xack.assert_awaited_once_with(
            "lottery_tasks", "workers", "1700000000000-0"
        )
        self.assertEqual(
            [call[0] for call in fake_redis.method_calls],
            ["xdel", "xack"],
        )

    async def test_current_encrypted_entry_is_only_acked(self):
        fake_redis = AsyncMock()
        with patch("app.services.recovery_daemon.redis", fake_redis):
            await _ack_converged_stream_message(
                "1700000000000-0",
                {"task_id": "task-1", "weibo_rip_encrypted": "sealed"},
            )

        fake_redis.xdel.assert_not_awaited()
        fake_redis.xack.assert_awaited_once_with(
            "lottery_tasks", "workers", "1700000000000-0"
        )
        fake_redis.eval.assert_not_awaited()

    async def test_platform_entry_is_acked_on_its_own_group(self):
        fake_redis = AsyncMock()
        binding = task_stream_binding_for_platform("bilibili")
        with patch("app.services.recovery_daemon.redis", fake_redis):
            await _ack_converged_stream_message(
                "1700000000000-0",
                {"task_id": "task-1"},
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )

        fake_redis.eval.assert_awaited_once_with(
            ANY,
            1,
            binding.stream_key,
            binding.group_name,
            "1700000000000-0",
        )

    async def test_terminal_fanout_ack_atomically_deletes_provenance_marker(
        self,
    ):
        fake_redis = AsyncMock()
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"status": "succeeded"}
        binding = task_stream_binding_for_platform("bilibili")
        fields = {
            "task_id": "task-1",
            LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
            LEGACY_SOURCE_MESSAGE_ID_FIELD: "1700000000000-0",
        }

        with patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ), patch(
            "app.services.recovery_daemon.database",
            fake_database,
        ):
            await _ack_converged_stream_message(
                "1800000000000-0",
                fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )

        fake_redis.xack.assert_not_awaited()
        fake_redis.eval.assert_awaited_once()
        args = fake_redis.eval.await_args.args
        self.assertIn("SISMEMBER", args[0])
        self.assertIn("XINFO", args[0])
        self.assertIn("XPENDING", args[0])
        self.assertIn("XDEL", args[0])
        self.assertIn("SREM", args[0])
        self.assertIn("SCARD", args[0])
        self.assertIn("DEL", args[0])
        self.assertEqual(args[1], 2)
        self.assertEqual(args[2], binding.stream_key)
        self.assertTrue(str(args[3]).startswith("legacy_task_fanout:"))
        self.assertEqual(
            args[4:],
            (
                binding.group_name,
                "1800000000000-0",
                (
                    f"{binding.stream_key}|task-1|"
                    "1800000000000-0"
                ),
            ),
        )

    async def test_nonterminal_fanout_ack_retains_provenance_marker(self):
        fake_redis = AsyncMock()
        fake_database = AsyncMock()
        fake_database.fetch_one.return_value = {"status": "queued"}
        binding = task_stream_binding_for_platform("bilibili")
        fields = {
            "task_id": "task-1",
            LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
            LEGACY_SOURCE_MESSAGE_ID_FIELD: "1700000000000-0",
        }

        with patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ), patch(
            "app.services.recovery_daemon.database",
            fake_database,
        ):
            await _ack_converged_stream_message(
                "1800000000000-0",
                fields,
                stream_key=binding.stream_key,
                group_name=binding.group_name,
            )

        fake_redis.eval.assert_awaited_once_with(
            ANY,
            1,
            binding.stream_key,
            binding.group_name,
            "1800000000000-0",
        )


class RebuildTaskPayloadRealRunGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_api_recovery_rejects_account_revision_drift(self):
        row = _task_row()
        row["current_execution_revision"] = 8
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=row),
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked, "account_execution_revision_changed"
            ):
                await _rebuild_task_payload("task-dry")

    async def test_real_run_recovery_rechecks_gate_and_fails_closed(self):
        row = _task_row(mode="real_run")
        with patch("app.services.recovery_daemon.database.fetch_one", new=AsyncMock(return_value=row)), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock(return_value={
                 "allowed": False,
                 "failed_gates": ["global_real_run_enabled"],
                 "blockers": [],
             })):
            with self.assertRaises(RealRunRecoveryBlocked):
                await _rebuild_task_payload("task-real")

    async def test_real_run_recovery_rejects_inconsistent_allow_with_raw_blockers(self):
        row = _task_row(mode="real_run")
        fetch = AsyncMock(return_value=row)
        inconsistent_decision = {
            "allowed": True,
            "failed_gates": [],
            "blockers": ["lottery_action_plan_stale"],
        }
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), patch(
            "app.services.recovery_daemon.evaluate_real_run_decision",
            new=AsyncMock(return_value=inconsistent_decision),
        ):
            with self.assertRaisesRegex(
                RealRunRecoveryBlocked,
                "lottery_action_plan_stale",
            ):
                await _rebuild_task_payload("task-real")

        # The immutable outbox row must not be read once the current gate has
        # exposed any blocker, so no payload can reach the re-enqueue path.
        self.assertEqual(fetch.await_count, 1)

    async def test_dry_run_recovery_does_not_call_real_run_gate(self):
        row = _task_row()
        expected = _task_payload("task-dry")
        outbox = {"stream_key": "lottery_tasks", "payload": json.dumps(expected)}
        fetch = AsyncMock(side_effect=[row, outbox])
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=AsyncMock()) as gate:
            payload = await _rebuild_task_payload("task-dry")

        self.assertEqual(payload, expected)
        gate.assert_not_called()

    async def test_repair_recovery_preserves_immutable_missing_action_subset(self):
        row = _task_row(
            mode="real_run",
            canonical_url="canonical://bilibili/dynamic/7",
            action_plan=json.dumps(
                {"required_actions": ["followed", "liked", "commented", "reposted"]}
            ),
        )
        repair_plan = {
            "version": 1,
            "source": "missing_action_repair",
            "required_actions": ["commented"],
            "full_required_actions": ["followed", "liked", "commented", "reposted"],
            "completed_actions": ["followed", "liked", "reposted"],
            "review_required": False,
        }
        original = _task_payload(
            "task-repair",
            mode="real_run",
            canonical_url="canonical://bilibili/dynamic/7",
            action_plan=json.dumps(repair_plan),
        )
        fetch = AsyncMock(
            side_effect=[
                row,
                {
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps(original),
                },
                _legacy_real_run_intent_context("task-repair"),
            ]
        )
        gate = AsyncMock(return_value={"allowed": True, "failed_gates": [], "blockers": []})

        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch), \
             patch("app.services.recovery_daemon.evaluate_real_run_decision", new=gate):
            recovered = await _rebuild_task_payload("task-repair")

        self.assertEqual(json.loads(recovered["action_plan"]), repair_plan)
        self.assertEqual(json.loads(recovered["action_plan"])["required_actions"], ["commented"])

    async def test_missing_immutable_outbox_payload_fails_closed(self):
        row = _task_row()
        fetch = AsyncMock(side_effect=[row, None])
        with patch("app.services.recovery_daemon.database.fetch_one", new=fetch):
            with self.assertRaisesRegex(TaskRecoveryBlocked, "immutable_task_payload_missing"):
                await _rebuild_task_payload("task-dry")

    async def test_platform_stream_rebuild_requires_same_immutable_stream(self):
        row = _task_row()
        expected = _task_payload("task-dry")
        stream_key = "lottery_tasks:bilibili"
        outbox = {"stream_key": stream_key, "payload": json.dumps(expected)}
        fetch = AsyncMock(side_effect=[row, outbox])

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ):
            payload = await _rebuild_task_payload(
                "task-dry",
                stream_key=stream_key,
            )

        self.assertEqual(payload, expected)

    async def test_repair_stream_rebuild_requires_real_repair_envelope(self):
        row = _task_row(mode="real_run")
        expected = _task_payload("task-repair", mode="real_run")
        expected["execution_intent_kind"] = "repair"
        binding = repair_task_stream_binding_for_platform("bilibili")
        outbox = {
            "stream_key": binding.stream_key,
            "payload": json.dumps(expected),
        }
        fetch = AsyncMock(side_effect=[row, outbox])

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ), patch(
            "app.services.recovery_daemon.evaluate_real_run_decision",
            new=AsyncMock(
                return_value={
                    "allowed": True,
                    "failed_gates": [],
                    "blockers": [],
                }
            ),
        ), patch(
            "app.services.recovery_daemon."
            "_validate_recovery_execution_intent_authority",
            new=AsyncMock(),
        ):
            payload = await _rebuild_task_payload(
                "task-repair",
                stream_key=binding.stream_key,
            )

        self.assertEqual(payload, expected)

    async def test_weibo_repair_recovery_uses_subset_rip_and_config_scope(self):
        full_plan = {
            "required_actions": ["liked", "commented"],
            "runtime_capability_requirements": (
                weibo_runtime_capability_requirements(
                    ("liked", "commented")
                )
            ),
        }
        subset_plan = {
            "required_actions": ["liked"],
            "runtime_capability_requirements": (
                weibo_runtime_capability_requirements(("liked",))
            ),
        }
        config_hash = compute_config_hash(
            {
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "execution_revision": 7,
                "runtime_capability_requirements": (
                    subset_plan["runtime_capability_requirements"]
                ),
                "weibo_rip_hash": "",
            }
        )
        canonical_url = "canonical://weibo/status/123456789"
        row = _task_row(
            mode="real_run",
            canonical_url=canonical_url,
            action_plan=json.dumps(full_plan),
        )
        row.update(
            {
                "platform": "weibo",
                "raw_url": canonical_url,
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "config_hash": config_hash,
                "recovery_binding_kind": "repair",
                "recovery_requested_actions": json.dumps(["liked"]),
            }
        )
        expected = _task_payload(
            "task-repair",
            mode="real_run",
            canonical_url=canonical_url,
            action_plan=json.dumps(full_plan),
        )
        expected.update(
            {
                "platform": "weibo",
                "raw_url": canonical_url,
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "config_hash": config_hash,
                "execution_intent_kind": "repair",
            }
        )
        stream = repair_task_stream_binding_for_platform("weibo")
        fetch = AsyncMock(
            side_effect=[
                row,
                {
                    "stream_key": stream.stream_key,
                    "payload": json.dumps(expected),
                },
            ]
        )
        gate = AsyncMock(
            return_value={
                "allowed": True,
                "failed_gates": [],
                "blockers": [],
            }
        )
        durable_binding = Mock(
            binding_kind="repair",
            bound_action_plan=subset_plan,
        )

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ), patch(
            "app.services.recovery_daemon.evaluate_real_run_decision",
            new=gate,
        ), patch(
            "app.services.recovery_daemon."
            "_validate_recovery_execution_intent_authority",
            new=AsyncMock(return_value=durable_binding),
        ):
            recovered = await _rebuild_task_payload(
                "task-repair",
                stream_key=stream.stream_key,
            )

        self.assertEqual(recovered, expected)
        self.assertEqual(
            gate.await_args.kwargs["execution_required_actions"],
            ("liked",),
        )

    async def test_standard_stream_rebuild_rejects_repair_envelope(self):
        row = _task_row(mode="real_run")
        payload = _task_payload("task-repair", mode="real_run")
        payload["execution_intent_kind"] = "repair"
        stream_key = "lottery_tasks:bilibili"
        fetch = AsyncMock(
            side_effect=[
                row,
                {
                    "stream_key": stream_key,
                    "payload": json.dumps(payload),
                },
            ]
        )

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ), patch(
            "app.services.recovery_daemon.evaluate_real_run_decision",
            new=AsyncMock(
                return_value={
                    "allowed": True,
                    "failed_gates": [],
                    "blockers": [],
                }
            ),
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "standard_task_stream_repair_forbidden",
            ):
                await _rebuild_task_payload(
                    "task-repair",
                    stream_key=stream_key,
                )

    async def test_platform_stream_rebuild_rejects_cross_stream_outbox(self):
        row = _task_row()
        expected = _task_payload("task-dry")
        fetch = AsyncMock(
            side_effect=[
                row,
                {
                    "stream_key": "lottery_tasks:weibo",
                    "payload": json.dumps(expected),
                },
            ]
        )

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "immutable_task_payload_missing",
            ):
                await _rebuild_task_payload(
                    "task-dry",
                    stream_key="lottery_tasks:bilibili",
                )

    async def test_fanned_out_recovery_keeps_legacy_outbox_authority(self):
        row = _task_row()
        expected = _task_payload("task-dry")
        source_message_id = "1700000000000-0"
        claimed = {
            **expected,
            LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
            LEGACY_SOURCE_MESSAGE_ID_FIELD: source_message_id,
        }
        fetch = AsyncMock(
            side_effect=[
                row,
                {
                    "stream_key": "lottery_tasks",
                    "payload": json.dumps(expected),
                },
            ]
        )

        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=fetch,
        ):
            rebuilt = await _rebuild_task_payload(
                "task-dry",
                stream_key="lottery_tasks:bilibili",
                claimed_fields=claimed,
            )

        self.assertEqual(rebuilt[LEGACY_SOURCE_STREAM_FIELD], "lottery_tasks")
        self.assertEqual(
            rebuilt[LEGACY_SOURCE_MESSAGE_ID_FIELD],
            source_message_id,
        )


class RecoveryStreamAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_stream_and_payload_are_authoritative(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        outbox = {
            "stream_key": binding.stream_key,
            "payload": json.dumps(payload),
        }
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                payload,
            )

        self.assertEqual(authority, "exact")

    async def test_other_immutable_stream_marks_injected_entry_foreign(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        outbox = {
            "stream_key": "lottery_tasks:weibo",
            "payload": json.dumps(payload),
        }
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                payload,
            )

        self.assertEqual(authority, "foreign")


class TaskStreamGroupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_group_verification_is_idempotent(self):
        class RacingRedis:
            def __init__(self):
                self.calls = 0

            async def xinfo_groups(self, *_args, **_kwargs):
                self.calls += 1
                await asyncio.sleep(0)
                return [{"name": binding.group_name}]

        binding = task_stream_binding_for_platform("bilibili")
        fake_redis = RacingRedis()
        with patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            await asyncio.gather(
                _ensure_task_stream_group(binding),
                _ensure_task_stream_group(binding),
            )

        self.assertEqual(fake_redis.calls, 2)

    async def test_group_verification_does_not_hide_redis_errors(self):
        fake_redis = AsyncMock()
        fake_redis.xinfo_groups.side_effect = RuntimeError(
            "NOAUTH authentication required"
        )
        binding = task_stream_binding_for_platform("weibo")

        with patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "redis_consumer_group_topology_unavailable",
            ):
                await _ensure_task_stream_group(binding)

    async def test_legacy_fanout_revalidates_restored_group_then_drains(self):
        processed = asyncio.Event()

        class LostGroupRedis:
            def __init__(self):
                self.group_calls = 0
                self.read_calls = 0
                self.wait_forever = asyncio.Event()

            async def xinfo_groups(self, stream):
                self.group_calls += 1
                self.asserted = stream
                return [
                    {
                        "name": (
                            LEGACY_TASK_STREAM_BINDING.group_name
                        )
                    }
                ]

            async def xreadgroup(self, *_args, **_kwargs):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise RuntimeError(
                        "NOGROUP No such key or consumer group"
                    )
                if self.read_calls == 2:
                    return [
                        (
                            "lottery_tasks",
                            [
                                (
                                    "1700000000000-0",
                                    {
                                        "task_id": "task-1",
                                        "platform": "bilibili",
                                    },
                                )
                            ],
                        )
                    ]
                await self.wait_forever.wait()
                return []

        async def process(*_args):
            processed.set()
            return True

        fake_redis = LostGroupRedis()
        with patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ), patch(
            "app.services.recovery_daemon._process_legacy_claimed_message",
            side_effect=process,
        ), patch(
            "app.services.recovery_daemon.asyncio.sleep",
            new=AsyncMock(),
        ), patch(
            "app.services.recovery_daemon.structured_log",
        ):
            loop = asyncio.create_task(_legacy_fanout_loop())
            try:
                await asyncio.wait_for(processed.wait(), timeout=1)
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

        self.assertEqual(fake_redis.group_calls, 2)
        self.assertGreaterEqual(fake_redis.read_calls, 2)
        self.assertEqual(
            fake_redis.asserted,
            LEGACY_TASK_STREAM_BINDING.stream_key,
        )


class RecoveryExecutionIntentAuthorityTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_recovery_resolves_historical_root_from_task_binding(self):
        payload = _task_payload("task-legacy", mode="real_run")
        reader = AsyncMock(
            return_value=_legacy_real_run_intent_context("task-legacy")
        )
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=reader,
        ):
            await _validate_recovery_execution_intent_authority(payload)

        query = " ".join(reader.await_args.args[0].split())
        self.assertLess(
            query.index(
                "LEFT JOIN task_execution_intent_bindings binding"
            ),
            query.index("LEFT JOIN lottery_execution_intents root"),
        )
        self.assertIn(
            "root.lottery_id = binding.lottery_id",
            query,
        )
        self.assertIn(
            "root.intent_id = binding.intent_id",
            query,
        )
        self.assertNotIn("root.lottery_id = tr.lottery_id", query)

    async def test_pre_contract_real_run_requires_empty_message_and_no_db_rows(
        self,
    ):
        payload = _task_payload("task-legacy", mode="real_run")
        context = _legacy_real_run_intent_context("task-legacy")
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=context),
        ):
            await _validate_recovery_execution_intent_authority(payload)

    async def test_root_without_task_binding_is_blocked(self):
        payload = _task_payload("task-legacy", mode="real_run")
        context = _legacy_real_run_intent_context("task-legacy")
        context["root_contract_version"] = 1
        context["root_intent_id"] = "11111111-1111-1111-1111-111111111111"
        context["root_intent_hash"] = "a" * 64
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=context),
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "execution_intent_database_binding_missing",
            ):
                await _validate_recovery_execution_intent_authority(payload)

    async def test_message_contract_without_database_root_is_blocked(self):
        payload = _task_payload("task-legacy", mode="real_run")
        payload["execution_intent_id"] = (
            "11111111-1111-1111-1111-111111111111"
        )
        context = _legacy_real_run_intent_context("task-legacy")
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=context),
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "execution_intent_database_root_missing",
            ):
                await _validate_recovery_execution_intent_authority(payload)

    async def test_new_contract_matches_rebuilt_database_binding(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        intent_id = "22222222-2222-2222-2222-222222222222"
        bound_plan = {"version": 2, "required_actions": ["followed"]}
        expected = TaskExecutionIntentBinding(
            contract_version=1,
            task_id=task_id,
            intent_id=intent_id,
            intent_hash="a" * 64,
            lottery_id=7,
            account_id=11,
            binding_kind="full",
            requested_actions=("followed",),
            requested_actions_hash="b" * 64,
            bound_action_plan=bound_plan,
            bound_action_plan_hash="c" * 64,
            evidence_action_plan_hash="d" * 64,
            rule_snapshot_id=91,
            rule_hash="e" * 64,
            execution_evidence_id=(
                "33333333-3333-3333-3333-333333333333"
            ),
            execution_evidence_kind="exact_execution_evidence",
            exact_execution_evidence_id=(
                "33333333-3333-3333-3333-333333333333"
            ),
            oauth_calibration_id=None,
            execution_path_id="bilibili_api_v2",
            target_hash="f" * 64,
            config_hash="1" * 64,
            execution_revision=7,
            account_lease_id=(
                "44444444-4444-4444-4444-444444444444"
            ),
            account_lease_generation=3,
            binding_hash="2" * 64,
        )
        payload = _task_payload(task_id, mode="real_run")
        payload.update(
            {
                "action_plan": json.dumps(bound_plan),
                "execution_intent_id": expected.intent_id,
                "execution_intent_hash": expected.intent_hash,
                "execution_intent_kind": expected.binding_kind,
                "execution_intent_binding_hash": expected.binding_hash,
                "requested_actions": json.dumps(
                    list(expected.requested_actions)
                ),
                "requested_actions_hash": (
                    expected.requested_actions_hash
                ),
                "requested_action_plan_hash": (
                    expected.bound_action_plan_hash
                ),
                "execution_evidence_kind": (
                    expected.execution_evidence_kind
                ),
                "exact_execution_evidence_id": (
                    expected.exact_execution_evidence_id
                ),
                "oauth_calibration_id": "",
            }
        )
        context = {
            **_legacy_real_run_intent_context(task_id),
            "root_contract_version": 1,
            "root_intent_id": expected.intent_id,
            "root_intent_hash": expected.intent_hash,
            "binding_contract_version": expected.contract_version,
            "binding_task_id": expected.task_id,
            "binding_intent_id": expected.intent_id,
            "binding_lottery_id": expected.lottery_id,
            "binding_account_id": expected.account_id,
            "binding_kind": expected.binding_kind,
            "binding_requested_actions": json.dumps(
                list(expected.requested_actions)
            ),
            "binding_requested_actions_hash": (
                expected.requested_actions_hash
            ),
            "binding_bound_action_plan": json.dumps(bound_plan),
            "binding_bound_action_plan_hash": (
                expected.bound_action_plan_hash
            ),
            "binding_evidence_action_plan_hash": (
                expected.evidence_action_plan_hash
            ),
            "binding_rule_snapshot_id": expected.rule_snapshot_id,
            "binding_rule_hash": expected.rule_hash,
            "binding_execution_evidence_id": (
                expected.execution_evidence_id
            ),
            "binding_execution_evidence_kind": (
                expected.execution_evidence_kind
            ),
            "binding_exact_execution_evidence_id": (
                expected.exact_execution_evidence_id
            ),
            "binding_oauth_calibration_id": None,
            "binding_execution_path_id": expected.execution_path_id,
            "binding_target_hash": expected.target_hash,
            "binding_config_hash": expected.config_hash,
            "binding_execution_revision": expected.execution_revision,
            "binding_account_lease_id": expected.account_lease_id,
            "binding_account_lease_generation": (
                expected.account_lease_generation
            ),
            "binding_hash": expected.binding_hash,
        }
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=context),
        ), patch(
            "app.services.recovery_daemon.coerce_frozen_execution_intent",
            return_value=Mock(
                full_action_plan=bound_plan,
                full_action_plan_hash=payload["action_plan_hash"],
            ),
        ), patch(
            "app.services.recovery_daemon.build_task_execution_intent_binding",
            return_value=expected,
        ):
            await _validate_recovery_execution_intent_authority(payload)
            payload["exact_execution_evidence_id"] = (
                "55555555-5555-5555-5555-555555555555"
            )
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "execution_intent_message_binding_mismatch",
            ):
                await _validate_recovery_execution_intent_authority(payload)
            payload["exact_execution_evidence_id"] = (
                expected.exact_execution_evidence_id
            )
            context["binding_execution_evidence_kind"] = (
                "oauth_account_calibration"
            )
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "execution_intent_database_binding_mismatch",
            ):
                await _validate_recovery_execution_intent_authority(payload)

    async def test_repair_replay_keeps_full_plan_and_subset_hash_binding(self):
        task_id = "11111111-1111-1111-1111-111111111111"
        intent_id = "22222222-2222-2222-2222-222222222222"
        full_plan = {
            "version": 2,
            "required_actions": ["followed", "liked"],
        }
        subset_plan = {"version": 2, "required_actions": ["liked"]}
        expected = TaskExecutionIntentBinding(
            contract_version=1,
            task_id=task_id,
            intent_id=intent_id,
            intent_hash="a" * 64,
            lottery_id=7,
            account_id=11,
            binding_kind="repair",
            requested_actions=("liked",),
            requested_actions_hash="b" * 64,
            bound_action_plan=subset_plan,
            bound_action_plan_hash="c" * 64,
            evidence_action_plan_hash="p" * 64,
            rule_snapshot_id=91,
            rule_hash="e" * 64,
            execution_evidence_id=(
                "33333333-3333-3333-3333-333333333333"
            ),
            execution_evidence_kind="exact_execution_evidence",
            exact_execution_evidence_id=(
                "33333333-3333-3333-3333-333333333333"
            ),
            oauth_calibration_id=None,
            execution_path_id="bilibili_api_v2",
            target_hash="f" * 64,
            config_hash="1" * 64,
            execution_revision=7,
            account_lease_id=(
                "44444444-4444-4444-4444-444444444444"
            ),
            account_lease_generation=3,
            binding_hash="2" * 64,
        )
        payload = _task_payload(task_id, mode="real_run")
        payload.update(
            {
                "action_plan": json.dumps(full_plan),
                "action_plan_hash": "p" * 64,
                "execution_intent_id": expected.intent_id,
                "execution_intent_hash": expected.intent_hash,
                "execution_intent_kind": "repair",
                "execution_intent_binding_hash": expected.binding_hash,
                "requested_actions": json.dumps(["liked"]),
                "requested_actions_hash": (
                    expected.requested_actions_hash
                ),
                "requested_action_plan_hash": (
                    expected.bound_action_plan_hash
                ),
                "execution_evidence_kind": (
                    expected.execution_evidence_kind
                ),
                "exact_execution_evidence_id": (
                    expected.exact_execution_evidence_id
                ),
                "oauth_calibration_id": "",
            }
        )
        context = {
            **_legacy_real_run_intent_context(task_id),
            "root_contract_version": 1,
            "root_intent_id": expected.intent_id,
            "root_intent_hash": expected.intent_hash,
            "binding_contract_version": 1,
            "binding_task_id": task_id,
            "binding_intent_id": intent_id,
            "binding_lottery_id": 7,
            "binding_account_id": 11,
            "binding_kind": "repair",
            "binding_requested_actions": json.dumps(["liked"]),
            "binding_requested_actions_hash": (
                expected.requested_actions_hash
            ),
            "binding_bound_action_plan": json.dumps(subset_plan),
            "binding_bound_action_plan_hash": (
                expected.bound_action_plan_hash
            ),
            "binding_evidence_action_plan_hash": "p" * 64,
            "binding_rule_snapshot_id": expected.rule_snapshot_id,
            "binding_rule_hash": expected.rule_hash,
            "binding_execution_evidence_id": (
                expected.execution_evidence_id
            ),
            "binding_execution_evidence_kind": (
                expected.execution_evidence_kind
            ),
            "binding_exact_execution_evidence_id": (
                expected.exact_execution_evidence_id
            ),
            "binding_oauth_calibration_id": None,
            "binding_execution_path_id": expected.execution_path_id,
            "binding_target_hash": expected.target_hash,
            "binding_config_hash": expected.config_hash,
            "binding_execution_revision": expected.execution_revision,
            "binding_account_lease_id": expected.account_lease_id,
            "binding_account_lease_generation": 3,
            "binding_hash": expected.binding_hash,
        }
        frozen = Mock(
            full_action_plan=full_plan,
            full_action_plan_hash="p" * 64,
        )
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=context),
        ), patch(
            "app.services.recovery_daemon.coerce_frozen_execution_intent",
            return_value=frozen,
        ), patch(
            "app.services.recovery_daemon.build_task_execution_intent_binding",
            return_value=expected,
        ):
            await _validate_recovery_execution_intent_authority(payload)

    async def test_payload_mismatch_remains_pending_unverified(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        claimed = dict(payload)
        claimed["account_id"] = "999"
        outbox = {
            "stream_key": binding.stream_key,
            "payload": json.dumps(payload),
        }
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                claimed,
            )

        self.assertEqual(authority, "unverified")

    async def test_fanned_out_copy_uses_legacy_outbox_as_authority(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        claimed = {
            **payload,
            LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
            LEGACY_SOURCE_MESSAGE_ID_FIELD: "1700000000000-0",
        }
        outbox = {
            "stream_key": "lottery_tasks",
            "payload": json.dumps(payload),
        }
        fake_redis = AsyncMock()
        fake_redis.sismember.return_value = True
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                claimed,
                message_id="1800000000000-0",
            )

        self.assertEqual(authority, "exact")
        fake_redis.sismember.assert_awaited_once()
        self.assertEqual(
            fake_redis.sismember.await_args.args[1],
            (
                f"{binding.stream_key}|task-dry|"
                "1800000000000-0"
            ),
        )

    async def test_forged_fanout_metadata_without_marker_is_unverified(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        claimed = {
            **payload,
            LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
            LEGACY_SOURCE_MESSAGE_ID_FIELD: "1700000000000-0",
        }
        outbox = {
            "stream_key": "lottery_tasks",
            "payload": json.dumps(payload),
        }
        fake_redis = AsyncMock()
        fake_redis.sismember.return_value = False
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                claimed,
                message_id="1800000000000-0",
            )

        self.assertEqual(authority, "unverified")

    async def test_legacy_outbox_without_fanout_metadata_is_foreign(self):
        binding = task_stream_binding_for_platform("bilibili")
        payload = _task_payload("task-dry")
        outbox = {
            "stream_key": "lottery_tasks",
            "payload": json.dumps(payload),
        }
        with patch(
            "app.services.recovery_daemon.database.fetch_one",
            new=AsyncMock(return_value=outbox),
        ):
            authority = await _recovery_stream_authority(
                "task-dry",
                binding,
                payload,
            )

        self.assertEqual(authority, "foreign")


class LegacyFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_legacy_platform_prefix_does_not_block_batch_sibling(
        self,
    ):
        blocked = asyncio.Event()
        weibo_processed = asyncio.Event()

        class OneBatchRedis:
            def __init__(self):
                self.delivered = False
                self.wait_forever = asyncio.Event()

            async def xinfo_groups(self, _stream):
                return [
                    {
                        "name": (
                            LEGACY_TASK_STREAM_BINDING.group_name
                        )
                    }
                ]

            async def xreadgroup(self, *_args, **_kwargs):
                if not self.delivered:
                    self.delivered = True
                    entries = [
                        (
                            f"{number}-0",
                            {"task_id": f"b-{number}", "platform": "bilibili"},
                        )
                        for number in range(1, 50)
                    ]
                    entries.append(
                        (
                            "50-0",
                            {"task_id": "w-50", "platform": "weibo"},
                        )
                    )
                    return [("lottery_tasks", entries)]
                await self.wait_forever.wait()
                return []

        async def process(_message_id, fields):
            if fields["platform"] == "bilibili":
                await blocked.wait()
            else:
                weibo_processed.set()
            return True

        loop = asyncio.create_task(_legacy_fanout_loop())
        with patch(
            "app.services.recovery_daemon.redis",
            OneBatchRedis(),
        ), patch(
            "app.services.recovery_daemon._process_legacy_claimed_message",
            side_effect=process,
        ):
            try:
                await asyncio.wait_for(weibo_processed.wait(), timeout=1)
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_exact_legacy_payload_uses_one_atomic_redis_transfer(self):
        payload = _task_payload("task-dry")
        database = AsyncMock()
        database.fetch_one.side_effect = [
            {
                "stream_key": "lottery_tasks",
                "payload": json.dumps(payload),
            },
            _fanout_task_authority(payload),
        ]
        fake_redis = AsyncMock()
        fake_redis.eval.return_value = "1800000000000-0"

        with patch("app.services.recovery_daemon.database", database), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            target_id = await _fanout_legacy_claimed_message(
                "1700000000000-0",
                payload,
            )

        self.assertEqual(target_id, "1800000000000-0")
        fake_redis.eval.assert_awaited_once()
        args = fake_redis.eval.await_args.args
        self.assertEqual(args[1], 3)
        self.assertEqual(args[2], "lottery_tasks")
        self.assertEqual(args[3], "lottery_tasks:bilibili")
        self.assertEqual(args[5], "workers")
        self.assertEqual(args[6], "1700000000000-0")
        self.assertEqual(
            args[7],
            "lottery_tasks:bilibili|task-dry|",
        )
        self.assertIn(LEGACY_SOURCE_STREAM_FIELD, args)
        self.assertIn(LEGACY_SOURCE_MESSAGE_ID_FIELD, args)

    async def test_fanout_recovery_authorizes_new_target_before_old_ack(self):
        binding = task_stream_binding_for_platform("bilibili")
        fake_redis = AsyncMock()
        fake_redis.eval.return_value = "1900000000000-0"
        with patch("app.services.recovery_daemon.redis", fake_redis):
            target_id = await _reenqueue_fanned_out_task(
                binding,
                {
                    "task_id": "task-dry",
                    LEGACY_SOURCE_STREAM_FIELD: "lottery_tasks",
                    LEGACY_SOURCE_MESSAGE_ID_FIELD: "1700000000000-0",
                },
                message_id="1800000000000-0",
                marker_key="legacy_task_fanout:marker",
            )

        self.assertEqual(target_id, "1900000000000-0")
        args = fake_redis.eval.await_args.args
        self.assertIn("SADD", args[0])
        self.assertIn("XINFO", args[0])
        self.assertIn("XPENDING", args[0])
        self.assertIn("XDEL", args[0])
        self.assertIn("SREM", args[0])
        self.assertNotIn("redis.call('DEL'", args[0])
        self.assertLess(args[0].index("SADD"), args[0].index("XACK"))
        self.assertLess(args[0].index("XACK"), args[0].index("XDEL"))
        self.assertEqual(args[1:6], (
            2,
            binding.stream_key,
            "legacy_task_fanout:marker",
            binding.group_name,
            "1800000000000-0",
        ))
        self.assertEqual(
            args[6],
            "lottery_tasks:bilibili|task-dry|",
        )

    async def test_pre_intent_outbox_gets_only_safe_intent_defaults(self):
        payload = _task_payload("task-dry")
        for field in (
            "execution_intent_id",
            "execution_intent_hash",
            "execution_intent_kind",
            "execution_intent_binding_hash",
            "requested_actions",
            "requested_actions_hash",
            "requested_action_plan_hash",
            "execution_evidence_kind",
            "exact_execution_evidence_id",
            "oauth_calibration_id",
        ):
            payload.pop(field)
        database = AsyncMock()
        database.fetch_one.side_effect = [
            {
                "stream_key": "lottery_tasks",
                "payload": json.dumps(payload),
            },
            _fanout_task_authority(payload),
        ]
        fake_redis = AsyncMock()
        fake_redis.eval.return_value = "1800000000000-0"

        with patch("app.services.recovery_daemon.database", database), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            await _fanout_legacy_claimed_message(
                "1700000000000-0",
                payload,
            )

        args = fake_redis.eval.await_args.args
        requested_actions_index = args.index("requested_actions")
        self.assertEqual(args[requested_actions_index + 1], "[]")
        intent_id_index = args.index("execution_intent_id")
        self.assertEqual(args[intent_id_index + 1], "")

    async def test_pre_platform_bilibili_uses_two_database_authorities(self):
        payload = _task_payload("task-dry")
        payload.pop("platform")
        database = AsyncMock()
        database.fetch_one.side_effect = [
            {
                "stream_key": "lottery_tasks",
                "payload": json.dumps(payload),
            },
            _fanout_task_authority(payload),
        ]
        fake_redis = AsyncMock()
        fake_redis.eval.return_value = "1800000000000-0"

        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            await _fanout_legacy_claimed_message(
                "1700000000000-0",
                payload,
            )

        args = fake_redis.eval.await_args.args
        self.assertEqual(args[3], "lottery_tasks:bilibili")
        platform_index = args.index("platform")
        self.assertEqual(args[platform_index + 1], "bilibili")

    async def test_missing_platform_for_non_bilibili_authority_stays_pending(
        self,
    ):
        payload = _task_payload("task-dry")
        payload.pop("platform")
        authority = _fanout_task_authority(payload)
        authority["lottery_platform"] = "weibo"
        authority["account_platform"] = "weibo"
        database = AsyncMock()
        database.fetch_one.side_effect = [
            {
                "stream_key": "lottery_tasks",
                "payload": json.dumps(payload),
            },
            authority,
        ]
        fake_redis = AsyncMock()

        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "legacy_fanout_platform_authority_missing",
            ):
                await _fanout_legacy_claimed_message(
                    "1700000000000-0",
                    payload,
                )

        fake_redis.eval.assert_not_awaited()

    async def test_claimed_payload_mismatch_is_not_transferred_or_acked(self):
        payload = _task_payload("task-dry")
        claimed = dict(payload)
        claimed["account_id"] = "999"
        database = AsyncMock()
        database.fetch_one.return_value = {
            "stream_key": "lottery_tasks",
            "payload": json.dumps(payload),
        }
        fake_redis = AsyncMock()

        with patch("app.services.recovery_daemon.database", database), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "legacy_fanout_payload_binding_mismatch",
            ):
                await _fanout_legacy_claimed_message(
                    "1700000000000-0",
                    claimed,
                )

        fake_redis.eval.assert_not_awaited()

    async def test_unknown_platform_is_left_for_operator_repair(self):
        payload = _task_payload("task-dry")
        payload["platform"] = "unknown"
        database = AsyncMock()
        database.fetch_one.side_effect = [
            {
                "stream_key": "lottery_tasks",
                "payload": json.dumps(payload),
            },
            _fanout_task_authority(payload),
        ]
        fake_redis = AsyncMock()

        with patch("app.services.recovery_daemon.database", database), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "legacy_fanout_platform_unsupported",
            ):
                await _fanout_legacy_claimed_message(
                    "1700000000000-0",
                    payload,
                )

        fake_redis.eval.assert_not_awaited()

    async def test_legacy_fanout_never_forwards_repair_protocol(self):
        payload = _task_payload("task-repair", mode="real_run")
        payload["execution_intent_kind"] = "repair"
        database = AsyncMock()
        database.fetch_one.return_value = {
            "stream_key": "lottery_tasks",
            "payload": json.dumps(payload),
        }
        fake_redis = AsyncMock()

        with patch(
            "app.services.recovery_daemon.database",
            database,
        ), patch(
            "app.services.recovery_daemon.redis",
            fake_redis,
        ):
            with self.assertRaisesRegex(
                TaskRecoveryBlocked,
                "legacy_task_stream_repair_forbidden",
            ):
                await _fanout_legacy_claimed_message(
                    "1700000000000-0",
                    payload,
                )

        fake_redis.eval.assert_not_awaited()


class FakeRecoveryDatabase:
    def __init__(
        self,
        *,
        lease_active,
        task_mode="dry_run",
        status="running",
        breaker_status="open",
        execution_intent_kind=None,
    ):
        self.task = {
            "task_id": "task-1",
            "account_id": 11,
            "lottery_id": 7,
            "account_lease_id": "lease-task-1",
            "account_lease_generation": 5,
            "status": status,
            "worker_id": "worker-old",
            "task_mode": task_mode,
            "execution_intent_kind": execution_intent_kind,
            "reconciliation_required": 0,
            "lease_active": 1 if lease_active else 0,
        }
        self.executions = []
        self.breaker_status = breaker_status

    def transaction(self):
        class Transaction:
            async def __aenter__(self_inner):
                return self

            async def __aexit__(self_inner, *exc):
                return False

        return Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM task_runs" in query:
            return dict(self.task)
        if "FROM lotteries" in query:
            return {"id": 7, "platform": "bilibili"}
        if "FROM accounts" in query:
            return {"id": 11}
        if "FROM circuit_breakers" in query:
            return {"status": self.breaker_status}
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, values=None):
        self.executions.append((query, dict(values or {})))
        if "UPDATE task_runs" in query:
            next_status = "failed" if "SET status = 'failed'" in query else "queued"
            self.task.update({"status": next_status, "worker_id": None, "lease_active": 0})
            if "reconciliation_required = 1" in query:
                self.task["reconciliation_required"] = 1
        return 1


class PrepareTaskForRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_running_owner_is_revoked_before_reenqueue(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="shadow_run")
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "recover")
        self.assertEqual(fake.task["status"], "queued")
        self.assertIsNone(fake.task["worker_id"])
        self.assertTrue(any("SET status = 'claimed'" in query for query, _ in fake.executions))
        self.assertFalse(any("SET status = 'ready'" in query for query, _ in fake.executions))

    async def test_expired_real_run_is_quarantined_not_reenqueued(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="real_run")
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "real_run_reconciliation_required")
        self.assertEqual(fake.task["status"], "failed")
        self.assertEqual(fake.task["reconciliation_required"], 1)
        self.assertTrue(any("external outcome requires reconciliation" in query for query, _ in fake.executions))
        terminal_query = next(
            query for query, _ in fake.executions if "external outcome requires reconciliation" in query
        )
        self.assertNotIn("worker_id = NULL", terminal_query)
        self.assertNotIn("stream_message_id = NULL", terminal_query)
        self.assertTrue(any("SET status = 'cooling'" in query for query, _ in fake.executions))
        self.assertFalse(any("execution_lock = NULL" in query for query, _ in fake.executions))
        unknown_intent_query = next(
            query for query, _ in fake.executions
            if "UPDATE external_action_intents" in query
        )
        self.assertIn("effect_certainty = 'unknown'", unknown_intent_query)
        self.assertIn("completed_at = COALESCE(completed_at, NOW())", unknown_intent_query)
        breaker_writes = [(query, values) for query, values in fake.executions if "circuit_breakers" in query]
        self.assertEqual(len(breaker_writes), 1)
        self.assertEqual(breaker_writes[0][1]["scope"], "platform:bilibili")

    async def test_expired_real_run_is_not_settled_without_confirmed_breaker(self):
        fake = FakeRecoveryDatabase(
            lease_active=False,
            task_mode="real_run",
            breaker_status="closed",
        )
        with patch("app.services.recovery_daemon.database", fake):
            with self.assertRaisesRegex(RuntimeError, "breaker_not_persisted"):
                await _prepare_task_for_recovery("task-1")

    async def test_active_lease_is_not_revoked_after_xclaim_race(self):
        fake = FakeRecoveryDatabase(lease_active=True)
        with patch("app.services.recovery_daemon.database", fake):
            result = await _prepare_task_for_recovery("task-1")

        self.assertEqual(result, "skip_owned_running_task")
        self.assertEqual(fake.task["status"], "running")
        self.assertEqual(fake.executions, [])


class RecoveryTerminalSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_block_cleanup_does_not_touch_reclaimed_running_task(self):
        fake = FakeRecoveryDatabase(lease_active=True, task_mode="shadow_run", status="running")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_blocked("task-1", "current gate failed")

        self.assertFalse(settled)
        self.assertEqual(fake.task["status"], "running")
        self.assertEqual(fake.executions, [])

    async def test_gate_block_cleanup_settles_still_queued_task(self):
        fake = FakeRecoveryDatabase(lease_active=False, task_mode="shadow_run", status="queued")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_blocked("task-1", "current gate failed")

        self.assertTrue(settled)
        self.assertEqual(fake.task["status"], "failed")
        self.assertTrue(any("UPDATE lotteries" in query for query, _ in fake.executions))
        lease_query, lease_values = next(
            (query, values)
            for query, values in fake.executions
            if "UPDATE account_operation_leases" in query
        )
        self.assertIn("generation = :lease_generation", lease_query)
        self.assertIn("owner_id = :task_id", lease_query)
        self.assertEqual(lease_values["lease_id"], "lease-task-1")
        self.assertEqual(lease_values["lease_generation"], 5)
        self.assertEqual(lease_values["operation_kind"], "shadow_run")
        self.assertEqual(lease_values["task_id"], "task-1")

    async def test_gate_block_cleanup_releases_only_repair_lease(self):
        fake = FakeRecoveryDatabase(
            lease_active=False,
            task_mode="real_run",
            status="queued",
            execution_intent_kind="repair",
        )
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_blocked(
                "task-1",
                "current gate failed",
            )

        self.assertTrue(settled)
        lease_query, lease_values = next(
            (query, values)
            for query, values in fake.executions
            if "UPDATE account_operation_leases" in query
        )
        self.assertIn("operation_kind = :operation_kind", lease_query)
        self.assertEqual(lease_values["operation_kind"], "repair_run")

    async def test_exhausted_cleanup_skips_task_that_is_no_longer_queued(self):
        fake = FakeRecoveryDatabase(lease_active=True, task_mode="dry_run", status="running")
        with patch("app.services.recovery_daemon.database", fake):
            settled = await _mark_recovery_exhausted("task-1")

        self.assertFalse(settled)
        self.assertEqual(fake.executions, [])


if __name__ == "__main__":
    unittest.main()
