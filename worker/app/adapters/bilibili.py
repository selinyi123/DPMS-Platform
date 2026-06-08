from playwright.async_api import Page

from app.adapter_config import has_complete_selectors, selectors_for
from app.adapters.base import BaseAdapter, UnsupportedPlatformAction
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
    REAL_ACTIONS = False
    STATUS = "calibration_required"
    DEFAULT_SELECTOR_PROBES = {
        "followed": [
            f"button:has-text('{FOLLOW_TEXT}')",
            f"[class*='follow-btn']:has-text('{FOLLOW_TEXT}')",
            f"[class*='follow-button']:has-text('{FOLLOW_TEXT}')",
        ],
        "liked": [
            f"[aria-label*='{LIKE_TEXT}']",
            "button[class*='like']",
            "[class*='like-button']",
        ],
        "commented": [
            "textarea",
            "[contenteditable='true'][role='textbox']",
            f"button:has-text('{PUBLISH_TEXT}')",
        ],
        "reposted": [
            f"button:has-text('{FORWARD_TEXT}')",
            "button[class*='share']",
            "[class*='share-button']",
        ],
    }

    def __init__(self, selector_config: dict | None = None):
        configured = selector_config if isinstance(selector_config, dict) else selectors_for(self.PLATFORM)
        self.configured_selectors = configured
        self.SELECTOR_PROBES = {
            **self.DEFAULT_SELECTOR_PROBES,
            **{
                phase: probe_selectors(value)
                for phase, value in configured.items()
                if phase in self.ACTIONS
            },
        }
        self.REAL_ACTIONS = complete_bilibili_selectors(configured) if selector_config is not None else has_complete_selectors(self.PLATFORM)
        self.STATUS = "configured" if self.REAL_ACTIONS else "calibration_required"

    async def _follow(self, page: Page):
        await self._click_phase(page, "followed")

    async def _like(self, page: Page):
        await self._click_phase(page, "liked")

    async def _comment(self, page: Page):
        config = self._phase_config("commented")
        if not isinstance(config, dict):
            raise UnsupportedPlatformAction("bilibili commented selectors require input and submit groups")
        inputs = selector_list(config.get("input") or config.get("inputs"))
        submits = selector_list(config.get("submit") or config.get("submits"))
        if not inputs or not submits:
            raise UnsupportedPlatformAction("bilibili comment input or submit selector is not configured")

        if await any_visible(page, selector_list(config.get("done") or config.get("success"))):
            structured_log("info", "bilibili_phase_already_complete", phase="commented")
            return

        await BehaviorEngine.random_delay()
        text = str(config.get("text") or COMMENT_TEXT).strip()
        for selector in inputs:
            box = page.locator(selector).first
            try:
                visible = await box.is_visible(timeout=1500)
            except Exception:
                continue
            if visible:
                await box.click()
                await BehaviorEngine.type_naturally(page, text)
                await click_first_visible(page, submits, "commented", required=True)
                await verify_done_state(page, config, "commented")
                return
        raise UnsupportedPlatformAction("bilibili comment input selector was not found")

    async def _repost(self, page: Page):
        config = self._phase_config("reposted")
        await self._click_phase(page, "reposted", verify=False)
        if isinstance(config, dict):
            confirms = selector_list(config.get("confirm") or config.get("submit"))
            if confirms:
                await click_first_visible(page, confirms, "repost_confirmed", required=True)
            await verify_done_state(page, config, "reposted")

    async def _click_phase(self, page: Page, phase: str, verify: bool = True):
        config = self._phase_config(phase)
        selectors = selector_list(config)
        if not selectors:
            raise UnsupportedPlatformAction(f"bilibili {phase} selectors are not configured")
        if isinstance(config, dict) and await any_visible(page, selector_list(config.get("done") or config.get("success"))):
            structured_log("info", "bilibili_phase_already_complete", phase=phase)
            return
        await click_first_visible(page, selectors, phase, required=True)
        if verify and isinstance(config, dict):
            await verify_done_state(page, config, phase)

    def _phase_config(self, phase: str):
        return self.configured_selectors.get(phase)


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


async def any_visible(page: Page, selectors: list[str], timeout: int = 1200) -> bool:
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=timeout):
                return True
        except Exception:
            continue
    return False


async def verify_done_state(page: Page, config: dict, phase: str) -> None:
    done = selector_list(config.get("done") or config.get("success"))
    if not done:
        return
    for selector in done:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=3000)
            return
        except Exception:
            continue
    raise RuntimeError(f"bilibili {phase} action could not be verified")


def selector_list(value) -> list[str]:
    if isinstance(value, dict):
        value = value.get("click") or value.get("selectors") or value.get("buttons") or []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def probe_selectors(value) -> list[str]:
    if not isinstance(value, dict):
        return selector_list(value)
    output = []
    for key in ("click", "selectors", "buttons", "input", "inputs", "submit", "submits", "confirm", "done", "success"):
        output.extend(selector_list(value.get(key)))
    return list(dict.fromkeys(output))


def complete_bilibili_selectors(configured: dict) -> bool:
    if not isinstance(configured, dict):
        return False
    if not all(selector_list(configured.get(phase)) for phase in ("followed", "liked", "reposted")):
        return False
    comment = configured.get("commented")
    return isinstance(comment, dict) and bool(
        selector_list(comment.get("input") or comment.get("inputs"))
        and selector_list(comment.get("submit") or comment.get("submits"))
    )
