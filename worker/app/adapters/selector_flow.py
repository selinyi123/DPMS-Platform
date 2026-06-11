from playwright.async_api import Page

from app.adapter_config import has_complete_selectors, selectors_for
from app.adapters.base import BaseAdapter, UnsupportedPlatformAction
from app.behavior_engine import BehaviorEngine
from app.utils.log import structured_log


DEFAULT_COMMENT_TEXT = "参与抽奖"


class SelectorFlowAdapter(BaseAdapter):
    """Selector-config-driven adapter shared by platforms with structured selectors.

    Real actions stay disabled until the platform selector config is complete:
    followed/liked/reposted need click selectors and commented needs an
    input + submit group reviewed from probe evidence.
    """

    REAL_ACTIONS = False
    STATUS = "calibration_required"
    COMMENT_TEXT = DEFAULT_COMMENT_TEXT
    DEFAULT_SELECTOR_PROBES: dict[str, list[str]] = {}

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
        self.REAL_ACTIONS = complete_structured_selectors(configured) if selector_config is not None else has_complete_selectors(self.PLATFORM)
        self.STATUS = "configured" if self.REAL_ACTIONS else "calibration_required"

    async def _follow(self, page: Page):
        await self._click_phase(page, "followed")

    async def _like(self, page: Page):
        await self._click_phase(page, "liked")

    async def _comment(self, page: Page):
        config = self._phase_config("commented")
        if not isinstance(config, dict):
            raise UnsupportedPlatformAction(f"{self.PLATFORM} commented selectors require input and submit groups")
        inputs = selector_list(config.get("input") or config.get("inputs"))
        submits = selector_list(config.get("submit") or config.get("submits"))
        if not inputs or not submits:
            raise UnsupportedPlatformAction(f"{self.PLATFORM} comment input or submit selector is not configured")

        if await any_visible(page, selector_list(config.get("done") or config.get("success"))):
            structured_log("info", "adapter_phase_already_complete", phase="commented", platform=self.PLATFORM)
            return

        await BehaviorEngine.random_delay()
        text = str(config.get("text") or self.COMMENT_TEXT).strip()
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
                await verify_done_state(page, config, "commented", self.PLATFORM)
                return
        raise UnsupportedPlatformAction(f"{self.PLATFORM} comment input selector was not found")

    async def _repost(self, page: Page):
        config = self._phase_config("reposted")
        await self._click_phase(page, "reposted", verify=False)
        if isinstance(config, dict):
            confirms = selector_list(config.get("confirm") or config.get("submit"))
            if confirms:
                await click_first_visible(page, confirms, "repost_confirmed", required=True)
            await verify_done_state(page, config, "reposted", self.PLATFORM)

    async def _click_phase(self, page: Page, phase: str, verify: bool = True):
        config = self._phase_config(phase)
        selectors = selector_list(config)
        if not selectors:
            raise UnsupportedPlatformAction(f"{self.PLATFORM} {phase} selectors are not configured")
        if isinstance(config, dict) and await any_visible(page, selector_list(config.get("done") or config.get("success"))):
            structured_log("info", "adapter_phase_already_complete", phase=phase, platform=self.PLATFORM)
            return
        await click_first_visible(page, selectors, phase, required=True)
        if verify and isinstance(config, dict):
            await verify_done_state(page, config, phase, self.PLATFORM)

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


async def verify_done_state(page: Page, config: dict, phase: str, platform: str = "") -> None:
    done = selector_list(config.get("done") or config.get("success"))
    if not done:
        return
    for selector in done:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=3000)
            return
        except Exception:
            continue
    raise RuntimeError(f"{platform or 'adapter'} {phase} action could not be verified")


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


def complete_structured_selectors(configured: dict) -> bool:
    if not isinstance(configured, dict):
        return False
    if not all(selector_list(configured.get(phase)) for phase in ("followed", "liked", "reposted")):
        return False
    comment = configured.get("commented")
    return isinstance(comment, dict) and bool(
        selector_list(comment.get("input") or comment.get("inputs"))
        and selector_list(comment.get("submit") or comment.get("submits"))
    )
