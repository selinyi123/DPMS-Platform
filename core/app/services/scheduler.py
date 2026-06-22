
import asyncio

from datetime import datetime

from app.db import database

from app.services.discovery import run_discovery

from app.services.risk_engine import check_all_accounts_health

from app.utils.log import structured_log


DAILY_RESET_SETTING = "daily_task_count_reset_date"


async def scheduler_loop():

    structured_log("info", "scheduler_started")

    while True:

        try:

            now = datetime.now()

            await reset_daily_account_counters_if_due(now)

            if now.minute % 15 == 0:

                await run_discovery()

            if now.minute % 10 == 0:

                await check_all_accounts_health()

            await asyncio.sleep(60)

        except Exception as e:

            structured_log("error", "scheduler_error", exception=e)

            await asyncio.sleep(60)


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
