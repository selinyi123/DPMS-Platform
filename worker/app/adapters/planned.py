from playwright.async_api import Page

from app.adapter_config import has_complete_selectors, selectors_for
from app.adapters.base import BaseAdapter, UnsupportedPlatformAction
from app.adapters.bilibili import click_first_visible
from app.behavior_engine import BehaviorEngine
from app.utils.log import structured_log


COMMENT_TEXT = "Participate"


class ConfigurableSelectorAdapter(BaseAdapter):
    REAL_ACTIONS = False
    STATUS = "planned"
    DEFAULT_SELECTOR_PROBES: dict[str, list[str]] = {}

    def __init__(self, selector_config: dict | None = None):
        configured = selector_config if isinstance(selector_config, dict) else selectors_for(self.PLATFORM)
        self.configured_selectors = configured
        self.SELECTOR_PROBES = {
            **self.DEFAULT_SELECTOR_PROBES,
            **{phase: normalize_probe_selectors(value) for phase, value in configured.items()},
        }
        self.REAL_ACTIONS = complete_selectors(configured) if selector_config is not None else has_complete_selectors(self.PLATFORM)
        self.STATUS = "configured" if self.REAL_ACTIONS else "planned"

    async def _follow(self, page: Page):
        await self._click_phase(page, "followed")

    async def _like(self, page: Page):
        await self._click_phase(page, "liked")

    async def _comment(self, page: Page):
        config = self._phase_config("commented")
        if isinstance(config, dict):
            inputs = normalize_probe_selectors(config.get("input") or config.get("inputs") or [])
            submits = normalize_probe_selectors(config.get("submit") or config.get("submits") or config.get("button") or [])
            text = str(config.get("text") or COMMENT_TEXT)
            await BehaviorEngine.random_delay()
            for selector in inputs:
                locator = page.locator(selector).first
                try:
                    if await locator.is_visible(timeout=1500):
                        await locator.click()
                        await BehaviorEngine.type_naturally(page, text)
                        await click_first_visible(page, submits, "commented", required=True)
                        return
                except Exception:
                    continue
            raise UnsupportedPlatformAction(f"{self.PLATFORM} comment input selector was not found")
        await self._click_phase(page, "commented")

    async def _repost(self, page: Page):
        await self._click_phase(page, "reposted")

    async def _click_phase(self, page: Page, phase: str):
        selectors = normalize_probe_selectors(self._phase_config(phase))
        if not selectors:
            raise UnsupportedPlatformAction(f"{self.PLATFORM} {phase} selectors are not configured")
        ok = await click_first_visible(page, selectors, phase, required=True)
        if ok:
            structured_log("info", "configured_adapter_phase_clicked", phase=phase)

    def _phase_config(self, phase: str):
        return self.configured_selectors.get(phase)


def normalize_probe_selectors(value) -> list[str]:
    if isinstance(value, dict):
        selectors = value.get("selectors") or value.get("click") or value.get("buttons") or []
        return normalize_probe_selectors(selectors)
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def complete_selectors(configured: dict) -> bool:
    return all(bool(configured.get(phase)) for phase in ("followed", "liked", "commented", "reposted"))


class WeiboAdapter(ConfigurableSelectorAdapter):
    PLATFORM = "weibo"
    DEFAULT_SELECTOR_PROBES = {
        "followed": ["text=\u5173\u6ce8", "button:has-text('\u5173\u6ce8')", "[class*='follow']"],
        "liked": ["text=\u8d5e", "[class*='like']", "[aria-label*='\u8d5e']"],
        "commented": ["textarea", "[contenteditable='true']", "button:has-text('\u8bc4\u8bba')"],
        "reposted": ["text=\u8f6c\u53d1", "button:has-text('\u8f6c\u53d1')", "[class*='forward']"],
    }


class DouyinAdapter(ConfigurableSelectorAdapter):
    PLATFORM = "douyin"
    DEFAULT_SELECTOR_PROBES = {
        "followed": ["text=\u5173\u6ce8", "button:has-text('\u5173\u6ce8')", "[class*='follow']"],
        "liked": ["[class*='like']", "[aria-label*='\u70b9\u8d5e']", "text=\u70b9\u8d5e"],
        "commented": ["textarea", "[contenteditable='true']", "[placeholder*='\u8bc4\u8bba']"],
        "reposted": ["text=\u5206\u4eab", "button:has-text('\u5206\u4eab')", "[class*='share']"],
    }


class XiaohongshuAdapter(ConfigurableSelectorAdapter):
    PLATFORM = "xiaohongshu"
    DEFAULT_SELECTOR_PROBES = {
        "followed": ["text=\u5173\u6ce8", "button:has-text('\u5173\u6ce8')", "[class*='follow']"],
        "liked": ["[class*='like']", "[aria-label*='\u70b9\u8d5e']", "text=\u70b9\u8d5e"],
        "commented": ["textarea", "[contenteditable='true']", "[placeholder*='\u8bc4\u8bba']"],
        "reposted": ["text=\u5206\u4eab", "button:has-text('\u5206\u4eab')", "[class*='share']"],
    }
