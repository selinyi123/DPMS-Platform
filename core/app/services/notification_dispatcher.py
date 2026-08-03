import asyncio
import contextvars
import uuid

from app.api.notify import (
    SENDERS,
    configured_channels,
    dispatch_notification,
    notification_config_revision,
)
from app.db import database, redis
from app.utils.log import structured_log
from shared.redis_consumer_groups import (
    NOTIFY_EVENT_GROUP_NAME,
    NOTIFY_EVENT_STREAM_KEY,
    retire_stale_consumer_metadata,
    verify_redis_consumer_group,
)
from shared.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA


STREAM_KEY = NOTIFY_EVENT_STREAM_KEY
GROUP_NAME = NOTIFY_EVENT_GROUP_NAME
NOTIFICATION_CONSUMER_PREFIX = "core-notify:"
NOTIFICATION_RECLAIM_INTERVAL_SECONDS = 30
NOTIFICATION_RECLAIM_COUNT = 5
NOTIFICATION_HANDLER_TIMEOUT_SECONDS = 120
# A whole batch enters the PEL before serial side effects start. Keep reclaim
# later than the maximum bounded batch runtime so a rolling peer cannot steal
# a live entry that is merely waiting behind earlier notifications.
NOTIFICATION_RECLAIM_IDLE_MILLISECONDS = 15 * 60 * 1000
_CURRENT_NOTIFICATION_STREAM_MESSAGE_ID = contextvars.ContextVar(
    "current_notification_stream_message_id",
    default=None,
)


def _new_notification_consumer_name() -> str:
    return f"{NOTIFICATION_CONSUMER_PREFIX}{uuid.uuid4().hex}"


async def _ack_terminal_notification(message_id: str) -> dict[str, int]:
    """ACK and delete only after every attached group confirms this entry."""

    result = list(
        await redis.eval(
            SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
            1,
            STREAM_KEY,
            GROUP_NAME,
            str(message_id),
        )
        or ()
    )
    if len(result) != 2:
        raise RuntimeError("notification_terminal_ack_result_invalid")
    try:
        acknowledged, deleted = (int(result[0]), int(result[1]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "notification_terminal_ack_result_invalid"
        ) from exc
    if (
        acknowledged not in {0, 1}
        or deleted not in {0, 1}
        or (acknowledged == 0 and deleted == 0)
    ):
        raise RuntimeError("notification_terminal_ack_result_invalid")
    return {
        "acknowledged": acknowledged,
        "deleted": deleted,
    }


async def _process_notification_entries(entries) -> int:
    processed = 0
    for message_id, data in entries or ():
        token = _CURRENT_NOTIFICATION_STREAM_MESSAGE_ID.set(str(message_id))
        try:
            await asyncio.wait_for(
                handle_event(dict(data or {})),
                timeout=NOTIFICATION_HANDLER_TIMEOUT_SECONDS,
            )
        finally:
            _CURRENT_NOTIFICATION_STREAM_MESSAGE_ID.reset(token)
        await _ack_terminal_notification(str(message_id))
        processed += 1
    return processed


async def _reclaim_stale_notifications(consumer_name: str) -> int:
    """Claim a bounded stale PEL batch and retry it before new deliveries."""

    pending = list(
        await redis.xpending_range(
            STREAM_KEY,
            GROUP_NAME,
            min="-",
            max="+",
            count=NOTIFICATION_RECLAIM_COUNT,
            idle=NOTIFICATION_RECLAIM_IDLE_MILLISECONDS,
        )
        or ()
    )
    message_ids = [
        str(entry.get("message_id") or "").strip()
        for entry in pending
        if (
            str(entry.get("message_id") or "").strip()
            and int(entry.get("time_since_delivered") or 0)
            >= NOTIFICATION_RECLAIM_IDLE_MILLISECONDS
        )
    ]
    if not message_ids:
        return 0
    claimed = await redis.xclaim(
        STREAM_KEY,
        GROUP_NAME,
        consumer_name,
        min_idle_time=NOTIFICATION_RECLAIM_IDLE_MILLISECONDS,
        message_ids=message_ids,
    )
    return await _process_notification_entries(claimed)


async def parse_channels(value: str | None) -> list[str]:
    if not value or value == "all":
        return await configured_channels()
    return [item.strip() for item in value.split(",") if item.strip()]


async def start_notification_dispatcher():
    await verify_redis_consumer_group(
        redis,
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
    )
    consumer_name = _new_notification_consumer_name()
    last_maintenance_at = float("-inf")
    while True:
        try:
            loop = asyncio.get_running_loop()
            if (
                loop.time() - last_maintenance_at
                >= NOTIFICATION_RECLAIM_INTERVAL_SECONDS
            ):
                try:
                    await _reclaim_stale_notifications(consumer_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    structured_log(
                        "error",
                        "notification_pending_reclaim_failed",
                        exception=exc,
                    )
                try:
                    await retire_stale_consumer_metadata(
                        redis,
                        stream_key=STREAM_KEY,
                        group_name=GROUP_NAME,
                        current_consumer_name=consumer_name,
                        managed_consumer_prefix=(
                            NOTIFICATION_CONSUMER_PREFIX
                        ),
                        minimum_idle_milliseconds=(
                            NOTIFICATION_RECLAIM_IDLE_MILLISECONDS
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    structured_log(
                        "error",
                        "notification_consumer_retention_failed",
                        exception=exc,
                    )
                last_maintenance_at = loop.time()
            messages = await redis.xreadgroup(
                GROUP_NAME,
                consumer_name,
                {STREAM_KEY: ">"},
                count=NOTIFICATION_RECLAIM_COUNT,
                block=5000,
            )
            if not messages:
                continue
            for stream_name, entries in messages:
                if str(stream_name) != STREAM_KEY:
                    raise RuntimeError(
                        "notification_stream_response_mismatch"
                    )
                await _process_notification_entries(entries)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            structured_log("error", "notification_dispatcher_error", exception=e)
            await asyncio.sleep(3)


async def handle_event(data: dict):
    stream_message_id = _CURRENT_NOTIFICATION_STREAM_MESSAGE_ID.get()
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
        config_revision = await notification_config_revision(channel)
        if config_revision is None:
            await database.execute(
                """INSERT INTO notify_logs (channel, title, content, success)
                   VALUES (:ch, :title, :content, 0)""",
                {
                    "ch": channel,
                    "title": f"[{severity}] {title}",
                    "content": f"{content}\n\nEVENT: {event_type}\nERROR: notification_config_revision_unavailable",
                },
            )
            continue
        log_id = await database.execute(
            "INSERT INTO notify_logs (channel, title, content, success, config_revision) VALUES (:ch, :title, :content, 0, :config_revision)",
            {
                "ch": channel,
                "title": f"[{severity}] {title}",
                "content": f"{content}\n\nEVENT: {event_type}",
                "config_revision": config_revision,
            },
        )
        await dispatch_notification(
            log_id,
            channel,
            f"[{severity}] {title}",
            f"{content}\n\nEVENT: {event_type}",
            config_revision,
            stream_message_id=stream_message_id,
        )
