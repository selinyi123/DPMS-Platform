import asyncio
import json
from pathlib import Path

from app.adapter_config import STRUCTURED_SELECTOR_PLATFORMS
from app.adapters.registry import get_adapter
from app.db import database, redis
from app.event_store.service import record_event
from app.safety import detect_page_risk
from app.utils.cookies import inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log


STREAM_KEY = "adapter_probe_requests"
GROUP_NAME = "adapter-probers"
CONSUMER_NAME = "adapter-prober-1"
SCREENSHOT_DIR = Path("/profiles/adapter-probes")


async def probe_loop(pool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    while not shutdown_event.is_set():
        try:
            messages = await asyncio.wait_for(
                redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000),
                timeout=1,
            )
            if not messages:
                continue
            for msg_id, data in messages[0][1]:
                await handle_probe(pool, {k: v for k, v in data.items()})
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "adapter_probe_loop_error", exception=e)
            await asyncio.sleep(3)


async def handle_probe(pool, probe: dict):
    probe_id = probe["probe_id"]
    platform = probe.get("platform", "bilibili")
    account_id = int(probe["account_id"])
    target_url = probe["target_url"]
    adapter = get_adapter(platform)
    screenshot_path = str(SCREENSHOT_DIR / f"{probe_id}.png")

    await database.execute(
        "UPDATE adapter_calibrations SET status = 'running', started_at = NOW() WHERE probe_id = :probe_id",
        {"probe_id": probe_id},
    )
    await record_event(
        aggregate="lottery",
        aggregate_id=probe.get("lottery_id") or probe_id,
        event_type="AdapterProbeStarted",
        payload={"probe_id": probe_id, "platform": platform, "account_id": account_id, "target_url": target_url},
        correlation_id=probe_id,
    )

    ctx = None
    page = None
    try:
        ctx = await pool.get_account_context(account_id, f"/profiles/{platform}/account_{account_id}")
        await inject_probe_cookies(ctx, account_id, platform)
        page = await ctx.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        await detect_page_risk(page, account_id)

        result = await probe_selectors(page, getattr(adapter, "SELECTOR_PROBES", {}))
        result["_summary"] = summarize_probe_result(platform, result)
        result["_recommended_config"] = build_recommended_config(platform, result)
        await page.screenshot(path=screenshot_path, full_page=True)
        await database.execute(
            """UPDATE adapter_calibrations
               SET status = 'succeeded', result = :result, screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE probe_id = :probe_id""",
            {"probe_id": probe_id, "result": json.dumps(result, ensure_ascii=False), "screenshot_path": screenshot_path},
        )
        await record_event(
            aggregate="lottery",
            aggregate_id=probe.get("lottery_id") or probe_id,
            event_type="AdapterProbeSucceeded",
            payload={
                "probe_id": probe_id,
                "platform": platform,
                "account_id": account_id,
                "result_summary": result.get("_summary"),
                "screenshot_path": screenshot_path,
            },
            correlation_id=probe_id,
        )
        structured_log("info", "adapter_probe_completed", task_id=probe_id, phase=platform)
    except Exception as e:
        if page:
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None
        await database.execute(
            """UPDATE adapter_calibrations
               SET status = 'failed', error_message = :error, screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE probe_id = :probe_id""",
            {"probe_id": probe_id, "error": str(e), "screenshot_path": screenshot_path},
        )
        await record_event(
            aggregate="lottery",
            aggregate_id=probe.get("lottery_id") or probe_id,
            event_type="AdapterProbeFailed",
            payload={"probe_id": probe_id, "platform": platform, "account_id": account_id, "error": str(e), "screenshot_path": screenshot_path},
            correlation_id=probe_id,
        )
        structured_log("error", "adapter_probe_failed", task_id=probe_id, exception=e)
    finally:
        if page:
            await page.close()


async def inject_probe_cookies(ctx, account_id: int, platform: str):
    row = await database.fetch_one(
        "SELECT encrypted_credential FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    try:
        credential = cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception:
        credential = credential_blob.decode("utf-8") if isinstance(credential_blob, bytes) else str(credential_blob)
    await inject_account_cookies(ctx, platform, credential)


async def probe_selectors(page, selector_groups: dict[str, list[str]]) -> dict:
    output = {}
    for phase, selectors in selector_groups.items():
        phase_result = []
        for selector in selectors:
            item = {"selector": selector, "visible": False, "count": 0, "error": None}
            try:
                locator = page.locator(selector)
                item["count"] = await locator.count()
                if item["count"]:
                    item["visible"] = await locator.first.is_visible(timeout=1000)
            except Exception as e:
                item["error"] = str(e)
            phase_result.append(item)
        output[phase] = phase_result
    return output


def summarize_probe_result(platform: str, result: dict) -> dict:
    phases = ["followed", "liked", "commented", "reposted"]
    phase_status = {}
    visible_phases = []
    for phase in phases:
        candidates = result.get(phase) if isinstance(result.get(phase), list) else []
        visible = [item for item in candidates if item.get("visible") and item.get("selector")]
        ready = bool(visible)
        if platform in STRUCTURED_SELECTOR_PLATFORMS and phase == "commented":
            ready = bool(
                first_selector_matching(visible, ["textarea", "contenteditable", "placeholder", "textbox"])
                and first_selector_matching(visible, ["button", "\u53d1\u5e03", "\u8bc4\u8bba", "\u53d1\u9001", "submit", "publish"])
            )
        phase_status[phase] = {
            "candidate_count": len(candidates),
            "visible_count": len(visible),
            "ready": ready,
            "visible_selectors": [item["selector"] for item in visible],
        }
        if ready:
            visible_phases.append(phase)
    return {
        "platform": platform,
        "required_phases": phases,
        "visible_phases": visible_phases,
        "missing_phases": [phase for phase in phases if phase not in visible_phases],
        "ready_phase_count": len(visible_phases),
        "ready_for_real_actions": len(visible_phases) == len(phases),
        "phase_status": phase_status,
    }


def build_recommended_config(platform: str, result: dict) -> dict:
    phases = {}
    for phase in ["followed", "liked", "reposted"]:
        selector = first_visible_selector(result.get(phase))
        if selector:
            phases[phase] = [selector]

    comment_candidates = [item for item in result.get("commented", []) if item.get("visible") and item.get("selector")]
    input_selector = first_selector_matching(comment_candidates, ["textarea", "contenteditable", "placeholder"])
    submit_selector = first_selector_matching(comment_candidates, ["button", "发布", "评论", "发送", "submit", "publish"])
    if input_selector and submit_selector and input_selector != submit_selector:
        phases["commented"] = {"input": [input_selector], "submit": [submit_selector], "text": "\u53c2\u4e0e\u62bd\u5956"}

    return {platform: phases} if phases else {}


def first_visible_selector(candidates) -> str | None:
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if item.get("visible") and item.get("selector"):
            return item["selector"]
    return None


def first_selector_matching(candidates: list[dict], markers: list[str]) -> str | None:
    for item in candidates:
        selector = str(item.get("selector") or "")
        lower = selector.lower()
        if any(marker.lower() in lower for marker in markers):
            return selector
    return None
