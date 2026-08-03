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
    def __init__(
        self,
        selector,
        calls,
        *,
        visible=True,
        visibility_error=False,
        click_error=False,
        wait_error=False,
    ):
        self.selector = selector
        self.calls = calls
        self.visible = visible
        self.visibility_error = visibility_error
        self.click_error = click_error
        self.wait_error = wait_error
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

    async def wait_for(self, state=None, timeout=None):
        self.calls.append(("wait_for", self.selector, state))
        if self.wait_error:
            raise RuntimeError("done state not observed")


class FakePage:
    def __init__(self, locators):
        self.locators = locators

    def locator(self, selector):
        return self.locators[selector]


class DummySelectorAdapter(SelectorFlowAdapter):
    PLATFORM = "weibo"
    ACTIONS = ("followed", "liked", "commented", "favorited", "reposted")


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

    async def test_favorite_uses_explicit_click_guard_and_done_readback(self):
        calls = []
        page = FakePage({
            "favorite": FakeLocator("favorite", calls),
            "favorited": FakeLocator("favorited", calls, visible=False),
        })
        adapter = DummySelectorAdapter(
            selector_config={
                "favorited": {
                    "click": ["favorite"],
                    "done": ["favorited"],
                }
            }
        )

        async def guard(event):
            calls.append(("guard", event))

        adapter.set_mutation_guard(guard)
        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            await adapter._favorite(page)

        self.assertEqual(
            calls,
            [
                ("visible", "favorited"),
                ("visible", "favorite"),
                ("handle", "favorite"),
                ("guard", "favorited"),
                ("click", "favorite"),
                ("wait_for", "favorited", "visible"),
            ],
        )
        self.assertTrue(adapter.mutation_started)

    async def test_favorite_without_done_selector_is_blocked_before_click(self):
        calls = []
        page = FakePage({"favorite": FakeLocator("favorite", calls)})
        adapter = DummySelectorAdapter(
            selector_config={"favorited": {"click": ["favorite"]}}
        )

        with self.assertRaisesRegex(UnsupportedPlatformAction, "success selectors"):
            await adapter._favorite(page)

        self.assertEqual(calls, [])
        self.assertFalse(adapter.mutation_started)

    async def test_favorite_unverified_after_click_keeps_unknown_outcome_marker(self):
        calls = []
        page = FakePage({
            "favorite": FakeLocator("favorite", calls),
            "favorited": FakeLocator(
                "favorited",
                calls,
                visible=False,
                wait_error=True,
            ),
        })
        adapter = DummySelectorAdapter(
            selector_config={
                "favorited": {
                    "click": ["favorite"],
                    "done": ["favorited"],
                }
            }
        )

        with patch("app.adapters.selector_flow.BehaviorEngine.random_delay", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "could not be verified"):
                await adapter._favorite(page)

        self.assertEqual(
            calls,
            [
                ("visible", "favorited"),
                ("visible", "favorite"),
                ("handle", "favorite"),
                ("click", "favorite"),
                ("wait_for", "favorited", "visible"),
            ],
        )
        self.assertTrue(adapter.mutation_started)


if __name__ == "__main__":
    unittest.main()
