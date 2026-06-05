
import asyncio

from datetime import datetime

from app.services.discovery import run_discovery

from app.services.risk_engine import check_all_accounts_health

from app.utils.log import structured_log



async def scheduler_loop():

    structured_log("info", "scheduler_started")

    while True:

        try:

            now = datetime.now()

            if now.minute % 15 == 0:

                await run_discovery()

            if now.minute % 10 == 0:

                await check_all_accounts_health()

            await asyncio.sleep(60)

        except Exception as e:

            structured_log("error", "scheduler_error", exception=e)

            await asyncio.sleep(60)
