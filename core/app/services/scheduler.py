
import asyncio

from datetime import datetime

from app.db import database

from app.services.discovery import run_discovery

from app.services.risk_engine import check_all_accounts_health

from app.utils.log import structured_log
from shared.platform_scope import normalize_platform_scope


DAILY_RESET_SETTING = "daily_task_count_reset_date"
_scheduled_discovery_task: asyncio.Task | None = None


async def scheduler_loop(
    *,
    platforms=None,
    include_global: bool = True,
    fail_closed: bool = False,
):
    if platforms is None:
        selected_platforms = None
    else:
        platform_values = (
            (platforms,) if isinstance(platforms, str) else tuple(platforms)
        )
        selected_platforms = (
            normalize_platform_scope(platform_values)
            if platform_values
            else ()
        )
    structured_log(
        "info",
        "scheduler_started",
        platform_scope=(
            "all"
            if selected_platforms is None
            else ",".join(selected_platforms) or "none"
        ),
        include_global=include_global,
    )
    while True:
        try:
            now = datetime.now()
            if include_global:
                await reset_daily_account_counters_if_due(now)
            if now.minute % 15 == 0 and selected_platforms != ():
                discovery_task = schedule_discovery(
                    platforms=selected_platforms
                )
                if fail_closed:
                    await discovery_task
            if include_global and now.minute % 10 == 0:
                await check_all_accounts_health()
            await asyncio.sleep(60)
        except Exception as e:
            structured_log("error", "scheduler_error", exception=e)
            if fail_closed:
                raise
            await asyncio.sleep(60)


def schedule_discovery(*, platforms=None) -> asyncio.Task:
    """Start one scheduler waiter without blocking later maintenance work."""

    global _scheduled_discovery_task
    task = _scheduled_discovery_task
    if task is None or task.done():
        discovery_call = (
            run_discovery()
            if platforms is None
            else run_discovery(platforms=platforms)
        )
        task = asyncio.create_task(
            discovery_call,
            name="dpms-scheduled-discovery",
        )
        _scheduled_discovery_task = task
        task.add_done_callback(_finish_scheduled_discovery)
    return task


def _finish_scheduled_discovery(task: asyncio.Task) -> None:
    global _scheduled_discovery_task
    if _scheduled_discovery_task is task:
        _scheduled_discovery_task = None
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        # Discovery failures are isolated from the scheduler loop so account
        # health and daily reset work continue on their own cadence.
        structured_log("error", "scheduled_discovery_failed", exception=exc)


async def reset_daily_account_counters_if_due(now: datetime) -> None:
    today = now.date().isoformat()
    row = await database.fetch_one(
        "SELECT setting_value FROM runtime_settings WHERE setting_key = :key",
        {"key": DAILY_RESET_SETTING},
    )
    last_reset = str(row["setting_value"]) if row else ""
    if last_reset == today:
        return
    if not last_reset:
        await set_daily_reset_marker(today)
        return
    await database.execute("UPDATE accounts SET daily_task_count = 0 WHERE daily_task_count <> 0")
    await set_daily_reset_marker(today)
    structured_log("info", "daily_task_count_reset", reset_date=today, previous_reset=last_reset)


async def set_daily_reset_marker(value: str) -> None:
    await database.execute(
        """INSERT INTO runtime_settings (setting_key, setting_value)
           VALUES (:key, :value)
           ON DUPLICATE KEY UPDATE setting_value = :value, updated_at = NOW()""",
        {"key": DAILY_RESET_SETTING, "value": value},
    )
