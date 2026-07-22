"""Offline contract tests for Douyin manual/Shadow support."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import task_runner  # noqa: E402
from app.account_calibrator import calibrated_account_status, verify_douyin_identity  # noqa: E402
from app.action_plan import (  # noqa: E402
    DOUYIN_ACTION_ORDER,
    DOUYIN_MANUAL_EXECUTION_PATH,
    ActionPlanV2Error,
    compute_action_plan_hash,
    validate_action_plan_v2,
)
from app.adapter_probe import (  # noqa: E402
    build_recommended_config,
    summarize_probe_result,
)
from app.adapters.base import UnsupportedPlatformAction  # noqa: E402
from app.adapters.douyin import DouyinAdapter  # noqa: E402
from app.real_run_gate import (  # noqa: E402
    RealRunGateBlocked,
    enforce_real_run_gate,
)


FOLLOW_HANDLE = "@抽奖作者"


def douyin_plan(*, actions=None, **overrides):
    required_actions = list(actions or DOUYIN_ACTION_ORDER)
    payloads = {
        "followed": {"target_handle": FOLLOW_HANDLE},
        "liked": {},
        "commented": {
            "text": "认真参与抽奖",
            "topic_tags": [],
            "mentions": [],
        },
        "favorited": {},
        "reposted": {
            "text": "转发参与抽奖",
            "topic_tags": [],
            "mentions": [],
        },
    }
    plan = {
        "version": 2,
        "platform": "douyin",
        "rule_snapshot_id": 401,
        "rule_hash": "b" * 64,
        "execution_path_id": DOUYIN_MANUAL_EXECUTION_PATH,
        "required_actions": required_actions,
        "action_payloads": {
            action: copy.deepcopy(payloads[action]) for action in required_actions
        },
        "content_requirements": {
            "follow_targets": (
                [FOLLOW_HANDLE] if "followed" in required_actions else []
            ),
            "commented": {"topic_tags": [], "mentions": []},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "executable": False,
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
            "submit": ["button.send"],
            "done": ["div.comment-sent"],
        },
        "favorited": {
            "click": ["button.favorite"],
            "done": ["button.favorited"],
        },
        "reposted": {
            "click": ["button.share"],
            "confirm": ["button.repost"],
            "done": ["div.reposted"],
        },
    }


class ActionPlanContractTests(unittest.TestCase):
    def assert_plan_code(self, expected: str, plan: dict) -> None:
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan, require_executable=False)
        self.assertEqual(caught.exception.code, expected)

    def test_manual_plan_preserves_favorite_and_repost_independently(self):
        plan = validate_action_plan_v2(
            douyin_plan(),
            require_executable=False,
        )
        self.assertEqual(plan.required_actions, DOUYIN_ACTION_ORDER)
        self.assertEqual(plan.payload_for("favorited"), {})
        self.assertEqual(plan.payload_for("reposted")["text"], "转发参与抽奖")

    def test_reviewed_action_subset_remains_variable(self):
        plan = validate_action_plan_v2(
            douyin_plan(actions=("liked", "commented", "favorited")),
            require_executable=False,
        )
        self.assertEqual(
            plan.required_actions,
            ("liked", "commented", "favorited"),
        )

    def test_plain_repost_without_source_text_is_preserved(self):
        plan = douyin_plan(actions=("reposted",))
        plan["action_payloads"]["reposted"] = {}
        plan["plan_hash"] = compute_action_plan_hash(plan)

        validated = validate_action_plan_v2(plan, require_executable=False)

        self.assertEqual({}, validated.payload_for("reposted"))

    def test_executable_claim_and_wrong_path_are_rejected(self):
        self.assert_plan_code(
            "douyin_manual_plan_must_be_non_executable",
            douyin_plan(executable=True),
        )
        self.assert_plan_code(
            "douyin_execution_path_invalid",
            douyin_plan(execution_path_id="bilibili_api_v2"),
        )


class AdapterCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_selector_config_never_enables_real_actions(self):
        adapter = DouyinAdapter(selector_config=complete_observation_config())
        self.assertFalse(adapter.REAL_ACTIONS)
        self.assertEqual(adapter.STATUS, "manual_only")
        self.assertTrue(adapter.MANUAL_CONFIRMATION_REQUIRED)
        self.assertFalse(adapter.OFFICIAL_INTERACTION_API_AVAILABLE)

    async def test_favorite_and_repost_use_distinct_readbacks_not_share_click(self):
        adapter = DouyinAdapter(selector_config=complete_observation_config())
        self.assertEqual(adapter.SELECTOR_PROBES["favorited"], ["button.favorited"])
        self.assertEqual(adapter.SELECTOR_PROBES["reposted"], ["div.reposted"])
        self.assertNotIn("button.share", adapter.SELECTOR_PROBES["reposted"])
        self.assertNotEqual(
            adapter.SELECTOR_PROBES["favorited"],
            adapter.SELECTOR_PROBES["reposted"],
        )

    async def test_every_interaction_fails_before_touching_page(self):
        adapter = DouyinAdapter(selector_config=complete_observation_config())
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
                    f"douyin_no_official_interaction_api:{action}",
                ):
                    await method(page)

    def test_complete_probe_is_manual_only_not_real_ready(self):
        result = {
            "followed": [{"selector": "button.follow", "visible": True}],
            "liked": [{"selector": "button.like", "visible": True}],
            "commented": [
                {"selector": "textarea.comment", "visible": True},
                {"selector": "button.send", "visible": True},
            ],
            "favorited": [
                {"selector": "button.favorited", "visible": True}
            ],
            "reposted": [{"selector": "div.reposted", "visible": True}],
        }
        summary = summarize_probe_result("douyin", result)
        self.assertTrue(summary["selector_observation_complete"])
        self.assertFalse(summary["ready_for_real_actions"])
        self.assertFalse(summary["real_run_capable"])
        self.assertTrue(summary["manual_confirmation_required"])
        self.assertEqual(
            summary["capability_block_reason"],
            "douyin_no_official_interaction_api",
        )
        recommended = build_recommended_config("douyin", result)["douyin"]
        self.assertEqual(
            recommended["favorited"],
            {"done": ["button.favorited"]},
        )
        self.assertEqual(
            recommended["reposted"],
            {"done": ["div.reposted"]},
        )


class GateOrderingDatabase:
    def __init__(self, setting: str):
        self.setting = setting
        self.fetch_one_calls = 0

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "runtime_settings" in query:
            return {"setting_value": self.setting}
        raise AssertionError("Douyin capability gate queried task/evidence state")

    async def fetch_all(self, query, values=None):
        raise AssertionError("Douyin capability gate queried breakers")

    async def execute(self, query, values=None):
        raise AssertionError("Douyin capability gate attempted a write")


class RealRunGateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def task():
        return {
            "task_id": "douyin-task-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "douyin",
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
            await self.assert_blocked("douyin_no_official_interaction_api", db)
        self.assertEqual(db.fetch_one_calls, 1)


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsigned_private_endpoint_is_not_called_or_claimed_verified(self):
        ctx = type(
            "Context",
            (),
            {"request": type("Request", (), {"get": AsyncMock()})()},
        )()
        result = await verify_douyin_identity(ctx)
        self.assertFalse(result["verified"])
        self.assertEqual(result["method"], "required_cookie_presence_only")
        ctx.request.get.assert_not_awaited()

    def test_unverified_identity_never_auto_promotes_ready(self):
        identity = {"verified": False, "method": "required_cookie_presence_only"}
        self.assertEqual(calibrated_account_status("warming", identity), "warming")
        self.assertEqual(calibrated_account_status("ready", identity), "warming")
        self.assertEqual(calibrated_account_status("cooling", identity), "cooling")

    def test_verified_identity_can_auto_promote_ready(self):
        self.assertEqual(calibrated_account_status("warming", {"verified": True}), "ready")


class PhaseAndShadowTests(unittest.TestCase):
    def test_requested_phases_follow_reviewed_variable_plan(self):
        task = {
            "platform": "douyin",
            "action_plan": douyin_plan(
                actions=("liked", "commented", "favorited")
            ),
        }
        self.assertEqual(
            task_runner.requested_phases(task, require_plan=False),
            ["liked", "commented", "favorited"],
        )

    def test_dry_run_is_rejected_before_claim_or_phase_write(self):
        message = {
            "task_id": "douyin-dry-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "douyin",
            "mode": "dry_run",
            "action_plan": douyin_plan(),
        }
        with self.assertRaisesRegex(
            task_runner.InvalidTaskMessage,
            "douyin_manual_shadow_only",
        ):
            task_runner.validate_task_message(message)

    def test_shadow_binding_requires_manual_plan_and_exact_platform(self):
        plan = douyin_plan()
        lottery = {
            "platform": "douyin",
            "raw_url": "https://www.douyin.com/video/7300000000000000000",
            "canonical_url": "canonical://douyin/video/7300000000000000000",
            "action_plan": plan,
        }
        task_runner.validate_shadow_task_binding(copy.deepcopy(lottery), lottery)

        forged = copy.deepcopy(plan)
        forged["platform"] = "bilibili"
        forged["plan_hash"] = compute_action_plan_hash(forged)
        task = copy.deepcopy(lottery)
        task["action_plan"] = forged
        lottery["action_plan"] = forged
        with self.assertRaisesRegex(
            task_runner.TaskClaimConflict,
            "shadow_task_action_plan_platform_mismatch",
        ):
            task_runner.validate_shadow_task_binding(task, lottery)


class ShadowExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_observes_reviewed_phases_without_clicking(self):
        adapter = DouyinAdapter(selector_config=complete_observation_config())

        class VisibleLocator:
            @property
            def first(self):
                return self

            async def is_visible(self, timeout=None):
                return True

            async def click(self):
                raise AssertionError("Shadow must not click")

        class Page:
            url = "https://www.douyin.com/video/7300000000000000000"

            async def goto(self, *args, **kwargs):
                return None

            async def wait_for_timeout(self, *args, **kwargs):
                return None

            def locator(self, selector):
                return VisibleLocator()

            async def close(self):
                return None

        page = Page()
        context = type(
            "Context",
            (),
            {"new_page": AsyncMock(return_value=page)},
        )()
        pool = type(
            "Pool",
            (),
            {"get_account_context": AsyncMock(return_value=context)},
        )()
        event = AsyncMock(return_value="event-1")
        task = {
            "task_id": "douyin-shadow-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "douyin",
            "mode": "shadow_run",
            "raw_url": page.url,
            "canonical_url": "canonical://douyin/video/7300000000000000000",
            "action_plan": douyin_plan(),
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
                task_runner,
                "install_main_frame_navigation_guard",
                AsyncMock(),
            ),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(
                task_runner,
                "capture_shadow_screenshot",
                AsyncMock(return_value="/evidence/douyin-shadow-1.png"),
            ),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
            patch.object(task_runner, "save_phase", AsyncMock()) as save_phase,
            patch.object(task_runner, "record_event", event),
        ):
            result = await task_runner.execute_shadow_run(task, adapter, pool)

        self.assertEqual(result, "/evidence/douyin-shadow-1.png")
        save_phase.assert_awaited_once_with("douyin-shadow-1", 7, 9, "completed")
        payload = event.await_args.kwargs["payload"]
        self.assertEqual(payload["required_phases"], list(DOUYIN_ACTION_ORDER))
        self.assertTrue(payload["selector_observation_complete"])
        self.assertFalse(payload["qualified"])
        self.assertFalse(payload["real_run_capable"])
        self.assertTrue(payload["manual_confirmation_required"])
        self.assertEqual(
            payload["capability_block_reason"],
            "douyin_no_official_interaction_api",
        )
        self.assertFalse(payload["side_effects"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
