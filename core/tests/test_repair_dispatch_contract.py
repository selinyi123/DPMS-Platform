import asyncio
import base64
import json
import os
import unittest
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from fastapi import HTTPException  # noqa: E402

from app.action_plan import compute_action_plan_hash, compute_target_hash  # noqa: E402
from app.api.lotteries import (  # noqa: E402
    RealRunCompletionAuthority,
    dispatch_lottery_repair,
    repair_dispatch_workers_ready,
    worker_rows_support_repair_dispatch,
)
from app.models.schemas import DispatchTaskRequest  # noqa: E402
from app.services.execution_intents import (  # noqa: E402
    build_frozen_execution_intent,
)
from app.task_streams import (  # noqa: E402
    repair_task_stream_binding_for_platform,
)


TASK_ID = "00000000-0000-4000-8000-000000000011"
SOURCE_TASK_ID = "00000000-0000-4000-8000-000000000012"
INTENT_ID = "00000000-0000-4000-8000-000000000013"
EVIDENCE_ID = "00000000-0000-4000-8000-000000000014"
LEASE_ID = "00000000-0000-4000-8000-000000000015"


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _RepairDatabase:
    def __init__(self, lottery, intent):
        self.lottery = lottery
        self.intent = intent
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values=None):
        if "FROM lottery_execution_intent_heads AS head" in query:
            return {
                **self.intent.__dict__,
                "current_generation": 1,
                "full_action_plan": json.dumps(
                    self.intent.full_action_plan,
                    ensure_ascii=False,
                ),
                "full_required_actions": json.dumps(
                    list(self.intent.full_required_actions),
                    ensure_ascii=False,
                ),
            }
        if "FROM lotteries" in query:
            return dict(self.lottery)
        raise AssertionError(query)

    async def execute(self, query, values=None):
        self.executions.append((str(query), dict(values or {})))
        return 1


def _contract():
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "required_actions": ["liked", "commented"],
        "action_payloads": {
            "liked": {},
            "commented": {"text": "参与抽奖"},
        },
        "content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {},
        "execution_path_id": "bilibili_api_v2",
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "review_required": False,
        "executable": True,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": [],
        "represented_requirements": [],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": [],
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    canonical_url = "https://t.bilibili.com/73"
    lottery = {
        "id": 73,
        "platform": "bilibili",
        "status": "pending",
        "execution_lock": None,
        "raw_url": canonical_url,
        "canonical_url": canonical_url,
        "rule_text": "点赞并评论参与",
        "action_plan": plan,
        "authoritative_rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
    }
    plan_binding = {
        "rule_snapshot_id": 101,
        "rule_hash": "a" * 64,
        "action_plan_hash": plan["plan_hash"],
        "execution_path_id": "bilibili_api_v2",
        "target_hash": compute_target_hash(canonical_url),
        "config_hash": "b" * 64,
        "execution_revision": 7,
        "required_actions": ("liked", "commented"),
        "action_plan": plan,
    }
    intent = build_frozen_execution_intent(
        lottery,
        source_task_id=SOURCE_TASK_ID,
        source_account_id=9,
        plan_binding=plan_binding,
        intent_id=INTENT_ID,
    )
    return lottery, plan_binding, intent


def _repair_lane(
    platform,
    *,
    status="healthy",
    success_age=1,
    failures=0,
    progress_operation=None,
    progress_age=None,
    inflight_count=0,
    inflight_limit=32,
    saturated=False,
):
    binding = repair_task_stream_binding_for_platform(platform)
    return {
        "stream": binding.stream_key,
        "group": binding.group_name,
        "platform": binding.platform,
        "repair": True,
        "protocol_version": binding.protocol_version,
        "status": status,
        "last_success_operation": "xreadgroup",
        "last_success_age_seconds": success_age,
        "last_loop_progress_operation": progress_operation,
        "last_loop_progress_age_seconds": progress_age,
        "inflight_count": inflight_count,
        "inflight_limit": inflight_limit,
        "saturated": saturated,
        "last_error_operation": (
            "xreadgroup" if failures else None
        ),
        "last_error_type": (
            "RuntimeError" if failures else None
        ),
        "last_error_age_seconds": 1 if failures else None,
        "consecutive_failures": failures,
    }


def _repair_heartbeat(
    worker_id,
    lanes,
    *,
    heartbeat_age=1,
    capable=True,
    health_contract_version=1,
):
    return {
        "worker_id": worker_id,
        "heartbeat_age_seconds": heartbeat_age,
        "detail": json.dumps(
            {
                "execution_intent_contract_version": 1,
                "capabilities": (
                    ["repair_execution_intent_v1"]
                    if capable
                    else []
                ),
                "task_consumer_name": worker_id,
                "task_lane_health": {
                    "contract_version": health_contract_version,
                    "lanes": list(lanes),
                },
            }
        ),
    }


class _RepairLaneDatabase:
    def __init__(self, rows):
        self.rows = list(rows)

    async def fetch_all(self, query, values=None):
        if "FROM worker_heartbeats" not in str(query):
            raise AssertionError(query)
        return list(self.rows)


class _RepairLaneRedis:
    def __init__(self, consumers_by_stream):
        self.consumers_by_stream = dict(consumers_by_stream)

    async def xinfo_consumers(self, stream, group):
        binding = next(
            (
                repair_task_stream_binding_for_platform(platform)
                for platform in (
                    "bilibili",
                    "weibo",
                    "xiaohongshu",
                    "douyin",
                )
                if repair_task_stream_binding_for_platform(
                    platform
                ).stream_key
                == stream
            ),
            None,
        )
        if binding is None or binding.group_name != group:
            raise AssertionError((stream, group))
        return list(self.consumers_by_stream.get(stream, ()))


class RepairDispatchContractTests(unittest.IsolatedAsyncioTestCase):
    def test_v1_recent_read_remains_rolling_upgrade_compatible(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        self.assertTrue(
            worker_rows_support_repair_dispatch(
                [
                    _repair_heartbeat(
                        "worker-v1",
                        [_repair_lane("bilibili", success_age=43)],
                        heartbeat_age=2,
                        health_contract_version=1,
                    )
                ],
                binding=binding,
                active_consumer_names=frozenset({"worker-v1"}),
            )
        )

    def test_v2_saturated_lane_accepts_fresh_progress_after_old_read(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        saturated_lane = _repair_lane(
            "bilibili",
            success_age=61,
            progress_operation="capacity_wait",
            progress_age=2,
            inflight_count=32,
            inflight_limit=32,
            saturated=True,
        )
        self.assertTrue(
            worker_rows_support_repair_dispatch(
                [
                    _repair_heartbeat(
                        "worker-saturated",
                        [saturated_lane],
                        heartbeat_age=1,
                        health_contract_version=2,
                    )
                ],
                binding=binding,
                active_consumer_names=frozenset(
                    {"worker-saturated"}
                ),
            )
        )

    def test_v2_saturation_evidence_boundaries_fail_closed(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        active = frozenset({"worker-saturated"})
        base = {
            "success_age": 61,
            "progress_operation": "capacity_wait",
            "progress_age": 1,
            "inflight_count": 32,
            "inflight_limit": 32,
            "saturated": True,
        }
        mutations = (
            {"progress_operation": "capacity_available"},
            {"progress_age": 45},
            {"inflight_count": 31},
            {"inflight_count": 33},
            {"inflight_limit": 0, "inflight_count": 0},
            {"inflight_limit": 257, "inflight_count": 257},
            {"saturated": False},
            {"failures": 1},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                lane_args = {**base, **mutation}
                row = _repair_heartbeat(
                    "worker-saturated",
                    [_repair_lane("bilibili", **lane_args)],
                    heartbeat_age=1,
                    health_contract_version=2,
                )
                self.assertFalse(
                    worker_rows_support_repair_dispatch(
                        [row],
                        binding=binding,
                        active_consumer_names=active,
                    )
                )

    def test_unknown_health_contract_fails_closed(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        row = _repair_heartbeat(
            "worker-future",
            [_repair_lane("bilibili")],
            health_contract_version=3,
        )
        self.assertFalse(
            worker_rows_support_repair_dispatch(
                [row],
                binding=binding,
                active_consumer_names=frozenset({"worker-future"}),
            )
        )

    def test_exact_lane_requires_active_healthy_consumer_heartbeat(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        capable = _repair_heartbeat(
            "worker-bilibili",
            [_repair_lane("bilibili")],
        )
        active = frozenset({"worker-bilibili"})
        self.assertTrue(
            worker_rows_support_repair_dispatch(
                [capable],
                binding=binding,
                active_consumer_names=active,
            )
        )
        self.assertFalse(
            worker_rows_support_repair_dispatch(
                [capable],
                binding=binding,
                active_consumer_names=frozenset(),
            )
        )
        self.assertFalse(
            worker_rows_support_repair_dispatch(
                [
                    _repair_heartbeat(
                        "worker-bilibili",
                        [
                            _repair_lane(
                                "bilibili",
                                status="degraded",
                                failures=1,
                            )
                        ],
                    )
                ],
                binding=binding,
                active_consumer_names=active,
            )
        )

    async def test_degraded_target_lane_does_not_poison_healthy_sibling(
        self,
    ):
        worker_id = "worker-shared"
        rows = [
            _repair_heartbeat(
                worker_id,
                [
                    _repair_lane(
                        "bilibili",
                        status="degraded",
                        failures=3,
                    ),
                    _repair_lane("weibo"),
                ],
            )
        ]
        bilibili = repair_task_stream_binding_for_platform("bilibili")
        weibo = repair_task_stream_binding_for_platform("weibo")
        lane_redis = _RepairLaneRedis(
            {
                bilibili.stream_key: [
                    {"name": worker_id, "idle": 100}
                ],
                weibo.stream_key: [
                    {"name": worker_id, "idle": 100}
                ],
            }
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase(rows),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )
            self.assertTrue(
                await repair_dispatch_workers_ready("weibo")
            )

    async def test_old_worker_does_not_block_capable_lane_consumer(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        rows = [
            _repair_heartbeat(
                "worker-old",
                [],
                capable=False,
            ),
            _repair_heartbeat(
                "worker-new",
                [_repair_lane("bilibili")],
            ),
        ]
        lane_redis = _RepairLaneRedis(
            {
                binding.stream_key: [
                    {"name": "worker-old", "idle": 10},
                    {"name": "worker-new", "idle": 10},
                ]
            }
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase(rows),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertTrue(
                await repair_dispatch_workers_ready("bilibili")
            )

    async def test_stale_heartbeat_or_consumer_blocks_exact_lane(self):
        binding = repair_task_stream_binding_for_platform("bilibili")
        stale_heartbeat = _repair_heartbeat(
            "worker-stale",
            [_repair_lane("bilibili")],
            heartbeat_age=46,
        )
        lane_redis = _RepairLaneRedis(
            {
                binding.stream_key: [
                    {"name": "worker-stale", "idle": 10}
                ]
            }
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase([stale_heartbeat]),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

        fresh_heartbeat = _repair_heartbeat(
            "worker-stale-consumer",
            [_repair_lane("bilibili")],
        )
        stale_consumer_redis = _RepairLaneRedis(
            {
                binding.stream_key: [
                    {
                        "name": "worker-stale-consumer",
                        "idle": 45_001,
                    }
                ]
            }
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase([fresh_heartbeat]),
        ), patch(
            "app.api.lotteries.redis",
            stale_consumer_redis,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

    async def test_stale_consumer_idle_accepts_only_v2_live_saturation(
        self,
    ):
        binding = repair_task_stream_binding_for_platform("bilibili")
        lane_redis = _RepairLaneRedis(
            {
                binding.stream_key: [
                    {
                        "name": "worker-saturated",
                        "idle": 90_000,
                    }
                ]
            }
        )
        saturated = _repair_heartbeat(
            "worker-saturated",
            [
                _repair_lane(
                    "bilibili",
                    success_age=90,
                    progress_operation="capacity_wait",
                    progress_age=1,
                    inflight_count=32,
                    inflight_limit=32,
                    saturated=True,
                )
            ],
            heartbeat_age=1,
            health_contract_version=2,
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase([saturated]),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertTrue(
                await repair_dispatch_workers_ready("bilibili")
            )

        stale_saturated = _repair_heartbeat(
            "worker-saturated",
            [
                _repair_lane(
                    "bilibili",
                    success_age=90,
                    progress_operation="capacity_wait",
                    progress_age=1,
                    inflight_count=32,
                    inflight_limit=32,
                    saturated=True,
                )
            ],
            heartbeat_age=46,
            health_contract_version=2,
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase([stale_saturated]),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

        v1 = _repair_heartbeat(
            "worker-saturated",
            [_repair_lane("bilibili", success_age=1)],
            heartbeat_age=1,
            health_contract_version=1,
        )
        with patch(
            "app.api.lotteries.database",
            _RepairLaneDatabase([v1]),
        ), patch(
            "app.api.lotteries.redis",
            lane_redis,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

    async def test_lane_health_queries_timeout_fail_closed(self):
        binding = repair_task_stream_binding_for_platform("bilibili")

        async def hang(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch(
            "app.api.lotteries.redis",
            SimpleNamespace(xinfo_consumers=hang),
        ), patch(
            "app.api.lotteries.REPAIR_LANE_HEALTH_QUERY_TIMEOUT_SECONDS",
            0.01,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

        lane_redis = _RepairLaneRedis(
            {
                binding.stream_key: [
                    {"name": "worker-timeout", "idle": 1}
                ]
            }
        )
        with patch(
            "app.api.lotteries.redis",
            lane_redis,
        ), patch(
            "app.api.lotteries.database",
            SimpleNamespace(fetch_all=hang),
        ), patch(
            "app.api.lotteries.REPAIR_LANE_HEALTH_QUERY_TIMEOUT_SECONDS",
            0.01,
        ):
            self.assertFalse(
                await repair_dispatch_workers_ready("bilibili")
            )

    async def test_explicit_non_source_account_is_rejected_and_audited(self):
        lottery, _plan_binding, intent = _contract()
        database = _RepairDatabase(lottery, intent)
        audit = AsyncMock()
        event = AsyncMock()
        repair_plan = {
            "eligible": True,
            "missing_actions": ["commented"],
        }

        with patch(
            "app.api.lotteries.database", database
        ), patch(
            "app.api.lotteries.REPAIR_DISPATCH_INTENT_BINDING_READY",
            True,
        ), patch(
            "app.api.lotteries.repair_dispatch_workers_ready",
            new=AsyncMock(return_value=True),
        ), patch(
            "app.api.lotteries.build_lottery_repair_plan",
            new=AsyncMock(return_value=repair_plan),
        ), patch(
            "app.api.lotteries.require_min_role",
            return_value={"actor_id": "operator-1"},
        ), patch(
            "app.api.lotteries.audit_event",
            new=audit,
        ), patch(
            "app.api.lotteries._record_post_commit_event",
            new=event,
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery_repair(
                    73,
                    DispatchTaskRequest(
                        account_id=10,
                        dry_run=False,
                        confirm=True,
                    ),
                    object(),
                )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["blockers"],
            ["execution_intent_repair_account_mismatch"],
        )
        self.assertEqual(
            audit.await_args.kwargs["detail"],
            {
                "platform": "bilibili",
                "code": "execution_intent_repair_account_mismatch",
                "http_status": 409,
            },
        )
        self.assertNotIn(
            "account_id",
            event.await_args.kwargs["payload"],
        )

    async def test_unavailable_repair_contract_returns_audited_503(self):
        lottery, _plan_binding, intent = _contract()
        database = _RepairDatabase(lottery, intent)
        audit = AsyncMock()
        event = AsyncMock()

        with patch(
            "app.api.lotteries.database", database
        ), patch(
            "app.api.lotteries.REPAIR_DISPATCH_INTENT_BINDING_READY",
            False,
        ), patch(
            "app.api.lotteries.build_lottery_repair_plan",
            new=AsyncMock(return_value={"eligible": True}),
        ), patch(
            "app.api.lotteries.require_min_role",
            return_value={"actor_id": "operator-1"},
        ), patch(
            "app.api.lotteries.audit_event",
            new=audit,
        ), patch(
            "app.api.lotteries._record_post_commit_event",
            new=event,
        ):
            with self.assertRaises(HTTPException) as caught:
                await dispatch_lottery_repair(
                    73,
                    DispatchTaskRequest(dry_run=False, confirm=True),
                    object(),
                )

        self.assertEqual(caught.exception.status_code, 503)
        expected = {
            "platform": "bilibili",
            "code": "repair_intent_binding_unavailable",
            "http_status": 503,
        }
        self.assertEqual(audit.await_args.kwargs["detail"], expected)
        self.assertEqual(event.await_args.kwargs["payload"], expected)

    async def test_queue_keeps_full_plan_and_binds_exact_subset(self):
        lottery, plan_binding, intent = _contract()
        database = _RepairDatabase(lottery, intent)
        enqueue = AsyncMock()
        evidence_revalidation = AsyncMock()
        pick = AsyncMock(
            return_value={
                "id": 9,
                "execution_revision": 7,
            }
        )

        class PlatformModule:
            action_order = ("followed", "liked", "commented", "reposted")
            real_run_supported = True
            real_run_blocker = None
            requires_exact_real_run_evidence = True

            def execution_path_blockers(self, _path):
                return []

            def requires_public_ingress(self, **_context):
                return False

            def account_execution_path_for_dispatch(self, **_context):
                return "bilibili_api_v2"

            def account_required_actions_for_dispatch(self, **context):
                return tuple(context["required_actions"])

            def build_dispatch_plan_binding(self, **_context):
                return dict(plan_binding)

            async def revalidate_exact_execution_evidence(
                self,
                **context,
            ):
                await evidence_revalidation(**context)

        decision = {
            "allowed": True,
            "decision_id": "decision-1",
            "policy_version": "policy-1",
            "blockers": [],
            "failed_gates": [],
            "gate": {"execution_evidence_id": EVIDENCE_ID},
        }
        decision_evaluator = AsyncMock(return_value=decision)
        account_lease = SimpleNamespace(
            lease_id=LEASE_ID,
            generation=3,
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.api.lotteries.database", database)
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.REPAIR_DISPATCH_INTENT_BINDING_READY",
                    True,
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.repair_dispatch_workers_ready",
                    new=AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.require_min_role",
                    return_value={"actor_id": "operator-1"},
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.get_platform_module",
                    return_value=PlatformModule(),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.get_platform",
                    return_value={
                        "label": "Bilibili",
                        "action_adapter": True,
                    },
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.load_runtime_selector_config",
                    new=AsyncMock(return_value={}),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.validate_lottery_target",
                    return_value=SimpleNamespace(valid=True, reason=None),
                )
            )
            stack.enter_context(
                patch("app.api.lotteries.require_confirmation")
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.is_real_run_enabled",
                    new=AsyncMock(return_value=True),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.circuit_breaker_allows",
                    new=AsyncMock(return_value=(True, None)),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.pick_account",
                    new=pick,
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.evaluate_real_run_decision",
                    new=decision_evaluator,
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.load_real_run_completion_authority",
                    new=AsyncMock(
                        return_value=RealRunCompletionAuthority(("liked",))
                    ),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.acquire_account_operation_lease",
                    new=AsyncMock(return_value=account_lease),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.bind_lease_to_task",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.enqueue_outbox",
                    new=enqueue,
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.try_flush_dedup",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.audit_event",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries._record_post_commit_event",
                    new=AsyncMock(),
                )
            )
            stack.enter_context(
                patch(
                    "app.api.lotteries.uuid.uuid4",
                    return_value=uuid.UUID(TASK_ID),
                )
            )

            result = await dispatch_lottery_repair(
                73,
                DispatchTaskRequest(dry_run=False, confirm=True),
                object(),
            )

        task_insert = next(
            values
            for query, values in database.executions
            if "INSERT INTO task_runs" in query
        )
        binding_insert = next(
            values
            for query, values in database.executions
            if "INSERT INTO task_execution_intent_bindings" in query
        )
        message = enqueue.await_args.args[0]
        stream_key = enqueue.await_args.args[1]

        self.assertEqual(
            task_insert["action_plan_hash"],
            intent.full_action_plan_hash,
        )
        self.assertEqual(
            binding_insert["evidence_action_plan_hash"],
            intent.full_action_plan_hash,
        )
        self.assertNotEqual(
            binding_insert["bound_action_plan_hash"],
            intent.full_action_plan_hash,
        )
        self.assertEqual(
            binding_insert["execution_evidence_kind"],
            "exact_execution_evidence",
        )
        self.assertEqual(
            binding_insert["exact_execution_evidence_id"],
            EVIDENCE_ID,
        )
        self.assertIsNone(binding_insert["oauth_calibration_id"])
        self.assertEqual(
            json.loads(message["action_plan"]),
            intent.full_action_plan,
        )
        self.assertEqual(
            message["action_plan_hash"],
            intent.full_action_plan_hash,
        )
        self.assertEqual(
            json.loads(message["requested_actions"]),
            ["commented"],
        )
        self.assertEqual(
            message["requested_action_plan_hash"],
            binding_insert["bound_action_plan_hash"],
        )
        self.assertEqual(
            message["execution_evidence_kind"],
            "exact_execution_evidence",
        )
        self.assertEqual(
            message["exact_execution_evidence_id"],
            EVIDENCE_ID,
        )
        self.assertEqual(message["oauth_calibration_id"], "")
        self.assertEqual(
            stream_key,
            "lottery_repair_tasks:v1:bilibili",
        )
        self.assertEqual(result["requested_actions"], ["commented"])
        self.assertEqual(pick.await_args.args[0], 9)
        self.assertEqual(
            pick.await_args.kwargs["required_actions"],
            ("commented",),
        )
        self.assertEqual(
            decision_evaluator.await_args.kwargs[
                "execution_required_actions"
            ],
            ("commented",),
        )
        evidence_revalidation.assert_awaited_once()
        self.assertEqual(
            evidence_revalidation.await_args.kwargs[
                "execution_required_actions"
            ],
            ("commented",),
        )


if __name__ == "__main__":
    unittest.main()
