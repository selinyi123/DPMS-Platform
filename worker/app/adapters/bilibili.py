from playwright.async_api import Page

from app.adapters.base import BaseAdapter
from app.behavior_engine import BehaviorEngine
from app.utils.log import structured_log


FOLLOW_TEXT = "\u5173\u6ce8"
LIKE_TEXT = "\u70b9\u8d5e"
COMMENT_TEXT = "\u53c2\u4e0e\u62bd\u5956"
PUBLISH_TEXT = "\u53d1\u5e03"
FORWARD_TEXT = "\u8f6c\u53d1"
CONFIRM_TEXT = "\u786e\u5b9a"


class BilibiliAdapter(BaseAdapter):
    PLATFORM = "bilibili"
    REAL_ACTIONS = True
    STATUS = "gray"
    SELECTOR_PROBES = {
        "followed": [
            f"text={FOLLOW_TEXT}",
            f"button:has-text('{FOLLOW_TEXT}')",
            f"[class*='follow']:has-text('{FOLLOW_TEXT}')",
        ],
        "liked": [
            f"text={LIKE_TEXT}",
            f"[aria-label*='{LIKE_TEXT}']",
            "[class*='like']",
        ],
        "commented": [
            "textarea",
            "[contenteditable='true']",
            f"button:has-text('{PUBLISH_TEXT}')",
        ],
        "reposted": [
            f"text={FORWARD_TEXT}",
            f"button:has-text('{FORWARD_TEXT}')",
            "[class*='share']",
        ],
    }

    async def _follow(self, page: Page):
        await click_first_visible(page, self.SELECTOR_PROBES["followed"], "followed")

    async def _like(self, page: Page):
        await click_first_visible(page, self.SELECTOR_PROBES["liked"], "liked")

    async def _comment(self, page: Page):
        await BehaviorEngine.random_delay()
        box = page.locator("textarea").first
        if await box.is_visible(timeout=1500):
            await box.click()
            await BehaviorEngine.type_naturally(page, COMMENT_TEXT)
            await click_first_visible(
                page,
                [f"text={PUBLISH_TEXT}", f"button:has-text('{PUBLISH_TEXT}')"],
                "commented",
            )
            return
        structured_log("warning", "comment_box_not_found")

    async def _repost(self, page: Page):
        await click_first_visible(page, self.SELECTOR_PROBES["reposted"], "reposted")
        await click_first_visible(
            page,
            [f"text={CONFIRM_TEXT}", f"button:has-text('{CONFIRM_TEXT}')", f"text={PUBLISH_TEXT}"],
            "repost_confirmed",
            required=False,
        )


async def click_first_visible(page: Page, selectors: list[str], event: str, required: bool = False):
    await BehaviorEngine.random_delay()
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=1500):
                await locator.click()
                structured_log("info", event)
                return True
        except Exception:
            continue
    if required:
        raise RuntimeError(f"Required selector not found for {event}")
    structured_log("warning", f"{event}_selector_not_found")
    return False
