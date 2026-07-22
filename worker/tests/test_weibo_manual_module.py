"""Offline contract tests for Weibo OAuth and manual-observation paths."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
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

from app import task_runner  # noqa: E402
from app.account_calibrator import (  # noqa: E402
    calibrated_account_status,
    verify_weibo_identity,
)
from app.action_plan import (  # noqa: E402
    WEIBO_ACTION_ORDER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    ActionPlanV2Error,
    compute_action_plan_hash,
    validate_action_plan_v2,
    weibo_runtime_capability_requirements,
)
from app.adapter_probe import summarize_probe_result  # noqa: E402
from app.adapters.base import UnsupportedPlatformAction  # noqa: E402
from app.adapters.weibo import WeiboAdapter  # noqa: E402
from app.real_run_gate import RealRunGateBlocked, enforce_real_run_gate  # noqa: E402


FOLLOW_HANDLE = "@lottery_host"
FRIEND_MENTIONS = ["@friend_a", "@friend_b"]


def weibo_plan(*, actions=None, manual: bool = True, **overrides):
    required_actions = list(actions or WEIBO_ACTION_ORDER)
    payloads = {
        "followed": {"target_handle": FOLLOW_HANDLE},
        "liked": {},
        "commented": {
            "text": "#抽奖# @friend_a @friend_b 参与",
            "topic_tags": ["#抽奖#"],
            "mentions": FRIEND_MENTIONS,
        },
        "favorited": {},
        "reposted": {},
    }
    execution_path = (
        WEIBO_MANUAL_EXECUTION_PATH if manual else WEIBO_OAUTH_EXECUTION_PATH
    )
    plan = {
        "version": 2,
        "platform": "weibo",
        "rule_snapshot_id": 501,
        "rule_hash": "c" * 64,
        "execution_path_id": execution_path,
        "required_actions": required_actions,
        "action_payloads": {
            action: copy.deepcopy(payloads[action]) for action in required_actions
        },
        "content_requirements": {
            "follow_targets": (
                [FOLLOW_HANDLE] if "followed" in required_actions else []
            ),
            "commented": {
                "topic_tags": ["#抽奖#"] if "commented" in required_actions else [],
                "mentions": FRIEND_MENTIONS if "commented" in required_actions else [],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": (
                [FOLLOW_HANDLE] if "followed" in required_actions else []
            ),
            "commented": {
                "topic_tags": ["#抽奖#"] if "commented" in required_actions else [],
                "mentions": [],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": (
            {"commented": {"mode": "exact", "count": 2}}
            if "commented" in required_actions
            else {}
        ),
        "runtime_capability_requirements": (
            {} if manual else weibo_runtime_capability_requirements(required_actions)
        ),
        "executable": not manual,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    plan.update(overrides)
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def complete_observation_config():
    return {
        "followed": {"click": ["button.follow"], "done": ["button.following"]},
        "liked": {"click": ["button.like"], "done": ["button.liked"]},
        "commented": {
            "input": ["textarea.comment"],
            "submit": ["button.publish"],
            "done": ["div.comment-sent"],
        },
        "favorited": {
            "click": ["button.favorite"],
            "done": ["button.favorited"],
        },
        "reposted": {"click": ["button.repost"], "done": ["div.repost-sent"]},
    }


class ActionPlanContractTests(unittest.TestCase):
    def assert_plan_code(self, expected: str, plan: dict) -> None:
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan, require_executable=False)
        self.assertEqual(caught.exception.code, expected)

    def test_oauth_plan_binds_variable_actions_capabilities_and_friend_mentions(self):
        plan = weibo_plan(actions=("liked", "commented"), manual=False)
        validated = validate_action_plan_v2(plan)
        self.assertEqual(validated.required_actions, ("liked", "commented"))
        self.assertEqual(validated.payload_for("commented")["mentions"], FRIEND_MENTIONS)
        self.assertEqual(
            validated.runtime_capability_requirements,
            weibo_runtime_capability_requirements(("liked", "commented")),
        )

    def test_manual_plan_is_non_executable_and_has_no_oauth_claim(self):
        validated = validate_action_plan_v2(
            weibo_plan(actions=("reposted",)), require_executable=False
        )
        self.assertEqual(validated.execution_path_id, WEIBO_MANUAL_EXECUTION_PATH)
        self.assertEqual(validated.payload_for("reposted"), {})
        self.assertEqual(validated.runtime_capability_requirements, {})

    def test_manual_executable_wrong_path_and_forged_capability_are_rejected(self):
        self.assert_plan_code(
            "weibo_manual_plan_must_be_non_executable",
            weibo_plan(executable=True),
        )
        self.assert_plan_code(
            "weibo_execution_path_invalid",
            weibo_plan(execution_path_id="weibo_selector_v1"),
        )
        self.assert_plan_code(
            "weibo_oauth_capability_contract_mismatch",
            weibo_plan(manual=False, runtime_capability_requirements={}),
        )


class AdapterCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_selector_adapter_is_observation_only_not_api_capability(self):
        adapter = WeiboAdapter(selector_config=complete_observation_config())
        self.assertFalse(adapter.REAL_ACTIONS)
        self.assertEqual(adapter.STATUS, "oauth_capability_required")
        self.assertTrue(adapter.MANUAL_CONFIRMATION_REQUIRED)
        self.assertTrue(adapter.OFFICIAL_INTERACTION_API_AVAILABLE)
        self.assertEqual(tuple(adapter.ACTIONS), WEIBO_ACTION_ORDER)

    async def test_every_browser_interaction_fails_before_touching_page(self):
        adapter = WeiboAdapter(selector_config=complete_observation_config())
        page = object()
        for action, method in (
            ("followed", adapter._follow),
            ("liked", adapter._like),
            ("commented", adapter._comment),
            ("favorited", adapter._favorite),
            ("reposted", adapter._repost),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    UnsupportedPlatformAction,
                    f"weibo_selector_mutations_disabled:{action}",
                ):
                    await method(page)

    def test_complete_probe_is_observation_only_not_real_ready(self):
        result = {
            "followed": [{"selector": "button.follow", "visible": True}],
            "liked": [{"selector": "button.like", "visible": True}],
            "commented": [
                {"selector": "textarea.comment", "visible": True},
                {"selector": "button.publish", "visible": True},
            ],
            "favorited": [{"selector": "button.favorite", "visible": True}],
            "reposted": [{"selector": "button.repost", "visible": True}],
        }
        summary = summarize_probe_result("weibo", result)
        self.assertTrue(summary["selector_observation_complete"])
        self.assertFalse(summary["ready_for_real_actions"])
        self.assertFalse(summary["real_run_capable"])
        self.assertTrue(summary["manual_confirmation_required"])
        self.assertEqual(
            summary["capability_block_reason"], "weibo_selector_observation_only"
        )


class GateOrderingDatabase:
    def __init__(self, setting: str):
        self.setting = setting
        self.fetch_one_calls = 0

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "runtime_settings" in query:
            return {"setting_value": self.setting}
        raise AssertionError("capability blocker must run before task/evidence queries")

    async def fetch_all(self, query, values=None):
        raise AssertionError("capability blocker must run before breaker queries")

    async def execute(self, query, values=None):
        raise AssertionError("capability gate attempted a write")


class RealRunGateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def task():
        return {
            "task_id": "weibo-task-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "weibo",
        }

    async def assert_blocked(self, expected: str, db: GateOrderingDatabase):
        with self.assertRaises(RealRunGateBlocked) as caught:
            await enforce_real_run_gate(self.task(), db=db, worker_id="worker-test")
        self.assertEqual(caught.exception.code, expected)

    async def test_process_then_db_then_capability_gate_order(self):
        db = GateOrderingDatabase("true")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REAL_RUN_ENABLED", None)
            await self.assert_blocked("process_real_run_disabled", db)
        self.assertEqual(db.fetch_one_calls, 0)

        db = GateOrderingDatabase("false")
        with patch.dict(os.environ, {"REAL_RUN_ENABLED": "true"}, clear=False):
            await self.assert_blocked("real_run_disabled", db)
        self.assertEqual(db.fetch_one_calls, 1)

        db = GateOrderingDatabase("true")
        with patch.dict(os.environ, {"REAL_RUN_ENABLED": "true"}, clear=False):
            await self.assert_blocked("weibo_oauth_capability_evidence_required", db)
        self.assertEqual(db.fetch_one_calls, 1)


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_path_does_not_call_private_web_identity_endpoint(self):
        ctx = type(
            "Context",
            (),
            {"request": type("Request", (), {"get": AsyncMock()})()},
        )()
        result = await verify_weibo_identity(ctx)
        self.assertFalse(result["verified"])
        self.assertEqual(result["method"], "required_cookie_presence_only")
        ctx.request.get.assert_not_awaited()

    def test_session_only_identity_never_auto_promotes_ready(self):
        identity = {"verified": False, "method": "required_cookie_presence_only"}
        self.assertEqual(calibrated_account_status("warming", identity), "warming")
        self.assertEqual(calibrated_account_status("ready", identity), "warming")
        self.assertEqual(calibrated_account_status("cooling", identity), "cooling")


class PhaseAndShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_requested_phases_follow_reviewed_variable_plan(self):
        task = {
            "platform": "weibo",
            "action_plan": weibo_plan(actions=("liked", "commented")),
        }
        self.assertEqual(
            task_runner.requested_phases(task, require_plan=False),
            ["liked", "commented"],
        )

    def test_dry_run_is_rejected_before_claim(self):
        message = {
            "task_id": "weibo-dry-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "weibo",
            "mode": "dry_run",
            "action_plan": weibo_plan(),
        }
        with self.assertRaisesRegex(
            task_runner.InvalidTaskMessage, "weibo_manual_shadow_only"
        ):
            task_runner.validate_task_message(message)

    async def test_manual_dry_run_executor_rechecks_before_phase_write(self):
        with patch.object(
            task_runner, "save_phase", AsyncMock()
        ) as save_phase:
            with self.assertRaisesRegex(
                RuntimeError,
                "weibo_manual_shadow_only",
            ):
                await task_runner.execute_dry_run(
                    "weibo-manual-dry-1",
                    7,
                    9,
                    list(WEIBO_ACTION_ORDER),
                    platform="weibo",
                    action_plan=weibo_plan(),
                )
        save_phase.assert_not_awaited()

    async def test_oauth_dry_run_succeeds_without_http_or_capability_evidence(self):
        plan = weibo_plan(
            actions=("liked", "favorited"),
            manual=False,
        )
        message = {
            "task_id": "weibo-oauth-dry-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "weibo",
            "mode": "dry_run",
            "action_plan": plan,
        }
        self.assertIs(task_runner.validate_task_message(message), message)
        with patch.object(
            task_runner, "save_phase", AsyncMock()
        ) as save_phase, patch.object(
            task_runner, "WeiboApiClient"
        ) as api_client, patch.object(
            task_runner, "record_event", AsyncMock()
        ) as record_event:
            await task_runner.execute_dry_run(
                message["task_id"],
                int(message["account_id"]),
                int(message["lottery_id"]),
                ["liked", "favorited"],
                platform="weibo",
                action_plan=plan,
            )
        api_client.assert_not_called()
        record_event.assert_not_awaited()
        self.assertEqual(
            [call.args[-1] for call in save_phase.await_args_list],
            ["liked", "favorited", "completed"],
        )

    def test_shadow_binding_requires_exact_reviewed_manual_plan(self):
        plan = weibo_plan()
        lottery = {
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/PCAGRFqKj",
            "canonical_url": "canonical://weibo/status/PCAGRFqKj",
            "action_plan": plan,
        }
        task_runner.validate_shadow_task_binding(copy.deepcopy(lottery), lottery)

        forged = copy.deepcopy(plan)
        forged["platform"] = "bilibili"
        forged["plan_hash"] = compute_action_plan_hash(forged)
        forged_lottery = {**lottery, "action_plan": forged}
        with self.assertRaisesRegex(
            task_runner.TaskClaimConflict,
            "shadow_task_action_plan_platform_mismatch",
        ):
            task_runner.validate_shadow_task_binding(
                copy.deepcopy(forged_lottery), forged_lottery
            )

    async def test_shadow_observes_without_clicking_or_claiming_api_capability(self):
        adapter = WeiboAdapter(selector_config=complete_observation_config())

        class VisibleLocator:
            @property
            def first(self):
                return self

            async def is_visible(self, timeout=None):
                return True

            async def click(self):
                raise AssertionError("Shadow must not click")

        class Page:
            url = "https://weibo.com/123456/PCAGRFqKj"

            async def goto(self, *args, **kwargs):
                return None

            async def wait_for_timeout(self, *args, **kwargs):
                return None

            def locator(self, selector):
                return VisibleLocator()

            async def close(self):
                return None

        page = Page()
        context = type("Context", (), {"new_page": AsyncMock(return_value=page)})()
        pool = type(
            "Pool", (), {"get_account_context": AsyncMock(return_value=context)}
        )()
        event = AsyncMock(return_value="event-1")
        task = {
            "task_id": "weibo-shadow-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "weibo",
            "mode": "shadow_run",
            "raw_url": page.url,
            "canonical_url": "canonical://weibo/status/PCAGRFqKj",
            "action_plan": weibo_plan(),
        }
        with (
            patch.object(
                task_runner,
                "validated_platform_canonical_uri",
                return_value=task["canonical_url"],
            ),
            patch.object(
                task_runner,
                "validated_platform_navigation_url",
                return_value=page.url,
            ),
            patch.object(task_runner, "validated_platform_content_url"),
            patch.object(
                task_runner, "install_main_frame_navigation_guard", AsyncMock()
            ),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(
                task_runner,
                "capture_shadow_screenshot",
                AsyncMock(return_value="/evidence/weibo-shadow-1.png"),
            ),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
            patch.object(task_runner, "save_phase", AsyncMock()) as save_phase,
            patch.object(task_runner, "record_event", event),
        ):
            result = await task_runner.execute_shadow_run(task, adapter, pool)

        self.assertEqual(result, "/evidence/weibo-shadow-1.png")
        save_phase.assert_awaited_once_with("weibo-shadow-1", 7, 9, "completed")
        payload = event.await_args.kwargs["payload"]
        self.assertEqual(payload["required_phases"], list(WEIBO_ACTION_ORDER))
        self.assertTrue(payload["selector_observation_complete"])
        self.assertFalse(payload["qualified"])
        self.assertFalse(payload["real_run_capable"])
        self.assertTrue(payload["manual_confirmation_required"])
        self.assertEqual(
            payload["capability_block_reason"], "weibo_selector_observation_only"
        )
        self.assertFalse(payload["side_effects"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
