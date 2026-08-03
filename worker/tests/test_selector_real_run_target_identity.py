import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import task_runner


class FakePage:
    def __init__(self, *, final_url=None, wait_url=None):
        self.url = ""
        self.main_frame = object()
        self.final_url = final_url
        self.wait_url = wait_url

    async def route(self, pattern, handler):
        self.route_pattern = pattern
        self.route_handler = handler

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = self.final_url or url

    async def wait_for_timeout(self, milliseconds):
        if self.wait_url:
            self.url = self.wait_url

    async def close(self):
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class FakePool:
    def __init__(self, page):
        self.context = FakeContext(page)

    async def get_account_context(
        self,
        account_id,
        profile_dir,
        proxy=None,
        **_kwargs,
    ):
        return self.context


class FakeAdapter:
    def __init__(self):
        self.follow = AsyncMock()
        self.like = AsyncMock()
        self.comment = AsyncMock()
        self.repost = AsyncMock()
        self._follow = self.follow
        self._like = self.like
        self._comment = self.comment
        self._repost = self.repost


class GuardAwareAdapter:
    def __init__(self):
        self.guard = None
        self.events = []

    def set_mutation_guard(self, guard):
        self.guard = guard

    async def _like(self, page):
        self.events.append("phase")
        await self.guard("liked")

    async def _follow(self, page):
        raise AssertionError("unexpected follow")

    async def _comment(self, page):
        raise AssertionError("unexpected comment")

    async def _repost(self, page):
        raise AssertionError("unexpected repost")


class PreClickCancelledAdapter(GuardAwareAdapter):
    def __init__(self):
        super().__init__()
        self.mutation_started = False

    def reset_mutation_tracking(self):
        self.mutation_started = False

    async def _like(self, page):
        raise asyncio.CancelledError()


class TwoClickGuardAwareAdapter(GuardAwareAdapter):
    def __init__(self):
        super().__init__()
        self.mutation_started = False
        self.first_click_started = False
        self.second_click_started = False

    def reset_mutation_tracking(self):
        self.mutation_started = False

    async def _repost(self, page):
        await self.guard("reposted")
        self.mutation_started = True
        self.first_click_started = True
        await self.guard("repost_confirmed")
        self.second_click_started = True


class SelectorRealRunTargetIdentityTests(unittest.IsolatedAsyncioTestCase):
    def task(self):
        return {
            "task_id": "selector-real-target-1",
            "account_id": "7",
            "lottery_id": "11",
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/PCAGRFqKj",
            "canonical_url": "canonical://weibo/status/PCAGRFqKj",
            "action_plan": {
                "required_actions": ["liked", "commented"],
                "review_required": False,
            },
        }

    async def test_wait_redirect_to_other_post_is_rejected_before_any_action(self):
        page = FakePage(wait_url="https://weibo.com/123456/OtherPost")
        adapter = FakeAdapter()
        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
                await task_runner.execute_real_task(self.task(), adapter, FakePool(page))

        adapter.like.assert_not_awaited()
        adapter.comment.assert_not_awaited()

    async def test_each_action_rechecks_identity_before_mutating(self):
        page = FakePage()
        adapter = FakeAdapter()

        async def save_phase_and_redirect(task_id, account_id, lottery_id, phase):
            if phase == "liked":
                page.url = "https://weibo.com/123456/OtherPost"

        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "enforce_task_real_run_gate", AsyncMock()),
            patch.object(task_runner, "save_phase", AsyncMock(side_effect=save_phase_and_redirect)),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            with self.assertRaisesRegex(ValueError, "content_identity_mismatch"):
                await task_runner.execute_real_task(self.task(), adapter, FakePool(page))

        adapter.like.assert_awaited_once()
        adapter.comment.assert_not_awaited()

    async def test_task_runner_installs_per_click_authoritative_guard(self):
        page = FakePage()
        adapter = GuardAwareAdapter()
        task = self.task()
        task["action_plan"]["required_actions"] = ["liked"]
        gate = AsyncMock()

        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "enforce_task_real_run_gate", gate),
            patch.object(task_runner, "save_phase", AsyncMock()),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            await task_runner.execute_real_task(task, adapter, FakePool(page))

        # Once at phase entry and once immediately before the mutation click.
        self.assertEqual(gate.await_count, 2)
        self.assertEqual(adapter.events, ["phase"])
        self.assertIsNone(adapter.guard)

    async def test_preclick_gate_rejection_does_not_claim_unknown_remote_outcome(self):
        page = FakePage()
        adapter = GuardAwareAdapter()
        task = self.task()
        task["action_plan"]["required_actions"] = ["liked"]
        gate = AsyncMock(
            side_effect=[
                None,
                task_runner.RealRunGateBlocked("circuit_breaker_blocked"),
            ]
        )
        quarantine = AsyncMock()

        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "enforce_task_real_run_gate", gate),
            patch.object(task_runner, "quarantine_external_action_outcome", quarantine),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            with self.assertRaises(task_runner.SelectorMutationPreconditionFailed):
                await task_runner.execute_real_task(task, adapter, FakePool(page))

        quarantine.assert_not_awaited()
        self.assertIsNone(adapter.guard)

    async def test_second_click_gate_rejection_after_first_click_is_quarantined(self):
        page = FakePage()
        adapter = TwoClickGuardAwareAdapter()
        task = self.task()
        task["action_plan"]["required_actions"] = ["reposted"]
        gate = AsyncMock(
            side_effect=[
                None,
                None,
                task_runner.RealRunGateBlocked("circuit_breaker_blocked"),
            ]
        )
        quarantine = AsyncMock()

        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "enforce_task_real_run_gate", gate),
            patch.object(task_runner, "quarantine_external_action_outcome", quarantine),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            with self.assertRaises(task_runner.ExternalActionOutcomeUnknown) as caught:
                await task_runner.execute_real_task(task, adapter, FakePool(page))

        self.assertEqual(caught.exception.action, "reposted")
        self.assertTrue(adapter.first_click_started)
        self.assertFalse(adapter.second_click_started)
        quarantine.assert_awaited_once()
        self.assertEqual(quarantine.await_args.kwargs["action"], "reposted")
        self.assertIsNone(adapter.guard)

    async def test_cancellation_before_click_does_not_quarantine_unknown_outcome(self):
        page = FakePage()
        adapter = PreClickCancelledAdapter()
        task = self.task()
        task["action_plan"]["required_actions"] = ["liked"]
        quarantine = AsyncMock()

        with (
            patch.object(task_runner, "get_latest_phase", AsyncMock(return_value="init")),
            patch.object(task_runner, "prepare_account_login", AsyncMock()),
            patch.object(task_runner, "refresh_task_lease", AsyncMock()),
            patch.object(task_runner, "detect_page_risk", AsyncMock()),
            patch.object(task_runner, "enforce_task_real_run_gate", AsyncMock()),
            patch.object(task_runner, "quarantine_external_action_outcome", quarantine),
            patch.object(task_runner, "capture_failure_screenshot", AsyncMock()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await task_runner.execute_real_task(task, adapter, FakePool(page))

        quarantine.assert_not_awaited()
        self.assertIsNone(adapter.guard)


if __name__ == "__main__":
    unittest.main()
