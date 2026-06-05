import asyncio

from app.api.notify import SENDERS, configured_channels, dispatch_notification
from app.db import database, redis
from app.utils.log import structured_log


STREAM_KEY = "notify_events"
GROUP_NAME = "notify-dispatchers"
CONSUMER_NAME = "core-notify-1"


async def parse_channels(value: str | None) -> list[str]:
    if not value or value == "all":
        return await configured_channels()
    return [item.strip() for item in value.split(",") if item.strip()]


async def start_notification_dispatcher():
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    while True:
        try:
            messages = await redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=5, block=5000)
            if not messages:
                continue

            for msg_id, data in messages[0][1]:
                await handle_event(data)
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "notification_dispatcher_error", exception=e)
            await asyncio.sleep(3)


async def handle_event(data: dict):
    title = data.get("title") or "DPMS notification"
    content = data.get("content") or ""
    event_type = data.get("event_type") or "generic"
    severity = data.get("severity") or "info"
    channels = await parse_channels(data.get("channels"))

    if not channels:
        await database.execute(
            """INSERT INTO notify_logs (channel, title, content, success)
               VALUES ('dispatch', :title, :content, 0)""",
            {"title": f"[{severity}] {title}", "content": f"{content}\n\nEVENT: {event_type}\nSKIPPED: no notification channels configured"},
        )
        structured_log("warning", "notification_skipped_no_channels", title=title)
        return

    for channel in channels:
        if channel not in SENDERS:
            await database.execute(
                """INSERT INTO notify_logs (channel, title, content, success)
                   VALUES (:ch, :title, :content, 0)""",
                {
                    "ch": channel,
                    "title": f"[{severity}] {title}",
                    "content": f"{content}\n\nEVENT: {event_type}\nERROR: unsupported notification channel",
                },
            )
            structured_log("warning", "notification_unsupported_channel", channel=channel, title=title)
            continue
        configured = await configured_channels()
        if channel not in configured:
            await database.execute(
                """INSERT INTO notify_logs (channel, title, content, success)
                   VALUES (:ch, :title, :content, 0)""",
                {
                    "ch": channel,
                    "title": f"[{severity}] {title}",
                    "content": f"{content}\n\nEVENT: {event_type}\nERROR: channel is not configured",
                },
            )
            structured_log("warning", "notification_channel_not_configured", channel=channel, title=title)
            continue
        log_id = await database.execute(
            "INSERT INTO notify_logs (channel, title, content, success) VALUES (:ch, :title, :content, 0)",
            {"ch": channel, "title": f"[{severity}] {title}", "content": f"{content}\n\nEVENT: {event_type}"},
        )
        await dispatch_notification(log_id, channel, f"[{severity}] {title}", f"{content}\n\nEVENT: {event_type}")
