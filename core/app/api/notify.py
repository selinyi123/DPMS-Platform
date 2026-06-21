import httpx

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import settings
from app.db import database
from app.event_store.service import record_event
from app.models.schemas import NotifyRequest, NotifySecretBundleUpdate, NotifySecretUpdate
from app.security import audit_event, require_confirmation, require_min_role
from app.utils.crypto import cookie_vault, notification_secret_aad
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


@router.get("/channels")
async def list_channels():
    return [
        {"id": "serverchan", "label": "ServerChan", "configured": await channel_configured("serverchan")},
        {"id": "feishu", "label": "Feishu", "configured": await channel_configured("feishu")},
        {"id": "webhook", "label": "Webhook", "configured": await channel_configured("webhook")},
        {
            "id": "telegram",
            "label": "Telegram",
            "configured": await channel_configured("telegram"),
        },
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
    missing_required = [
        env["name"]
        for channel in guide
        for env in channel["env"]
        if env["required"] and not env["configured"]
    ]
    env_bundle = "\n\n".join(item["env_example"] for item in guide)
    minimum_env_bundle = next((item["env_example"] for item in guide if not configured_by_id.get(item["id"])), guide[0]["env_example"])
    return {
        "configured_count": sum(1 for item in guide if configured_by_id.get(item["id"])),
        "required_channel_count": 1,
        "production_ready": any(configured_by_id.values()),
        "missing_required": missing_required,
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
    for env_name, value in updates.items():
        await save_secret(env_name, value)
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
        payload={"saved_keys": list(updates.keys()), "missing_required": missing, "configured": not missing},
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
        await save_secret(env_name, value)

    channel_states = {}
    for channel, keys in CHANNEL_SECRET_KEYS.items():
        if any(key in parsed for key in keys):
            missing = [key for key in keys if not await secret_configured(key)]
            channel_states[channel] = {"configured": not missing, "missing_required": missing}

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
    for key_name in CHANNEL_SECRET_KEYS[channel]:
        await database.execute(
            "DELETE FROM notification_secrets WHERE key_name = :key_name",
            {"key_name": key_name},
        )
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
        latest = await database.fetch_one(
            """SELECT id, title, content, success, created_at
               FROM notify_logs
               WHERE channel = :channel
               ORDER BY id DESC
               LIMIT 1""",
            {"channel": channel["id"]},
        )
        item = dict(channel)
        item["last_log"] = dict(latest) if latest else None
        item["healthy"] = bool(channel["configured"] and (not latest or latest["success"]))
        item["last_error"] = extract_last_error(latest["content"]) if latest and not latest["success"] else None
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
    return [dict(row) for row in rows]


@router.post("/send")
async def send_notification(payload: NotifyRequest, background_tasks: BackgroundTasks, request: Request):
    actor = require_min_role(request, "operator")
    if payload.channel not in VALID_NOTIFICATION_CHANNELS:
        raise HTTPException(400, detail="Unsupported notification channel")
    log_id = await database.execute(
        "INSERT INTO notify_logs (channel, title, content, success) VALUES (:ch, :t, :c, 0)",
        {"ch": payload.channel, "t": payload.title, "c": payload.content},
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
    background_tasks.add_task(dispatch_notification, log_id, payload.channel, payload.title, payload.content)
    return {"status": "queued", "log_id": log_id}


async def dispatch_notification(log_id: int, channel: str, title: str, content: str):
    try:
        await SENDERS.get(channel, send_webhook)(title, content)
        await database.execute("UPDATE notify_logs SET success = 1 WHERE id = :id", {"id": log_id})
        await record_event(
            aggregate="notification_log",
            aggregate_id=log_id,
            event_type="NotificationSent",
            payload={"channel": channel, "title": title},
            correlation_id=log_id,
        )
    except Exception as e:
        await database.execute(
            "UPDATE notify_logs SET success = 0, content = CONCAT(content, '\n\nERROR: ', :err) WHERE id = :id",
            {"id": log_id, "err": str(e)},
        )
        await record_event(
            aggregate="notification_log",
            aggregate_id=log_id,
            event_type="NotificationFailed",
            payload={"channel": channel, "title": title, "error": str(e)},
            correlation_id=log_id,
        )


async def send_serverchan(title: str, content: str):
    serverchan_key = await secret_value("SERVERCHAN_KEY")
    if not serverchan_key:
        raise ValueError("SERVERCHAN_KEY is not configured")
    url = f"https://sctapi.ftqq.com/{serverchan_key}.send"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data={"title": title, "desp": content}, timeout=10)
        response.raise_for_status()


async def send_feishu(title: str, content: str):
    feishu_webhook = await secret_value("FEISHU_WEBHOOK")
    if not feishu_webhook:
        raise ValueError("FEISHU_WEBHOOK is not configured")
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
    if key_name == "GENERIC_WEBHOOK_URL":
        try:
            assert_public_http_url(value, require_https=True, resolve_dns=True)
        except ValueError as exc:
            raise HTTPException(400, detail=f"Invalid GENERIC_WEBHOOK_URL: {exc}") from exc
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
