import base64
import binascii
import hashlib
import hmac
import secrets

import httpx

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.db import database, execute_affected_rows
from app.event_store.service import record_event
from app.models.schemas import NotifyRequest, NotifySecretBundleUpdate, NotifySecretUpdate
from app.security import audit_event, require_confirmation, require_min_role
from app.utils.crypto import cookie_vault, notification_secret_aad
from app.utils.log import structured_log
from app.utils.network_safety import assert_public_http_url


router = APIRouter()

CHANNEL_SECRET_KEYS = {
    "serverchan": ["SERVERCHAN_KEY"],
    "feishu": ["FEISHU_WEBHOOK"],
    "webhook": ["GENERIC_WEBHOOK_URL"],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
}
SECRET_ATTRS = {
    "SERVERCHAN_KEY": "serverchan_key",
    "FEISHU_WEBHOOK": "feishu_webhook",
    "GENERIC_WEBHOOK_URL": "generic_webhook_url",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}
VALID_NOTIFICATION_CHANNELS = set(CHANNEL_SECRET_KEYS)
NOTIFICATION_RESPONSE_MAX_BYTES = 64 * 1024
NOTIFICATION_REVISION_HMAC_CONTEXT = b"dpms:notification-config-revision:v1"
NOTIFICATION_DELIVERY_STALE_SECONDS = 120
NOTIFICATION_DELIVERY_MAX_ATTEMPTS = 5


class NotificationDeliveryContractError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def notification_delivery_key(
    channel: str,
    *,
    stream_message_id: str | None = None,
    log_id: int | str | None = None,
) -> str:
    """Return a deterministic key for one externally visible delivery.

    A Redis stream message is the durable identity for automatic notices.  A
    manual send has no stream id, so its notify-log id is used instead.  The
    digest keeps provider/channel details out of a primary key while retaining
    a stable, bounded value for MySQL.
    """

    normalized_channel = str(channel or "").strip().lower()
    if not normalized_channel:
        raise ValueError("notification_delivery_channel_required")
    normalized_stream_id = str(stream_message_id or "").strip()
    if normalized_stream_id:
        source = f"stream:{normalized_stream_id}"
    else:
        normalized_log_id = str(log_id or "").strip()
        if not normalized_log_id:
            raise ValueError("notification_delivery_identity_required")
        source = f"log:{normalized_log_id}"
    digest = hashlib.sha256(
        f"dpms:notification-delivery:v1:{normalized_channel}:{source}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"notify:{digest}"


async def _claim_notification_delivery(
    *,
    delivery_key: str,
    channel: str,
    stream_message_id: str | None,
    notify_log_id: int | str | None,
    config_revision: str,
) -> dict | None:
    """Claim a notification exactly once before invoking an external sender.

    ``None`` is a development-only compatibility result used by unit/dev
    instances that have not applied migration 0030 yet.  Production always
    fails closed if the durable claim table is unavailable.
    """

    try:
        async with database.transaction():
            await database.execute(
                """INSERT INTO notification_delivery_attempts
                   (delivery_key, stream_message_id, channel, notify_log_id,
                    config_revision, status)
                   VALUES (:delivery_key, :stream_message_id, :channel,
                           :notify_log_id, :config_revision, 'pending')
                   ON DUPLICATE KEY UPDATE delivery_key = delivery_key""",
                {
                    "delivery_key": delivery_key,
                    "stream_message_id": str(stream_message_id or "") or None,
                    "channel": channel,
                    "notify_log_id": notify_log_id,
                    "config_revision": config_revision,
                },
            )
            row = await database.fetch_one(
                """SELECT delivery_key, stream_message_id, channel,
                          notify_log_id, status, attempts, claim_token,
                          config_revision,
                          (updated_at < (NOW() - INTERVAL :stale SECOND))
                            AS stale
                   FROM notification_delivery_attempts
                  WHERE delivery_key = :delivery_key
                  FOR UPDATE""",
                {
                    "delivery_key": delivery_key,
                    "stale": NOTIFICATION_DELIVERY_STALE_SECONDS,
                },
            )
            if not row:
                raise NotificationDeliveryContractError(
                    "notification_delivery_claim_missing"
                )
            current = dict(row)
            status = str(current.get("status") or "").strip().lower()
            if status == "sent":
                return {"status": "sent", "delivery_key": delivery_key}
            if status == "uncertain":
                return {
                    "status": "uncertain",
                    "delivery_key": delivery_key,
                }
            if (
                current.get("config_revision")
                and str(current["config_revision"]) != str(config_revision)
            ):
                await database.execute(
                    """UPDATE notification_delivery_attempts
                       SET status = 'uncertain', uncertain_at = NOW(),
                           last_error = 'notification_config_revision_changed'
                     WHERE delivery_key = :delivery_key
                       AND status IN ('pending', 'failed')""",
                    {"delivery_key": delivery_key},
                )
                return {
                    "status": "uncertain",
                    "delivery_key": delivery_key,
                }
            if status == "sending":
                stale_value = current.get("stale")
                is_stale = stale_value in (True, 1, "1", "true", "TRUE")
                if not is_stale:
                    return {"status": "busy", "delivery_key": delivery_key}
                await database.execute(
                    """UPDATE notification_delivery_attempts
                       SET status = 'uncertain', uncertain_at = NOW(),
                           last_error = 'notification_sender_claim_expired'
                     WHERE delivery_key = :delivery_key AND status = 'sending'""",
                    {"delivery_key": delivery_key},
                )
                return {
                    "status": "uncertain",
                    "delivery_key": delivery_key,
                }
            attempts = int(current.get("attempts") or 0)
            if attempts >= NOTIFICATION_DELIVERY_MAX_ATTEMPTS:
                await database.execute(
                    """UPDATE notification_delivery_attempts
                       SET status = 'uncertain', uncertain_at = NOW(),
                           last_error = 'notification_delivery_attempt_limit'
                     WHERE delivery_key = :delivery_key""",
                    {"delivery_key": delivery_key},
                )
                return {
                    "status": "uncertain",
                    "delivery_key": delivery_key,
                }
            claim_token = secrets.token_hex(16)
            await database.execute(
                """UPDATE notification_delivery_attempts
                   SET status = 'sending', attempts = attempts + 1,
                       claim_token = :claim_token,
                       notify_log_id = COALESCE(notify_log_id, :notify_log_id),
                       config_revision = :config_revision,
                       last_error = NULL, uncertain_at = NULL
                 WHERE delivery_key = :delivery_key
                   AND status IN ('pending', 'failed')""",
                {
                    "delivery_key": delivery_key,
                    "claim_token": claim_token,
                    "notify_log_id": notify_log_id,
                    "config_revision": config_revision,
                },
            )
            return {
                "status": "sending",
                "delivery_key": delivery_key,
                "claim_token": claim_token,
            }
    except NotificationDeliveryContractError:
        raise
    except Exception as exc:
        if str(settings.deployment_mode or "").strip().lower() != "production":
            # Local/dev compatibility is intentionally explicit.  A
            # production process must have migration 0030 before it can send.
            return None
        raise NotificationDeliveryContractError(
            "notification_delivery_claim_unavailable"
        ) from exc


async def _finish_notification_delivery(
    claim: dict | None,
    *,
    success: bool,
    error: str | None = None,
    uncertain: bool = False,
) -> None:
    if not claim or claim.get("status") != "sending":
        return
    if success:
        await database.execute(
            """UPDATE notification_delivery_attempts
               SET status = 'sent', sent_at = NOW(), claim_token = NULL,
                   last_error = NULL
             WHERE delivery_key = :delivery_key
               AND status = 'sending' AND claim_token = :claim_token""",
            {
                "delivery_key": claim["delivery_key"],
                "claim_token": claim["claim_token"],
            },
        )
    elif uncertain:
        await database.execute(
            """UPDATE notification_delivery_attempts
               SET status = 'uncertain', uncertain_at = NOW(),
                   claim_token = NULL, last_error = :error
             WHERE delivery_key = :delivery_key
               AND status = 'sending' AND claim_token = :claim_token""",
            {
                "delivery_key": claim["delivery_key"],
                "claim_token": claim["claim_token"],
                "error": str(error or "notification_delivery_uncertain")[:512],
            },
        )
    else:
        await database.execute(
            """UPDATE notification_delivery_attempts
               SET status = 'failed', claim_token = NULL,
                   last_error = :error
             WHERE delivery_key = :delivery_key
               AND status = 'sending' AND claim_token = :claim_token""",
            {
                "delivery_key": claim["delivery_key"],
                "claim_token": claim["claim_token"],
                "error": str(error or "notification_delivery_failed")[:512],
            },
        )


async def _mark_replayed_notification_log(log_id: int, status: str) -> None:
    """Keep a duplicate/replay log truthful without invoking the provider."""

    try:
        if status == "sent":
            await database.execute(
                "UPDATE notify_logs SET success = 1 WHERE id = :id",
                {"id": log_id},
            )
        elif status == "uncertain":
            await database.execute(
                """UPDATE notify_logs
                      SET success = 0,
                          content = CONCAT(
                            content,
                            '\n\nERROR: notification_delivery_uncertain'
                          )
                    WHERE id = :id""",
                {"id": log_id},
            )
    except Exception:
        if str(settings.deployment_mode or "").strip().lower() == "production":
            raise


def _notification_revision_token(
    channel: str,
    revision: int,
    secret_values: list[tuple[str, str]],
) -> str:
    try:
        master_key = base64.b64decode(
            settings.encryption_key,
            validate=True,
        )
    except (binascii.Error, TypeError, ValueError) as exc:
        raise RuntimeError("notification_config_revision_key_invalid") from exc
    if len(master_key) != 32:
        raise RuntimeError("notification_config_revision_key_invalid")
    derived_key = hmac.new(
        master_key,
        NOTIFICATION_REVISION_HMAC_CONTEXT,
        hashlib.sha256,
    ).digest()
    digest = hmac.new(derived_key, digestmod=hashlib.sha256)
    for value in (channel, str(revision)):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    for key_name, secret in secret_values:
        for value in (key_name, secret):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return f"{revision}:{digest.hexdigest()}"


def notification_config_epoch(config_revision: str) -> int:
    try:
        epoch_text, digest = str(config_revision).split(":", 1)
        epoch = int(epoch_text)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("notification_config_revision_invalid") from exc
    if epoch <= 0 or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise RuntimeError("notification_config_revision_invalid")
    return epoch


async def notification_config_revision(channel: str) -> str | None:
    if channel not in CHANNEL_SECRET_KEYS:
        return None
    secret_values = []
    for key_name in CHANNEL_SECRET_KEYS[channel]:
        value = await secret_value(key_name)
        if not value:
            return None
        secret_values.append((key_name, value))
    row = await database.fetch_one(
        "SELECT revision FROM notification_channel_revisions WHERE channel = :channel",
        {"channel": channel},
    )
    if not row:
        raise RuntimeError("notification_config_revision_unavailable")
    revision = int(row["revision"] or 0)
    if revision <= 0:
        raise RuntimeError("notification_config_revision_invalid")
    return _notification_revision_token(
        channel,
        revision,
        secret_values,
    )


async def advance_notification_config_revision(channel: str) -> None:
    await database.execute(
        """INSERT INTO notification_channel_revisions (channel, revision)
           VALUES (:channel, 2)
           ON DUPLICATE KEY UPDATE
             revision = notification_channel_revisions.revision + 1,
             updated_at = CURRENT_TIMESTAMP""",
        {"channel": channel},
    )


async def _channel_item(channel: str, label: str) -> dict:
    revision = await notification_config_revision(channel)
    return {
        "id": channel,
        "label": label,
        "configured": revision is not None,
    }


@router.get("/channels")
async def list_channels():
    return [
        await _channel_item("serverchan", "ServerChan"),
        await _channel_item("feishu", "Feishu"),
        await _channel_item("webhook", "Webhook"),
        await _channel_item("telegram", "Telegram"),
    ]


@router.get("/config-guide")
async def notification_config_guide():
    channels = await list_channels()
    configured_by_id = {channel["id"]: channel["configured"] for channel in channels}
    guide = [
        {
            "id": "serverchan",
            "label": "ServerChan",
            "env": [{"name": "SERVERCHAN_KEY", "required": True, "configured": await secret_configured("SERVERCHAN_KEY")}],
            "env_example": "SERVERCHAN_KEY=<serverchan-send-key>",
            "test_payload": {"channel": "serverchan", "title": "DPMS test", "content": "ServerChan notification test"},
        },
        {
            "id": "feishu",
            "label": "Feishu",
            "env": [{"name": "FEISHU_WEBHOOK", "required": True, "configured": await secret_configured("FEISHU_WEBHOOK")}],
            "env_example": "FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<token>",
            "test_payload": {"channel": "feishu", "title": "DPMS test", "content": "Feishu notification test"},
        },
        {
            "id": "webhook",
            "label": "Webhook",
            "env": [{"name": "GENERIC_WEBHOOK_URL", "required": True, "configured": await secret_configured("GENERIC_WEBHOOK_URL")}],
            "env_example": "GENERIC_WEBHOOK_URL=https://example.com/dpms/webhook",
            "test_payload": {"channel": "webhook", "title": "DPMS test", "content": "Generic webhook notification test"},
        },
        {
            "id": "telegram",
            "label": "Telegram",
            "env": [
                {"name": "TELEGRAM_BOT_TOKEN", "required": True, "configured": await secret_configured("TELEGRAM_BOT_TOKEN")},
                {"name": "TELEGRAM_CHAT_ID", "required": True, "configured": await secret_configured("TELEGRAM_CHAT_ID")},
            ],
            "env_example": "TELEGRAM_BOT_TOKEN=<bot-token>\nTELEGRAM_CHAT_ID=<chat-id>",
            "test_payload": {"channel": "telegram", "title": "DPMS test", "content": "Telegram notification test"},
        },
    ]
    production_ready = any(configured_by_id.values())
    missing_by_channel = {
        channel["id"]: [
            env["name"]
            for env in channel["env"]
            if env["required"] and not env["configured"]
        ]
        for channel in guide
    }
    # Production requires any one complete channel, not every supported
    # channel. Keep the legacy flat field useful to existing clients by
    # returning only the smallest currently suggested option.
    minimum_channel = next(
        (
            item
            for item in guide
            if not configured_by_id.get(item["id"])
        ),
        None,
    )
    missing_required = (
        []
        if production_ready or minimum_channel is None
        else missing_by_channel[minimum_channel["id"]]
    )
    env_bundle = "\n\n".join(item["env_example"] for item in guide)
    minimum_env_bundle = (
        ""
        if production_ready or minimum_channel is None
        else minimum_channel["env_example"]
    )
    return {
        "configured_count": sum(1 for item in guide if configured_by_id.get(item["id"])),
        "required_channel_count": 1,
        "production_ready": production_ready,
        "missing_required": missing_required,
        "missing_by_channel": missing_by_channel,
        "env_bundle": env_bundle,
        "minimum_env_bundle": minimum_env_bundle,
        "channels": guide,
        "apply_steps": [
            "Edit .env with one or more notification channel values.",
            "Run docker compose up -d --build so core-api receives the new environment.",
            "Open Operations & Notify and send a manual notification test.",
            "Confirm the latest notify_logs row is Sent before relying on production alerts.",
        ],
        "test_endpoint": "POST /api/notify/send",
    }


@router.put("/secrets/{channel}")
async def save_notification_secrets(channel: str, payload: NotifySecretUpdate, request: Request):
    actor = require_min_role(request, "admin")
    if channel not in CHANNEL_SECRET_KEYS:
        raise HTTPException(400, detail="Unsupported notification channel")
    data = payload.model_dump(exclude_none=True)
    allowed = set(CHANNEL_SECRET_KEYS[channel])
    updates = {}
    for env_name, attr in SECRET_ATTRS.items():
        if env_name in allowed and attr in data:
            value = (data[attr] or "").strip()
            if value:
                validate_secret_value(env_name, value)
                updates[env_name] = value
    if not updates:
        raise HTTPException(400, detail="No secret value was provided for this channel")
    async with database.transaction():
        for env_name, value in updates.items():
            await save_secret(env_name, value)
        await advance_notification_config_revision(channel)
    missing = [env_name for env_name in CHANNEL_SECRET_KEYS[channel] if not await secret_configured(env_name)]
    await audit_event(
        request,
        action="notification_secret.save",
        resource_type="notification_channel",
        resource_id=channel,
        result="saved",
        risk_level="high",
        detail={"saved_keys": list(updates.keys()), "missing_required": missing},
    )
    await record_event(
        aggregate="notification_channel",
        aggregate_id=channel,
        event_type="NotificationSecretSaved",
        payload={
            "saved_keys": list(updates.keys()),
            "missing_required": missing,
            "configured": not missing,
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "saved",
        "channel": channel,
        "configured": not missing,
        "saved_keys": list(updates.keys()),
        "missing_required": missing,
    }


@router.put("/secrets")
async def save_notification_secret_bundle(payload: NotifySecretBundleUpdate, request: Request):
    actor = require_min_role(request, "admin")
    parsed = parse_secret_bundle(payload.content)
    if not parsed:
        raise HTTPException(400, detail="No supported notification secret was found")

    for env_name, value in parsed.items():
        validate_secret_value(env_name, value)

    affected_channels = [
        channel
        for channel, keys in CHANNEL_SECRET_KEYS.items()
        if any(key in parsed for key in keys)
    ]
    async with database.transaction():
        for env_name, value in parsed.items():
            await save_secret(env_name, value)
        for channel in affected_channels:
            await advance_notification_config_revision(channel)

    channel_states = {}
    for channel, keys in CHANNEL_SECRET_KEYS.items():
        if any(key in parsed for key in keys):
            missing = [key for key in keys if not await secret_configured(key)]
            channel_states[channel] = {
                "configured": not missing,
                "missing_required": missing,
            }

    await audit_event(
        request,
        action="notification_secret.bundle_save",
        resource_type="notification_channel",
        resource_id="bundle",
        result="saved",
        risk_level="high",
        detail={"saved_keys": list(parsed.keys()), "channels": channel_states},
    )
    await record_event(
        aggregate="notification_channel",
        aggregate_id="bundle",
        event_type="NotificationSecretBundleSaved",
        payload={"saved_keys": list(parsed.keys()), "channels": channel_states},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "saved",
        "saved_keys": list(parsed.keys()),
        "channels": channel_states,
        "configured_channels": [
            channel for channel, state in channel_states.items() if state["configured"]
        ],
    }


@router.delete("/secrets/{channel}")
async def clear_notification_secrets(channel: str, request: Request):
    actor = require_min_role(request, "admin")
    require_confirmation(request)
    if channel not in CHANNEL_SECRET_KEYS:
        raise HTTPException(400, detail="Unsupported notification channel")
    async with database.transaction():
        for key_name in CHANNEL_SECRET_KEYS[channel]:
            await database.execute(
                "DELETE FROM notification_secrets WHERE key_name = :key_name",
                {"key_name": key_name},
            )
        await advance_notification_config_revision(channel)
    await audit_event(
        request,
        action="notification_secret.clear",
        resource_type="notification_channel",
        resource_id=channel,
        result="cleared",
        risk_level="high",
    )
    await record_event(
        aggregate="notification_channel",
        aggregate_id=channel,
        event_type="NotificationSecretCleared",
        payload={"cleared_keys": CHANNEL_SECRET_KEYS[channel]},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "cleared", "channel": channel, "configured": await channel_configured(channel)}


@router.get("/status")
async def notification_status():
    channels = await list_channels()
    output = []
    for channel in channels:
        current_revision = await notification_config_revision(channel["id"])
        latest = await database.fetch_one(
            """SELECT id, title, content, success, config_revision, created_at
               FROM notify_logs
               WHERE channel = :channel
               ORDER BY id DESC
               LIMIT 1""",
            {"channel": channel["id"]},
        )
        item = dict(channel)
        public_latest = dict(latest) if latest else None
        if public_latest is not None:
            public_latest.pop("config_revision", None)
        item["last_log"] = public_latest
        revision_matches = bool(
            latest
            and latest["config_revision"]
            and latest["config_revision"] == current_revision
        )
        # A stored secret proves configuration only. Do not report a channel
        # healthy until at least one actual delivery has succeeded.
        item["healthy"] = bool(
            channel["configured"]
            and latest
            and latest["success"]
            and revision_matches
        )
        item["verification_required"] = bool(
            channel["configured"] and not revision_matches
        )
        item["last_error"] = extract_last_error(latest["content"]) if latest and revision_matches and not latest["success"] else None
        output.append(item)

    skipped = await database.fetch_one(
        """SELECT id, title, content, success, created_at
           FROM notify_logs
           WHERE channel = 'dispatch'
           ORDER BY id DESC
           LIMIT 1"""
    )
    return {
        "configured_count": sum(1 for channel in channels if channel["configured"]),
        "channels": output,
        "last_dispatch_skip": dict(skipped) if skipped else None,
    }


@router.get("/logs")
async def list_notify_logs(limit: int = 50):
    limit = min(max(int(limit or 50), 1), 200)
    rows = await database.fetch_all(
        "SELECT * FROM notify_logs ORDER BY id DESC LIMIT :limit",
        {"limit": limit},
    )
    items = []
    for row in rows:
        item = dict(row)
        item.pop("config_revision", None)
        content = str(item.get("content") or "")
        item["delivery_status"] = (
            "sent"
            if item.get("success")
            else "skipped"
            if item.get("channel") == "dispatch" and "SKIPPED:" in content
            else "failed"
        )
        items.append(item)
    return items


@router.post("/send")
async def send_notification(payload: NotifyRequest, background_tasks: BackgroundTasks, request: Request):
    actor = require_min_role(request, "operator")
    if payload.channel not in VALID_NOTIFICATION_CHANNELS:
        raise HTTPException(400, detail="Unsupported notification channel")
    config_revision = await notification_config_revision(payload.channel)
    if config_revision is None:
        raise HTTPException(
            409,
            detail=(
                f"Notification channel is not configured: {payload.channel}"
            ),
        )
    log_id = await database.execute(
        "INSERT INTO notify_logs (channel, title, content, success, config_revision) VALUES (:ch, :t, :c, 0, :config_revision)",
        {
            "ch": payload.channel,
            "t": payload.title,
            "c": payload.content,
            "config_revision": config_revision,
        },
    )
    await record_event(
        aggregate="notification_log",
        aggregate_id=log_id,
        event_type="NotificationQueued",
        payload={"channel": payload.channel, "title": payload.title},
        correlation_id=log_id,
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    background_tasks.add_task(
        dispatch_notification,
        log_id,
        payload.channel,
        payload.title,
        payload.content,
        config_revision,
    )
    return {"status": "queued", "log_id": log_id}


async def dispatch_notification(
    log_id: int,
    channel: str,
    title: str,
    content: str,
    config_revision: str,
    *,
    stream_message_id: str | None = None,
    delivery_key: str | None = None,
):
    claim = None
    sender_invoked = False
    provider_completed = False
    delivery_committed = False
    try:
        resolved_delivery_key = delivery_key or notification_delivery_key(
            channel,
            stream_message_id=stream_message_id,
            log_id=log_id,
        )
        claim = await _claim_notification_delivery(
            delivery_key=resolved_delivery_key,
            channel=channel,
            stream_message_id=stream_message_id,
            notify_log_id=log_id,
            config_revision=config_revision,
        )
        if claim and claim.get("status") in {"sent", "uncertain", "busy"}:
            # ``uncertain`` is deliberately terminal for automatic retries:
            # the external provider may already have accepted the request.
            # Operators can inspect/reconcile the attempt before any manual
            # resend, while a concurrent live sender gets a harmless no-op.
            await _mark_replayed_notification_log(
                log_id,
                str(claim.get("status")),
            )
            return
        if await notification_config_revision(channel) != config_revision:
            raise NotificationDeliveryContractError(
                "notification_config_revision_changed"
            )
        sender_invoked = True
        await SENDERS.get(channel, send_webhook)(title, content)
        # A successful return only proves that the provider accepted the
        # request; the following DB writes can still fail. From this point on,
        # any interruption is ambiguous and must never be exposed as a plain
        # retryable failure.
        provider_completed = True
        if await notification_config_revision(channel) != config_revision:
            raise NotificationDeliveryContractError(
                "notification_config_revision_changed"
            )
        updated = await execute_affected_rows(
            """UPDATE notify_logs AS logs
               JOIN notification_channel_revisions AS revisions
                 ON revisions.channel = logs.channel
                  SET logs.success = 1
                WHERE logs.id = :id
                  AND logs.config_revision = :config_revision
                  AND revisions.revision = :config_epoch""",
            {
                "id": log_id,
                "config_revision": config_revision,
                "config_epoch": notification_config_epoch(
                    config_revision
                ),
            },
        )
        if updated != 1:
            raise NotificationDeliveryContractError(
                "notification_config_revision_changed"
            )
        await _finish_notification_delivery(claim, success=True)
        delivery_committed = True
        await record_event(
            aggregate="notification_log",
            aggregate_id=log_id,
            event_type="NotificationSent",
            payload={"channel": channel, "title": title},
            correlation_id=log_id,
        )
    except Exception as exc:
        # A provider response and the durable sent receipt already committed;
        # an observability/EventStore failure must not turn a delivered
        # notification into a visible failure or invite an operator to resend.
        if delivery_committed:
            structured_log(
                "warning",
                "notification_delivery_observation_failed_after_commit",
                channel=channel,
                log_id=log_id,
                cause_type=type(exc).__name__,
            )
            return
        error_code = (
            "notification_delivery_uncertain"
            if provider_completed
            else notification_delivery_error_code(exc)
        )
        try:
            await _finish_notification_delivery(
                claim,
                success=False,
                error=error_code,
                uncertain=sender_invoked or provider_completed,
            )
        except Exception:
            # Keep the existing notify-log failure visible even if the
            # idempotency bookkeeping is temporarily unavailable.
            pass
        await database.execute(
            "UPDATE notify_logs SET success = 0, content = CONCAT(content, '\n\nERROR: ', :err) WHERE id = :id",
            {"id": log_id, "err": error_code},
        )
        await record_event(
            aggregate="notification_log",
            aggregate_id=log_id,
            event_type=(
                "NotificationDeliveryUncertain"
                if provider_completed
                else "NotificationFailed"
            ),
            payload={
                "channel": channel,
                "title": title,
                "error": error_code,
                "error_code": error_code,
            },
            correlation_id=log_id,
        )


def notification_delivery_error_code(exc: BaseException) -> str:
    """Return a useful failure class without persisting secret-bearing URLs.

    ``httpx`` exception text includes the request URL. Several supported
    providers carry their token or key in that URL, so exception messages must
    never be copied into notification logs or EventStore payloads.
    """

    if isinstance(exc, NotificationDeliveryContractError):
        return exc.code
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and 100 <= status_code <= 599:
            return f"notification_http_status:{status_code}"
        return "notification_http_status_error"
    if isinstance(exc, httpx.TimeoutException):
        return "notification_timeout"
    if isinstance(exc, httpx.RequestError):
        return "notification_transport_error"
    return f"notification_delivery_error:{type(exc).__name__[:64]}"


def _business_response(response: httpx.Response, channel: str) -> dict:
    if len(response.content) > NOTIFICATION_RESPONSE_MAX_BYTES:
        raise NotificationDeliveryContractError(
            f"notification_{channel}_response_invalid"
        )
    try:
        payload = response.json()
    except (ValueError, UnicodeError) as exc:
        raise NotificationDeliveryContractError(
            f"notification_{channel}_response_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise NotificationDeliveryContractError(
            f"notification_{channel}_response_invalid"
        )
    return payload


def _require_zero_code(response: httpx.Response, channel: str) -> None:
    payload = _business_response(response, channel)
    code = payload.get("code")
    if channel == "feishu" and code is None:
        code = payload.get("StatusCode")
    if isinstance(code, bool) or not isinstance(code, int):
        raise NotificationDeliveryContractError(
            f"notification_{channel}_response_invalid"
        )
    if code != 0:
        raise NotificationDeliveryContractError(
            f"notification_{channel}_business_rejected"
        )


async def send_serverchan(title: str, content: str):
    serverchan_key = await secret_value("SERVERCHAN_KEY")
    if not serverchan_key:
        raise ValueError("SERVERCHAN_KEY is not configured")
    url = f"https://sctapi.ftqq.com/{serverchan_key}.send"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data={"title": title, "desp": content}, timeout=10)
        response.raise_for_status()
        _require_zero_code(response, "serverchan")


async def send_feishu(title: str, content: str):
    feishu_webhook = await secret_value("FEISHU_WEBHOOK")
    if not feishu_webhook:
        raise ValueError("FEISHU_WEBHOOK is not configured")
    validate_secret_value("FEISHU_WEBHOOK", feishu_webhook)
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"content": title, "tag": "plain_text"}},
            "elements": [{"tag": "div", "text": {"content": content, "tag": "lark_md"}}],
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(feishu_webhook, json=payload, timeout=10)
        response.raise_for_status()
        _require_zero_code(response, "feishu")


async def send_webhook(title: str, content: str):
    generic_webhook_url = await secret_value("GENERIC_WEBHOOK_URL")
    if not generic_webhook_url:
        raise ValueError("GENERIC_WEBHOOK_URL is not configured")
    validate_secret_value("GENERIC_WEBHOOK_URL", generic_webhook_url)
    async with httpx.AsyncClient() as client:
        response = await client.post(generic_webhook_url, json={"title": title, "content": content}, timeout=10)
        response.raise_for_status()


async def send_telegram(title: str, content: str):
    telegram_bot_token = await secret_value("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = await secret_value("TELEGRAM_CHAT_ID")
    if not telegram_bot_token or not telegram_chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not configured")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={"chat_id": telegram_chat_id, "text": f"{title}\n\n{content}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = _business_response(response, "telegram")
        if payload.get("ok") is not True:
            if "ok" not in payload or not isinstance(payload["ok"], bool):
                raise NotificationDeliveryContractError(
                    "notification_telegram_response_invalid"
                )
            raise NotificationDeliveryContractError(
                "notification_telegram_business_rejected"
            )


SENDERS = {
    "serverchan": send_serverchan,
    "feishu": send_feishu,
    "webhook": send_webhook,
    "telegram": send_telegram,
}


def extract_last_error(content: str | None) -> str | None:
    if not content:
        return None
    marker = "ERROR:"
    if marker not in content:
        return None
    return content.rsplit(marker, 1)[-1].strip()


def parse_secret_bundle(content: str) -> dict[str, str]:
    supported = set(SECRET_ATTRS.keys())
    parsed = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in supported:
            continue
        value = value.strip().strip("'").strip('"')
        if value and not looks_like_placeholder_secret(value):
            parsed[key] = value
    return parsed


def looks_like_placeholder_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        lowered in {"...", "changeme", "change-me", "your-key", "your-secret"}
        or lowered.startswith("<") and lowered.endswith(">")
        or "<token>" in lowered
        or "<bot-token>" in lowered
        or "<chat-id>" in lowered
        or "<serverchan-send-key>" in lowered
        or "example.com" in lowered
    )


async def save_secret(key_name: str, value: str):
    encrypted = cookie_vault.encrypt(value, aad=notification_secret_aad(key_name))
    row = await database.fetch_one(
        "SELECT id FROM notification_secrets WHERE key_name = :key_name",
        {"key_name": key_name},
    )
    if row:
        await database.execute(
            "UPDATE notification_secrets SET encrypted_value = :encrypted_value, updated_at = NOW() WHERE key_name = :key_name",
            {"key_name": key_name, "encrypted_value": encrypted},
        )
        return
    await database.execute(
        "INSERT INTO notification_secrets (key_name, encrypted_value) VALUES (:key_name, :encrypted_value)",
        {"key_name": key_name, "encrypted_value": encrypted},
    )


async def secret_value(key_name: str) -> str:
    row = await database.fetch_one(
        "SELECT encrypted_value FROM notification_secrets WHERE key_name = :key_name",
        {"key_name": key_name},
    )
    if row and row["encrypted_value"]:
        return cookie_vault.decrypt(row["encrypted_value"], aad=notification_secret_aad(key_name))
    attr = SECRET_ATTRS.get(key_name)
    return getattr(settings, attr, "") if attr else ""


async def secret_configured(key_name: str) -> bool:
    return bool(await secret_value(key_name))


def validate_secret_value(key_name: str, value: str) -> None:
    if key_name in {"FEISHU_WEBHOOK", "GENERIC_WEBHOOK_URL"}:
        try:
            assert_public_http_url(value, require_https=True, resolve_dns=True)
        except ValueError as exc:
            raise HTTPException(400, detail=f"Invalid {key_name}: {exc}") from exc
    if len(value.encode("utf-8")) > 4096:
        raise HTTPException(400, detail=f"{key_name} is too large")


async def channel_configured(channel: str) -> bool:
    return all([await secret_configured(key_name) for key_name in CHANNEL_SECRET_KEYS[channel]])


async def configured_channels() -> list[str]:
    channels = []
    for channel in CHANNEL_SECRET_KEYS:
        if await channel_configured(channel):
            channels.append(channel)
    return channels
