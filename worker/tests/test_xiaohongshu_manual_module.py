"""Offline contract tests for Xiaohongshu manual/Shadow support."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.action_plan import (  # noqa: E402
    ActionPlanV2Error,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_REQUIRED_ACTIONS,
    compute_action_plan_hash,
    validate_action_plan_v2,
)
from app.adapter_probe import (  # noqa: E402
    build_recommended_config,
    summarize_probe_result,
)
from app.adapters.base import UnsupportedPlatformAction  # noqa: E402
from app.adapters.xiaohongshu import XiaohongshuAdapter  # noqa: E402
from app.real_run_gate import (  # noqa: E402
    RealRunGateBlocked,
    enforce_real_run_gate,
)
from app import task_runner  # noqa: E402


FOLLOW_HANDLE = "@抽奖博主"


def xiaohongshu_plan(**overrides):
    plan = {
        "version": 2,
        "platform": "xiaohongshu",
        "rule_snapshot_id": 301,
        "rule_hash": "a" * 64,
        "execution_path_id": XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        "required_actions": list(XIAOHONGSHU_REQUIRED_ACTIONS),
        "action_payloads": {
            "followed": {"target_handle": FOLLOW_HANDLE},
            "liked": {},
            "commented": {
                "text": "认真参与抽奖",
                "topic_tags": [],
                "mentions": [],
            },
            "favorited": {},
        },
        "content_requirements": {
            "follow_targets": [FOLLOW_HANDLE],
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
        # Prove even a legacy complete mutation config cannot enable real use.
        "reposted": {"click": ["button.share"], "done": ["div.shared"]},
    }


class ActionPlanContractTests(unittest.TestCase):
    def assert_plan_code(self, expected: str, plan: dict) -> None:
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan, require_executable=False)
        self.assertEqual(caught.exception.code, expected)

    def test_manual_plan_expresses_exact_four_action_contract(self):
        plan = xiaohongshu_plan()
        validated = validate_action_plan_v2(plan, require_executable=False)
        self.assertEqual(validated.required_actions, XIAOHONGSHU_REQUIRED_ACTIONS)
        self.assertEqual(
            validated.execution_path_id,
            XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        )
        self.assertEqual(
            validated.content_requirements["reposted"],
            {"topic_tags": [], "mentions": []},
        )

    def test_manual_plan_is_not_accepted_as_executable(self):
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(xiaohongshu_plan())
        self.assertEqual(caught.exception.code, "action_plan_not_executable")

        plan = xiaohongshu_plan(executable=True)
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan, require_executable=False)
        self.assertEqual(
            caught.exception.code,
            "xiaohongshu_no_official_interaction_api",
        )

    def test_missing_favorite_or_repost_substitution_is_rejected(self):
        for actions in (
            ["followed", "liked", "commented"],
            ["followed", "liked", "commented", "reposted"],
        ):
            with self.subTest(actions=actions):
                plan = xiaohongshu_plan()
                plan["required_actions"] = actions
                plan["action_payloads"] = {
                    action: copy.deepcopy(
                        plan["action_payloads"].get(
                            action,
                            {"text": "转发参与"} if action == "reposted" else {},
                        )
                    )
                    for action in actions
                }
                plan["plan_hash"] = compute_action_plan_hash(plan)
                self.assert_plan_code(
                    "xiaohongshu_four_action_plan_required",
                    plan,
                )

    def test_wrong_execution_path_is_rejected(self):
        plan = xiaohongshu_plan(execution_path_id="selector_flow")
        self.assert_plan_code("xiaohongshu_execution_path_invalid", plan)

    def test_other_platforms_cannot_smuggle_favorited(self):
        plan = xiaohongshu_plan(
            platform="weibo",
            execution_path_id="weibo_selector_v1",
        )
        self.assert_plan_code("action_plan_required_actions_invalid", plan)


class AdapterCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_selector_config_never_enables_real_actions(self):
        adapter = XiaohongshuAdapter(selector_config=complete_observation_config())
        self.assertFalse(adapter.REAL_ACTIONS)
        self.assertEqual(adapter.STATUS, "manual_only")
        self.assertTrue(adapter.MANUAL_CONFIRMATION_REQUIRED)
        self.assertFalse(adapter.OFFICIAL_INTERACTION_API_AVAILABLE)
        self.assertEqual(
            set(adapter.ACTIONS),
            {"followed", "liked", "commented", "favorited"},
        )
        self.assertNotIn("reposted", adapter.SELECTOR_PROBES)

    async def test_every_interaction_method_fails_before_touching_page(self):
        adapter = XiaohongshuAdapter(selector_config=complete_observation_config())
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
                    f"xiaohongshu_no_official_interaction_api:{action}",
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
            "favorited": [{"selector": "button.favorite", "visible": True}],
        }
        summary = summarize_probe_result("xiaohongshu", result)
        self.assertEqual(
            summary["required_phases"],
            ["followed", "liked", "commented", "favorited"],
        )
        self.assertTrue(summary["selector_observation_complete"])
        self.assertFalse(summary["ready_for_real_actions"])
        self.assertTrue(summary["manual_confirmation_required"])
        self.assertEqual(
            summary["capability_block_reason"],
            "xiaohongshu_no_official_interaction_api",
        )
        recommended = build_recommended_config("xiaohongshu", result)
        self.assertIn("favorited", recommended["xiaohongshu"])
        self.assertNotIn("reposted", recommended["xiaohongshu"])


class GateOrderingDatabase:
    def __init__(self, setting: str):
        self.setting = setting
        self.fetch_one_calls = 0

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls += 1
        if "runtime_settings" in query:
            return {"setting_value": self.setting}
        raise AssertionError("Xiaohongshu capability gate queried task/evidence state")

    async def fetch_all(self, query, values=None):
        raise AssertionError("Xiaohongshu capability gate queried breakers")

    async def execute(self, query, values=None):
        raise AssertionError("Xiaohongshu capability gate attempted a write")


class RealRunGateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def task():
        return {
            "task_id": "xhs-task-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "xiaohongshu",
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
            await self.assert_blocked(
                "xiaohongshu_no_official_interaction_api",
                db,
            )
        self.assertEqual(db.fetch_one_calls, 1)


class PhaseAndShadowTests(unittest.IsolatedAsyncioTestCase):
    def test_platform_specific_phase_order_does_not_regress_bilibili(self):
        xhs_task = {
            "platform": "xiaohongshu",
            "action_plan": xiaohongshu_plan(),
        }
        self.assertEqual(
            task_runner.requested_phases(xhs_task, require_plan=False),
            ["followed", "liked", "commented", "favorited"],
        )
        self.assertEqual(
            task_runner.requested_phases(
                {
                    "platform": "bilibili",
                    "action_plan": {
                        "required_actions": [
                            "followed",
                            "liked",
                            "commented",
                            "reposted",
                        ]
                    },
                },
                require_plan=False,
            ),
            ["followed", "liked", "commented", "reposted"],
        )

    def test_favorite_dry_run_is_rejected_before_claim_or_phase_write(self):
        for action_plan in (
            xiaohongshu_plan(),
            {"required_actions": ["followed", "liked", "commented"]},
            None,
        ):
            with self.subTest(action_plan=action_plan):
                message = {
                    "task_id": "xhs-dry-1",
                    "account_id": "7",
                    "lottery_id": "9",
                    "platform": "xiaohongshu",
                    "mode": "dry_run",
                }
                if action_plan is not None:
                    message["action_plan"] = action_plan
                with self.assertRaisesRegex(
                    task_runner.InvalidTaskMessage,
                    "xiaohongshu_manual_shadow_only",
                ):
                    task_runner.validate_task_message(message)

    async def test_dry_run_executor_rechecks_before_save_phase(self):
        with patch.object(task_runner, "save_phase", AsyncMock()) as save_phase:
            with self.assertRaisesRegex(
                RuntimeError,
                "xiaohongshu_manual_shadow_only",
            ):
                await task_runner.execute_dry_run(
                    "xhs-dry-1",
                    7,
                    9,
                    ["followed", "liked", "commented", "favorited"],
                    platform="xiaohongshu",
                )
        save_phase.assert_not_awaited()

    def test_shadow_binding_accepts_non_executable_reviewed_plan(self):
        plan = xiaohongshu_plan()
        lottery = {
            "platform": "xiaohongshu",
            "raw_url": "https://www.xiaohongshu.com/explore/abc123",
            "canonical_url": "canonical://xiaohongshu/note/abc123",
            "action_plan": plan,
        }
        task = dict(lottery)
        task["action_plan"] = copy.deepcopy(plan)
        task_runner.validate_shadow_task_binding(task, lottery)

    def test_shadow_binding_rejects_missing_or_forged_plan_platform(self):
        for forged_platform in (None, "bilibili"):
            with self.subTest(forged_platform=forged_platform):
                plan = xiaohongshu_plan()
                if forged_platform is None:
                    plan.pop("platform")
                else:
                    plan["platform"] = forged_platform
                plan["plan_hash"] = compute_action_plan_hash(plan)
                lottery = {
                    "platform": "xiaohongshu",
                    "raw_url": "https://www.xiaohongshu.com/explore/abc123",
                    "canonical_url": "canonical://xiaohongshu/note/abc123",
                    "action_plan": plan,
                }
                task = dict(lottery)
                task["action_plan"] = copy.deepcopy(plan)
                with self.assertRaisesRegex(
                    task_runner.TaskClaimConflict,
                    "shadow_task_action_plan_platform_mismatch",
                ):
                    task_runner.validate_shadow_task_binding(task, lottery)

    async def test_shadow_observes_four_phases_without_clicking(self):
        adapter = XiaohongshuAdapter(selector_config=complete_observation_config())

        class VisibleLocator:
            @property
            def first(self):
                return self

            async def is_visible(self, timeout=None):
                return True

            async def click(self):
                raise AssertionError("Shadow must not click")

        class Page:
            url = "https://www.xiaohongshu.com/explore/abc123"

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
            "task_id": "xhs-shadow-1",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "xiaohongshu",
            "mode": "shadow_run",
            "raw_url": page.url,
            "canonical_url": "canonical://xiaohongshu/note/abc123",
            "action_plan": xiaohongshu_plan(),
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
                AsyncMock(return_value="/evidence/xhs-shadow-1.png"),
            ),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
            patch.object(task_runner, "save_phase", AsyncMock()) as save_phase,
            patch.object(task_runner, "record_event", event),
        ):
            result = await task_runner.execute_shadow_run(task, adapter, pool)

        self.assertEqual(result, "/evidence/xhs-shadow-1.png")
        save_phase.assert_awaited_once_with("xhs-shadow-1", 7, 9, "completed")
        payload = event.await_args.kwargs["payload"]
        self.assertEqual(
            payload["required_phases"],
            ["followed", "liked", "commented", "favorited"],
        )
        self.assertTrue(payload["selector_observation_complete"])
        self.assertFalse(payload["qualified"])
        self.assertFalse(payload["real_run_capable"])
        self.assertTrue(payload["manual_confirmation_required"])
        self.assertFalse(payload["side_effects"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
