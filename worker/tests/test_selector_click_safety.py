import unittest
from unittest.mock import AsyncMock, patch

from app.adapters.base import UnsupportedPlatformAction
from app.adapters.selector_flow import (
    SelectorReadbackIndeterminate,
    SelectorFlowAdapter,
    click_first_visible,
    verify_done_state,
)


class FakeLocator:
    def __init__(self, selector, calls, *, visible=True, visibility_error=False, click_error=False):
        self.selector = selector
        self.calls = calls
        self.visible = visible
        self.visibility_error = visibility_error
        self.click_error = click_error
        self.first = self

    async def is_visible(self, timeout=None):
        self.calls.append(("visible", self.selector))
        if self.visibility_error:
            raise RuntimeError("visibility lookup failed")
        return self.visible

    async def element_handle(self):
        self.calls.append(("handle", self.selector))
        return self

    async def click(self):
        self.calls.append(("click", self.selector))
        if self.click_error:
            raise RuntimeError("click completion unknown")


class FakePage:
    def __init__(self, locators):
        self.locators = locators

    def locator(self, selector):
        return self.locators[selector]


class DummySelectorAdapter(SelectorFlowAdapter):
    PLATFORM = "weibo"
    ACTIONS = ("followed", "liked", "commented", "reposted")


class SelectorClickSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_exception_never_falls_through_to_second_selector(self):
        calls = []
        page = FakePage({
            "first": FakeLocator("first", calls, click_error=True),
            "second": FakeLocator("second", calls),
        })

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "click completion unknown"):
                await click_first_visible(page, ["first", "second"], "liked", required=True)

        self.assertEqual(calls, [("visible", "first"), ("handle", "first"), ("click", "first")])

    async def test_visibility_lookup_failure_can_try_next_selector(self):
        calls = []
        page = FakePage({
            "first": FakeLocator("first", calls, visibility_error=True),
            "second": FakeLocator("second", calls),
        })

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            clicked = await click_first_visible(page, ["first", "second"], "liked", required=True)

        self.assertTrue(clicked)
        self.assertEqual(
            calls,
            [("visible", "first"), ("visible", "second"), ("handle", "second"), ("click", "second")],
        )

    async def test_authoritative_guard_runs_immediately_before_click(self):
        calls = []
        page = FakePage({"first": FakeLocator("first", calls)})

        async def guard(event):
            calls.append(("guard", event))

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            await click_first_visible(
                page,
                ["first"],
                "commented",
                required=True,
                before_click=guard,
            )

        self.assertEqual(
            calls,
            [("visible", "first"), ("handle", "first"), ("guard", "commented"), ("click", "first")],
        )

    async def test_guard_failure_prevents_click(self):
        calls = []
        page = FakePage({"first": FakeLocator("first", calls)})
        guard = AsyncMock(side_effect=RuntimeError("gate closed"))

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "gate closed"):
                await click_first_visible(
                    page,
                    ["first"],
                    "reposted",
                    required=True,
                    before_click=guard,
                )

        self.assertEqual(calls, [("visible", "first"), ("handle", "first")])

    async def test_success_readback_error_blocks_before_mutation(self):
        calls = []
        page = FakePage({
            "first": FakeLocator("first", calls),
            "done": FakeLocator("done", calls, visibility_error=True),
        })
        adapter = DummySelectorAdapter(
            selector_config={"liked": {"click": ["first"], "done": ["done"]}}
        )

        with self.assertRaisesRegex(
            SelectorReadbackIndeterminate,
            "success_readback_indeterminate",
        ):
            await adapter._like(page)

        self.assertEqual(calls, [("visible", "done")])
        self.assertFalse(adapter.mutation_started)

    async def test_mutation_marker_is_set_immediately_before_click(self):
        calls = []
        page = FakePage({"first": FakeLocator("first", calls)})

        def started(event):
            calls.append(("started", event))

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            await click_first_visible(
                page,
                ["first"],
                "liked",
                required=True,
                on_click_started=started,
            )

        self.assertEqual(
            calls,
            [("visible", "first"), ("handle", "first"), ("started", "liked"), ("click", "first")],
        )

    async def test_missing_success_readback_is_fail_closed(self):
        with self.assertRaisesRegex(UnsupportedPlatformAction, "success selectors"):
            await verify_done_state(FakePage({}), {}, "liked", "weibo")

    async def test_missing_readback_blocks_before_any_mutation_click(self):
        calls = []
        page = FakePage({"first": FakeLocator("first", calls)})
        adapter = DummySelectorAdapter(
            selector_config={"liked": {"click": ["first"]}}
        )

        with self.assertRaisesRegex(UnsupportedPlatformAction, "success selectors"):
            await adapter._like(page)

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
