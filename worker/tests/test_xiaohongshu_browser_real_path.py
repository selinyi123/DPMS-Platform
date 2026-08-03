from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.action_plan import (  # noqa: E402
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    compute_action_plan_hash,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.adapters.base import UnsupportedPlatformAction  # noqa: E402
from app.adapters.xiaohongshu import XiaohongshuAdapter  # noqa: E402
from app.platform_modules.registry import get_platform_module  # noqa: E402
from app.platform_modules import xiaohongshu as xiaohongshu_module  # noqa: E402
from app.platform_modules.xiaohongshu import (  # noqa: E402
    _xiaohongshu_observation,
    validate_xiaohongshu_real_run_evidence,
)
from app.services.execution_evidence import materialize_for_probe  # noqa: E402
from shared.xiaohongshu_browser_contract import (  # noqa: E402
    XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
    XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
    compute_xiaohongshu_browser_config_hash,
    hash_xiaohongshu_browser_observation,
    validate_xiaohongshu_browser_observation,
)


CANONICAL_URL = "canonical://xiaohongshu/note/0123456789abcdef01234567"
RAW_URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
FOLLOW_HANDLE = "@抽奖博主"
COMMENT_TEXT = " 精确评论文案 "


def selector_config():
    return {
        "followed": {
            "click": ["button.follow"],
            "done": ["button.following"],
        },
        "liked": {
            "click": ["button.like"],
            "done": ["button.liked"],
        },
        "commented": {
            "input": ["textarea.comment"],
            "submit": ["button.send"],
            "done": ["div.comment-sent"],
        },
        "favorited": {
            "click": ["button.favorite"],
            "done": ["button.favorited"],
        },
    }


def browser_plan(*, required_actions=None):
    actions = tuple(
        required_actions
        if required_actions is not None
        else ("followed", "liked", "commented", "favorited")
    )
    rule_text = "关注、点赞、收藏并精确评论后参与抽奖"
    payloads = {
        "followed": {"target_handle": FOLLOW_HANDLE},
        "liked": {},
        "commented": {
            "text": COMMENT_TEXT,
            "topic_tags": [],
            "mentions": [],
        },
        "favorited": {},
    }
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "rule_snapshot_id": 301,
        "rule_hash": compute_rule_hash(rule_text),
        "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        "required_actions": list(actions),
        "action_payloads": {action: payloads[action] for action in actions},
        "content_requirements": {
            "follow_targets": [FOLLOW_HANDLE] if "followed" in actions else [],
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return rule_text, plan


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class EvidenceMaterializationDatabase:
    def __init__(self, *, probe, shadow, authority):
        self.probe = probe
        self.shadow = shadow
        self.authority = authority
        self.inserted = None
        self.persisted_query = None

    def transaction(self):
        return FakeTransaction()

    async def fetch_one(self, query, values=None):
        normalized = " ".join(query.lower().split())
        if "from lotteries l" in normalized:
            return dict(self.authority)
        if "from execution_evidence_bindings e" in normalized:
            self.persisted_query = normalized
            if self.inserted is None:
                return None
            return {
                **self.inserted,
                "status": "verified",
                "verified_at": "2026-07-31T00:02:00Z",
                "expires_at": "2026-08-01T00:00:00Z",
                "expiry_bounded": 1,
            }
        if "from adapter_calibrations ac" in normalized:
            return dict(self.probe)
        if "from task_runs tr" in normalized:
            return dict(self.shadow)
        raise AssertionError(normalized)

    async def execute(self, query, values=None):
        normalized = " ".join(query.lower().split())
        if "insert into execution_evidence_bindings" not in normalized:
            raise AssertionError(normalized)
        self.inserted = dict(values or {})
        return 1


class ShadowObservationDatabase:
    def __init__(self, current):
        self.current = current
        self.persisted = None

    def transaction(self):
        return FakeTransaction()

    async def fetch_one(self, query, values=None):
        normalized = " ".join(query.lower().split())
        if "select tr.task_id" in normalized:
            return dict(self.current)
        if "select preflight_observation" in normalized:
            return dict(self.persisted or {})
        raise AssertionError(normalized)

    async def execute(self, query, values=None):
        normalized = " ".join(query.lower().split())
        if "update task_runs" not in normalized:
            raise AssertionError(normalized)
        self.persisted = {
            "preflight_observation": values["observation"],
            "preflight_observation_kind": values["kind"],
            "preflight_observation_hash": values["observation_hash"],
        }
        return 1


class XiaohongshuBrowserContractTests(unittest.TestCase):
    def test_modes_default_and_manual_compatibility_are_explicit(self):
        module = get_platform_module("xiaohongshu")
        self.assertEqual(
            module.default_execution_path,
            XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        )
        browser = module.execution_path(XIAOHONGSHU_BROWSER_EXECUTION_PATH)
        self.assertEqual(
            browser.supported_modes,
            frozenset({"dry_run", "shadow_run", "real_run"}),
        )
        manual = module.execution_path("xiaohongshu_manual_v1")
        self.assertEqual(manual.supported_modes, frozenset({"shadow_run"}))
        self.assertEqual(
            browser.execution_evidence_kind,
            "exact_execution_evidence",
        )

    def test_ordered_action_subsets_and_exact_comment_text_are_bound(self):
        for actions in (
            ("liked",),
            ("liked", "commented", "favorited"),
            ("followed", "liked", "commented", "favorited"),
        ):
            with self.subTest(actions=actions):
                _rule, raw = browser_plan(required_actions=actions)
                plan = validate_action_plan_v2(raw)
                self.assertEqual(plan.required_actions, actions)

        adapter = XiaohongshuAdapter(selector_config=selector_config())
        adapter.bind_reviewed_comment_text(COMMENT_TEXT)
        self.assertEqual(
            adapter._comment_text(adapter.configured_selectors["commented"]),
            COMMENT_TEXT,
        )
        adapter.configured_selectors["commented"]["text"] = "伪造文案"
        with self.assertRaisesRegex(
            UnsupportedPlatformAction,
            "xiaohongshu_comment_text_binding_mismatch",
        ):
            adapter._comment_text(adapter.configured_selectors["commented"])

    def test_config_hash_is_shared_and_selector_completeness_is_fail_closed(self):
        config = selector_config()
        expected = compute_xiaohongshu_browser_config_hash(4, config)
        self.assertEqual(len(expected), 64)
        adapter = XiaohongshuAdapter(selector_config=config)
        self.assertTrue(adapter.supports_actions(adapter.ACTIONS))
        missing = selector_config()
        missing["commented"].pop("done")
        self.assertFalse(
            XiaohongshuAdapter(selector_config=missing).supports_actions(
                ("commented",)
            )
        )


class XiaohongshuOfflineEvidenceChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_and_shadow_handlers_emit_shared_schema(self):
        _rule_text, raw_plan = browser_plan()
        plan = validate_action_plan_v2(raw_plan)
        config = selector_config()
        target_hash = compute_target_hash(CANONICAL_URL)
        config_hash = compute_xiaohongshu_browser_config_hash(4, config)
        phase_status = {
            action: {"ready": True} for action in plan.required_actions
        }

        class ProbeObservation:
            def __init__(self, **values):
                self.__dict__.update(values)

        probe_runtime = SimpleNamespace(ProbeObservation=ProbeObservation)
        generic = SimpleNamespace(
            result={"_summary": {"phase_status": phase_status}}
        )
        binding = {
            "probe_id": "probe-handler-1",
            "platform": "xiaohongshu",
            "lottery_id": 7,
            "account_id": 9,
            "execution_revision": 4,
            "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            "target_hash": target_hash,
            "config_hash": config_hash,
            "action_plan": raw_plan,
            "selector_config": config,
        }
        with patch.object(
            xiaohongshu_module,
            "execute_browser_observation_probe",
            AsyncMock(return_value=generic),
        ):
            probe_result = (
                await xiaohongshu_module.execute_xiaohongshu_browser_probe(
                    binding,
                    pool=object(),
                    runtime=probe_runtime,
                )
            )
        validated_probe = validate_xiaohongshu_browser_observation(
            probe_result.result,
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            ),
            expected_evidence_id="probe-handler-1",
            expected_lottery_id=7,
            expected_account_id=9,
            expected_execution_revision=4,
            expected_target_hash=target_hash,
            expected_rule_snapshot_id=plan.rule_snapshot_id,
            expected_rule_hash=plan.rule_hash,
            expected_action_plan_hash=plan.plan_hash,
            expected_config_hash=config_hash,
            expected_actions=plan.required_actions,
            expected_follow_target_handle=FOLLOW_HANDLE,
            expected_comment_text_hash=probe_result.result[
                "comment_text_hash"
            ],
        )
        self.assertEqual(
            validated_probe.observation_hash,
            probe_result.observation_hash,
        )
        self.assertTrue(probe_result.materialize_execution_evidence)

        current = {
            "task_id": "shadow-handler-1",
            "status": "running",
            "worker_id": "worker-test",
            "account_id": 9,
            "lottery_id": 7,
            "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            "rule_snapshot_id": plan.rule_snapshot_id,
            "target_hash": target_hash,
            "rule_hash": plan.rule_hash,
            "action_plan_hash": plan.plan_hash,
            "config_hash": config_hash,
            "reconciliation_required": 0,
            "account_platform": "xiaohongshu",
            "execution_revision": 4,
            "operation_kind": "shadow_run",
            "owner_id": "shadow-handler-1",
            "lease_task_id": "shadow-handler-1",
            "lease_active": 1,
            "lease_unreleased": 1,
            "lease_latest_generation": 1,
            "active_account_lease_count": 1,
        }
        db = ShadowObservationDatabase(current)
        record_event = AsyncMock(return_value="event-xhs-shadow")
        shadow_runtime = SimpleNamespace(
            database=db,
            WORKER_ID="worker-test",
            TaskOwnershipLost=RuntimeError,
            validate_action_plan_v2=validate_action_plan_v2,
            row_get=lambda row, key, default=None: (
                row.get(key, default) if row else default
            ),
            canonical_json_bytes=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            parse_json_field=lambda value: (
                json.loads(value) if isinstance(value, str) else value
            ),
            record_event=record_event,
        )
        adapter = XiaohongshuAdapter(selector_config=config)

        async def generic_shadow(*_args, **_kwargs):
            adapter._last_shadow_observation_context = {
                "capability_checks": {
                    action: True for action in plan.required_actions
                },
                "account_authenticated": True,
                "target_identity_verified": True,
                "selector_observation_complete": True,
            }
            return "/evidence/shadow-handler-1.png"

        task = {
            "task_id": "shadow-handler-1",
            "platform": "xiaohongshu",
            "account_id": "9",
            "lottery_id": "7",
            "canonical_url": CANONICAL_URL,
            "target_hash": target_hash,
            "config_hash": config_hash,
            "action_plan": raw_plan,
        }
        with patch.object(
            xiaohongshu_module,
            "execute_browser_observation_shadow",
            AsyncMock(side_effect=generic_shadow),
        ):
            screenshot = await (
                xiaohongshu_module.execute_xiaohongshu_browser_shadow_task(
                    task,
                    adapter,
                    pool=object(),
                    runtime=shadow_runtime,
                )
            )
        self.assertEqual(screenshot, "/evidence/shadow-handler-1.png")
        persisted_observation = json.loads(
            db.persisted["preflight_observation"]
        )
        validated_shadow = validate_xiaohongshu_browser_observation(
            persisted_observation,
            expected_observation_kind=(
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            expected_evidence_id="shadow-handler-1",
            expected_lottery_id=7,
            expected_account_id=9,
            expected_execution_revision=4,
            expected_target_hash=target_hash,
            expected_rule_snapshot_id=plan.rule_snapshot_id,
            expected_rule_hash=plan.rule_hash,
            expected_action_plan_hash=plan.plan_hash,
            expected_config_hash=config_hash,
            expected_actions=plan.required_actions,
            expected_follow_target_handle=FOLLOW_HANDLE,
            expected_comment_text_hash=persisted_observation[
                "comment_text_hash"
            ],
        )
        self.assertEqual(
            validated_shadow.observation_hash,
            db.persisted["preflight_observation_hash"],
        )

    async def test_probe_shadow_materialize_then_real_gate_accepts(self):
        rule_text, raw_plan = browser_plan()
        plan = validate_action_plan_v2(raw_plan)
        config = selector_config()
        execution_revision = 4
        config_hash = compute_xiaohongshu_browser_config_hash(
            execution_revision, config
        )
        target_hash = compute_target_hash(CANONICAL_URL)
        checks = {action: True for action in plan.required_actions}
        probe_observation = _xiaohongshu_observation(
            evidence_id="probe-xhs-1",
            observation_kind=XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
            lottery_id=7,
            account_id=9,
            execution_revision=execution_revision,
            target_hash=target_hash,
            plan=plan,
            config_hash=config_hash,
            capability_checks=checks,
        )
        shadow_observation = _xiaohongshu_observation(
            evidence_id="shadow-xhs-1",
            observation_kind=XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
            lottery_id=7,
            account_id=9,
            execution_revision=execution_revision,
            target_hash=target_hash,
            plan=plan,
            config_hash=config_hash,
            capability_checks=checks,
        )
        probe_hash = hash_xiaohongshu_browser_observation(probe_observation)
        shadow_hash = hash_xiaohongshu_browser_observation(shadow_observation)
        contract = {
            "lottery_id": 7,
            "account_id": 9,
            "platform": "xiaohongshu",
            "rule_snapshot_id": plan.rule_snapshot_id,
            "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            "target_hash": target_hash,
            "rule_hash": plan.rule_hash,
            "action_plan_hash": plan.plan_hash,
            "config_hash": config_hash,
        }
        terminal = {
            "status": "succeeded",
            "finished_at": "2026-07-31T00:01:00Z",
            "source_fresh": 1,
            "source_lease_released": 1,
            "source_lease_covers_observation": 1,
        }
        probe_row = {
            **contract,
            **terminal,
            "probe_id": "probe-xhs-1",
            "result": json.dumps(probe_observation, ensure_ascii=False),
            "observation_kind": XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
            "observation_hash": probe_hash,
        }
        shadow_row = {
            **contract,
            **terminal,
            "task_id": "shadow-xhs-1",
            "task_mode": "shadow_run",
            "preflight_observation": json.dumps(
                shadow_observation, ensure_ascii=False
            ),
            "preflight_observation_kind": (
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            "preflight_observation_hash": shadow_hash,
        }
        authority = {
            "lottery_id": 7,
            "platform": "xiaohongshu",
            "raw_url": RAW_URL,
            "canonical_url": CANONICAL_URL,
            "lottery_rule_text": rule_text,
            "action_plan": raw_plan,
            "rule_snapshot_id": plan.rule_snapshot_id,
            "rule_hash": plan.rule_hash,
            "action_plan_hash": plan.plan_hash,
            "snapshot_rule_text": rule_text,
            "is_complete": 1,
            "attested_by": "operator-1",
            "attested_at": "2026-07-31T00:00:00Z",
            "account_platform": "xiaohongshu",
            "account_status": "ready",
            "execution_revision": execution_revision,
            "selector_config_json": json.dumps(config, ensure_ascii=False),
        }
        db = EvidenceMaterializationDatabase(
            probe=probe_row,
            shadow=shadow_row,
            authority=authority,
        )
        evidence_id = await materialize_for_probe(
            db=db, probe_id="probe-xhs-1"
        )
        self.assertTrue(evidence_id)
        self.assertEqual(
            db.inserted["probe_observation_kind"],
            XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND,
        )
        self.assertEqual(
            db.inserted["shadow_observation_kind"],
            XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND,
        )
        self.assertIn("select e.id, e.lottery_id, e.account_id", db.persisted_query)

        gate_row = {
            "account_execution_revision": execution_revision,
            "lottery_canonical_url": CANONICAL_URL,
            "task_execution_evidence_id": evidence_id,
            "task_target_hash": target_hash,
            "task_config_hash": config_hash,
            "evidence_id": evidence_id,
            "evidence_lottery_id": 7,
            "evidence_account_id": 9,
            "evidence_platform": "xiaohongshu",
            "evidence_rule_snapshot_id": plan.rule_snapshot_id,
            "evidence_execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            "evidence_target_hash": target_hash,
            "evidence_rule_hash": plan.rule_hash,
            "evidence_action_plan_hash": plan.plan_hash,
            "evidence_config_hash": config_hash,
            "evidence_probe_id": "probe-xhs-1",
            "evidence_shadow_task_id": "shadow-xhs-1",
            "evidence_probe_observation_kind": (
                XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            ),
            "evidence_probe_observation_hash": probe_hash,
            "evidence_shadow_observation_kind": (
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            "evidence_shadow_observation_hash": shadow_hash,
            "evidence_status": "verified",
            "evidence_verified_at": "2026-07-31T00:02:00Z",
            "evidence_expires_at": "2026-08-01T00:00:00Z",
            "evidence_active": 1,
            "evidence_time_bounded": 1,
            "evidence_probe_status": "succeeded",
            "evidence_probe_observation": probe_observation,
            "source_probe_observation_kind": (
                XIAOHONGSHU_BROWSER_PROBE_OBSERVATION_KIND
            ),
            "source_probe_observation_hash": probe_hash,
            "evidence_probe_finished_at": "2026-07-31T00:01:00Z",
            "evidence_probe_fresh": 1,
            "evidence_probe_lease_released": 1,
            "evidence_probe_lease_covers_observation": 1,
            "evidence_shadow_status": "succeeded",
            "evidence_shadow_task_mode": "shadow_run",
            "evidence_shadow_observation": shadow_observation,
            "source_shadow_observation_kind": (
                XIAOHONGSHU_BROWSER_SHADOW_OBSERVATION_KIND
            ),
            "source_shadow_observation_hash": shadow_hash,
            "evidence_shadow_finished_at": "2026-07-31T00:01:00Z",
            "evidence_shadow_fresh": 1,
            "evidence_shadow_lease_released": 1,
            "evidence_shadow_lease_covers_observation": 1,
            "evidence_shadow_target_hash": target_hash,
            "evidence_shadow_config_hash": config_hash,
        }
        task = {
            "execution_evidence_id": evidence_id,
            "target_hash": target_hash,
            "config_hash": config_hash,
            "selector_config": config,
        }
        binding = validate_xiaohongshu_real_run_evidence(
            task=task,
            row=gate_row,
            account_id=9,
            lottery_id=7,
            platform="xiaohongshu",
            plan=plan,
            execution_plan=plan,
        )
        self.assertEqual(binding.evidence_id, evidence_id)
        self.assertEqual(binding.execution_revision, execution_revision)


if __name__ == "__main__":
    unittest.main(verbosity=2)
