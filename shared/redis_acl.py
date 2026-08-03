"""Redis ACL startup contract shared by Core and Worker.

The application identity is allowed to inspect its own effective permissions
with ``ACL WHOAMI`` / ``ACL DRYRUN``.  Production startup then proves both the
required role and the denial of destructive commands without executing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from shared.platform_ids import PLATFORM_IDS
from shared.redis_consumer_groups import (
    RedisConsumerGroupTopologyError,
    runtime_consumer_group_specs,
    verify_redis_consumer_group_topology,
)


MIN_REDIS_PASSWORD_LENGTH = 24
DEFAULT_DEV_REDIS_PASSWORDS = frozenset(
    {
        "dpms-core-local-only-change-me-2026",
        "dpms-worker-local-only-change-me-2026",
        "dpms-health-local-only-change-me-2026",
    }
)
FORBIDDEN_REDIS_COMMANDS = (
    ("FLUSHDB",),
    ("FLUSHALL",),
    (
        "XGROUP",
        "CREATE",
        "lottery_tasks:bilibili",
        "workers:bilibili",
        "0",
        "MKSTREAM",
    ),
    (
        "XGROUP",
        "DESTROY",
        "lottery_tasks:bilibili",
        "workers:bilibili",
    ),
    (
        "XGROUP",
        "SETID",
        "lottery_tasks:bilibili",
        "workers:bilibili",
        "0-0",
    ),
    ("DEL", "lottery_tasks:bilibili"),
)
REQUIRED_REDIS_COMMANDS_BY_ROLE = {
    "core": (
        ("PING",),
        ("INFO", "SERVER"),
        (
            "XREADGROUP",
            "GROUP",
            "workers:bilibili",
            "acl-preflight",
            "COUNT",
            "1",
            "STREAMS",
            "lottery_tasks:bilibili",
            ">",
        ),
        ("XADD", "lottery_tasks:bilibili", "*", "preflight", "1"),
        (
            "XADD",
            "discovery_scan_requests:v1:bilibili",
            "*",
            "preflight",
            "1",
        ),
        ("XADD", "login_requests", "*", "preflight", "1"),
        (
            "XREADGROUP",
            "GROUP",
            "discovery-platform-runners:v1:bilibili",
            "acl-preflight",
            "COUNT",
            "1",
            "STREAMS",
            "discovery_scan_requests:v1:bilibili",
            ">",
        ),
        (
            "XACK",
            "discovery_scan_requests:v1:bilibili",
            "discovery-platform-runners:v1:bilibili",
            "0-1",
        ),
        (
            "XDEL",
            "discovery_scan_requests:v1:bilibili",
            "0-1",
        ),
        (
            "XCLAIM",
            "discovery_scan_requests:v1:bilibili",
            "discovery-platform-runners:v1:bilibili",
            "acl-preflight",
            "1",
            "0-1",
        ),
        (
            "XPENDING",
            "discovery_scan_requests:v1:bilibili",
            "discovery-platform-runners:v1:bilibili",
        ),
        ("XACK", "lottery_tasks:bilibili", "workers:bilibili", "0-1"),
        ("XDEL", "lottery_tasks:bilibili", "0-1"),
        ("XLEN", "lottery_tasks:bilibili"),
        ("XRANGE", "lottery_tasks:bilibili", "-", "+", "COUNT", "1"),
        (
            "XCLAIM",
            "lottery_tasks:bilibili",
            "workers:bilibili",
            "acl-preflight",
            "1",
            "0-1",
        ),
        ("XPENDING", "lottery_tasks:bilibili", "workers:bilibili"),
        ("XINFO", "GROUPS", "lottery_tasks:bilibili"),
        (
            "XINFO",
            "CONSUMERS",
            "lottery_tasks:bilibili",
            "workers:bilibili",
        ),
        ("EVAL", "return 1", "1", "lottery_tasks:bilibili"),
        ("GET", "dpms:task-stream:continuity:v1"),
        ("SET", "dpms:task-stream:continuity:v1", "acl-preflight"),
        (
            "GET",
            "discovery_scan_result:v1:acl-preflight:bilibili",
        ),
        (
            "SET",
            "discovery_scan_result:v1:acl-preflight:bilibili",
            "1",
            "EX",
            "60",
        ),
        (
            "DEL",
            "discovery_scan_result:v1:acl-preflight:bilibili",
        ),
        (
            "XADD",
            "xiaohongshu_target_pursuit_requests:v1",
            "*",
            "preflight",
            "1",
        ),
        (
            "GET",
            "xiaohongshu_target_pursuit_result:v1:"
            "00000000-0000-0000-0000-000000000000",
        ),
        (
            "DEL",
            "xiaohongshu_target_pursuit_result:v1:"
            "00000000-0000-0000-0000-000000000000",
        ),
        ("INCR", "daily_limit:acl-preflight"),
        ("EXPIRE", "daily_limit:acl-preflight", "60"),
        ("DEL", "recovery_count:acl-preflight"),
        ("SADD", "legacy_task_fanout:acl-preflight", "member"),
        ("SISMEMBER", "legacy_task_fanout:acl-preflight", "member"),
        ("SMEMBERS", "legacy_task_fanout:acl-preflight"),
        ("SREM", "legacy_task_fanout:acl-preflight", "member"),
        ("SCARD", "legacy_task_fanout:acl-preflight"),
        ("PUBLISH", "structured_logs", "acl-preflight"),
        ("PUBLISH", "worker:reload", "acl-preflight"),
    ),
    "worker": (
        ("PING",),
        (
            "XREADGROUP",
            "GROUP",
            "workers:bilibili",
            "acl-preflight",
            "COUNT",
            "1",
            "STREAMS",
            "lottery_tasks:bilibili",
            ">",
        ),
        ("XADD", "notify_events", "*", "preflight", "1"),
        ("XACK", "lottery_tasks:bilibili", "workers:bilibili", "0-1"),
        ("XDEL", "lottery_tasks:bilibili", "0-1"),
        (
            "XCLAIM",
            "lottery_tasks:bilibili",
            "workers:bilibili",
            "acl-preflight",
            "1",
            "0-1",
        ),
        ("XPENDING", "lottery_tasks:bilibili", "workers:bilibili"),
        ("XINFO", "GROUPS", "lottery_tasks:bilibili"),
        (
            "XINFO",
            "CONSUMERS",
            "lottery_tasks:bilibili",
            "workers:bilibili",
        ),
        ("EVAL", "return 1", "1", "lottery_tasks:bilibili"),
        ("GET", "account_calibration_legacy_fanout:acl-preflight"),
        (
            "SET",
            "account_calibration_requeue:acl-preflight",
            "1",
            "EX",
            "60",
        ),
        ("DEL", "account_calibration_requeue:acl-preflight"),
        ("ZADD", "risk_window:acl-preflight", "1", "member"),
        ("ZREMRANGEBYSCORE", "risk_window:acl-preflight", "-inf", "0"),
        ("ZCARD", "risk_window:acl-preflight"),
        ("EXPIRE", "risk_window:acl-preflight", "60"),
        ("SADD", "legacy_task_fanout:acl-preflight", "member"),
        ("SISMEMBER", "legacy_task_fanout:acl-preflight", "member"),
        ("SMEMBERS", "legacy_task_fanout:acl-preflight"),
        ("SREM", "legacy_task_fanout:acl-preflight", "member"),
        ("SCARD", "legacy_task_fanout:acl-preflight"),
        ("DEL", "legacy_task_fanout:acl-preflight"),
        ("SUBSCRIBE", "worker:reload"),
    ),
}


_ACL_PLATFORM_SENTINEL = "bilibili"
_GLOBAL_REQUIRED_REDIS_COMMANDS_BY_ROLE = {
    "core": frozenset(
        {
            ("PING",),
            ("INFO", "SERVER"),
            ("GET", "dpms:task-stream:continuity:v1"),
            (
                "SET",
                "dpms:task-stream:continuity:v1",
                "acl-preflight",
            ),
        }
    ),
    "worker": frozenset(
        {
            ("PING",),
            ("XADD", "notify_events", "*", "preflight", "1"),
            (
                "SET",
                "account_calibration_requeue:acl-preflight",
                "1",
                "EX",
                "60",
            ),
            ("DEL", "account_calibration_requeue:acl-preflight"),
            ("ZADD", "risk_window:acl-preflight", "1", "member"),
            (
                "ZREMRANGEBYSCORE",
                "risk_window:acl-preflight",
                "-inf",
                "0",
            ),
            ("ZCARD", "risk_window:acl-preflight"),
            ("EXPIRE", "risk_window:acl-preflight", "60"),
            ("SUBSCRIBE", "worker:reload"),
        }
    ),
}


def _command_mentions_platform_sentinel(command: tuple[str, ...]) -> bool:
    return any(
        _ACL_PLATFORM_SENTINEL in str(argument)
        for argument in command
    )


def _command_for_platform(
    command: tuple[str, ...],
    platform: str,
) -> tuple[str, ...]:
    return tuple(
        str(argument).replace(_ACL_PLATFORM_SENTINEL, platform)
        for argument in command
    )


def _normalize_acl_platforms(platforms) -> tuple[str, ...]:
    if platforms is None:
        # Preserve the original public API for direct callers while every
        # production entrypoint passes its validated runtime scope explicitly.
        return (_ACL_PLATFORM_SENTINEL,)
    if isinstance(platforms, str):
        raw = tuple(
            item.strip().casefold()
            for item in platforms.split(",")
            if item.strip()
        )
    else:
        raw = tuple(
            str(item or "").strip().casefold()
            for item in platforms
            if str(item or "").strip()
        )
    if raw == ("all",):
        return PLATFORM_IDS
    unknown = sorted(set(raw) - set(PLATFORM_IDS))
    if unknown:
        raise RedisACLPreflightError(
            "redis_acl_platform_scope_unsupported"
        )
    selected = set(raw)
    return tuple(
        platform for platform in PLATFORM_IDS if platform in selected
    )


def required_redis_commands_for_scope(
    role: str,
    *,
    platforms=None,
    include_shared: bool,
) -> tuple[tuple[str, ...], ...]:
    """Build the exact dry-run contract for one runtime ownership scope."""

    selected = _normalize_acl_platforms(platforms)
    templates = REQUIRED_REDIS_COMMANDS_BY_ROLE.get(role)
    if templates is None:
        raise RedisACLPreflightError(
            "redis_acl_required_command_contract_missing"
        )
    commands: list[tuple[str, ...]] = []
    global_commands = _GLOBAL_REQUIRED_REDIS_COMMANDS_BY_ROLE[role]
    for command in templates:
        if _command_mentions_platform_sentinel(command):
            commands.extend(
                _command_for_platform(command, platform)
                for platform in selected
            )
        elif include_shared or command in global_commands:
            commands.append(command)
    topology_specs = runtime_consumer_group_specs(
        role,
        platforms=selected,
        include_shared=include_shared,
    )
    for spec in topology_specs:
        commands.extend(
            (
                ("XINFO", "GROUPS", spec.stream_key),
                (
                    "XREADGROUP",
                    "GROUP",
                    spec.group_name,
                    "acl-preflight",
                    "COUNT",
                    "1",
                    "STREAMS",
                    spec.stream_key,
                    ">",
                ),
            )
        )
        if role == "worker":
            commands.append(
                (
                    "XGROUP",
                    "DELCONSUMER",
                    spec.stream_key,
                    spec.group_name,
                    "acl-preflight-stale",
                )
            )
        elif (
            role == "core"
            and spec.subsystem in {"notification", "discovery_scan"}
        ):
            commands.append(
                (
                    "XGROUP",
                    "DELCONSUMER",
                    spec.stream_key,
                    spec.group_name,
                    "acl-preflight-stale",
                )
            )
    if role == "worker" and "xiaohongshu" in selected:
        commands.append(
            (
                "SET",
                "xiaohongshu_target_pursuit_result:v1:"
                "00000000-0000-0000-0000-000000000000",
                "1",
                "EX",
                "60",
            )
        )
    if not commands:
        raise RedisACLPreflightError("redis_acl_scope_empty")
    return tuple(dict.fromkeys(commands))


def forbidden_redis_commands_for_scope(
    *,
    platforms=None,
    include_shared: bool,
) -> tuple[tuple[str, ...], ...]:
    """Build destructive denials without probing another platform's keys."""

    selected = _normalize_acl_platforms(platforms)
    commands: list[tuple[str, ...]] = [
        ("FLUSHDB",),
        ("FLUSHALL",),
    ]
    if include_shared:
        commands.extend(
            (
                (
                    "XGROUP",
                    "CREATE",
                    "lottery_tasks",
                    "workers",
                    "0",
                    "MKSTREAM",
                ),
                ("XGROUP", "DESTROY", "lottery_tasks", "workers"),
                (
                    "XGROUP",
                    "SETID",
                    "lottery_tasks",
                    "workers",
                    "0-0",
                ),
                ("DEL", "lottery_tasks"),
            )
        )
    platform_templates = tuple(
        command
        for command in FORBIDDEN_REDIS_COMMANDS
        if _command_mentions_platform_sentinel(command)
    )
    for platform in selected:
        commands.extend(
            _command_for_platform(command, platform)
            for command in platform_templates
        )
    return tuple(dict.fromkeys(commands))


@dataclass(frozen=True)
class RedisACLIdentity:
    username: str
    password: str


class RedisACLPreflightError(RuntimeError):
    """Raised when the configured Redis identity is absent or over-privileged."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def configured_redis_acl_identity(
    redis_url: str,
    *,
    configured_username: str = "",
    configured_password: str = "",
) -> RedisACLIdentity:
    """Resolve credentials without ever including the password in an error."""

    try:
        parsed = urlsplit(str(redis_url or ""))
    except ValueError as exc:
        raise RedisACLPreflightError("redis_acl_url_invalid") from exc
    if parsed.scheme not in {"redis", "rediss"}:
        raise RedisACLPreflightError("redis_acl_url_invalid")
    username = str(configured_username or "").strip()
    password = str(configured_password or "")
    if not username and parsed.username is not None:
        username = unquote(parsed.username).strip()
    if not password and parsed.password is not None:
        password = unquote(parsed.password)
    return RedisACLIdentity(username=username, password=password)


def validate_redis_acl_credentials(
    redis_url: str,
    *,
    expected_username: str,
    configured_username: str = "",
    configured_password: str = "",
    reject_development_passwords: bool,
) -> RedisACLIdentity:
    identity = configured_redis_acl_identity(
        redis_url,
        configured_username=configured_username,
        configured_password=configured_password,
    )
    if identity.username != expected_username:
        raise RedisACLPreflightError("redis_acl_username_mismatch")
    if not identity.password:
        raise RedisACLPreflightError("redis_acl_password_missing")
    if reject_development_passwords and (
        len(identity.password) < MIN_REDIS_PASSWORD_LENGTH
        or identity.password in DEFAULT_DEV_REDIS_PASSWORDS
    ):
        raise RedisACLPreflightError("redis_acl_password_insecure")
    return identity


def _dryrun_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip().casefold()


def _dryrun_allowed(value) -> bool:
    return _dryrun_text(value) == "ok"


def _dryrun_denied(value) -> bool:
    text = _dryrun_text(value)
    return bool(
        text
        and text != "ok"
        and (
            "no permission" in text
            or "noperm" in text
            or "not allowed" in text
        )
    )


async def verify_redis_acl(
    redis_client,
    *,
    redis_url: str,
    expected_username: str,
    role: str | None = None,
    configured_username: str = "",
    configured_password: str = "",
    reject_development_passwords: bool,
    platforms=None,
    include_shared: bool = True,
) -> None:
    """Fail closed unless the connected role denies destructive operations."""

    identity = validate_redis_acl_credentials(
        redis_url,
        expected_username=expected_username,
        configured_username=configured_username,
        configured_password=configured_password,
        reject_development_passwords=reject_development_passwords,
    )
    try:
        actual_username = str(
            await redis_client.execute_command("ACL", "WHOAMI")
        ).strip()
    except Exception as exc:
        raise RedisACLPreflightError(
            "redis_acl_whoami_unavailable"
        ) from exc
    if actual_username != identity.username:
        raise RedisACLPreflightError("redis_acl_authenticated_user_mismatch")

    # The concrete ACL identity and the command contract are different for
    # platform lanes (for example ``core-bilibili`` still uses the ``core``
    # contract). Keep the prefix fallback for older direct callers while
    # making production call sites pass the role explicitly.
    contract_role = str(role or expected_username).strip().casefold()
    if contract_role not in REQUIRED_REDIS_COMMANDS_BY_ROLE:
        contract_role = contract_role.split("-", 1)[0]
    required_commands = required_redis_commands_for_scope(
        contract_role,
        platforms=platforms,
        include_shared=include_shared,
    )
    for command in required_commands:
        try:
            result = await redis_client.execute_command(
                "ACL",
                "DRYRUN",
                actual_username,
                *command,
            )
        except Exception as exc:
            if _dryrun_denied(exc):
                raise RedisACLPreflightError(
                    "redis_acl_required_command_denied"
                ) from exc
            raise RedisACLPreflightError(
                "redis_acl_dryrun_unavailable"
            ) from exc
        if not _dryrun_allowed(result):
            raise RedisACLPreflightError(
                "redis_acl_required_command_denied"
            )

    forbidden_commands = forbidden_redis_commands_for_scope(
        platforms=platforms,
        include_shared=include_shared,
    )
    for command in forbidden_commands:
        try:
            result = await redis_client.execute_command(
                "ACL",
                "DRYRUN",
                actual_username,
                *command,
            )
        except Exception as exc:
            if _dryrun_denied(exc):
                continue
            raise RedisACLPreflightError(
                "redis_acl_dryrun_unavailable"
            ) from exc
        if not _dryrun_denied(result):
            raise RedisACLPreflightError(
                "redis_acl_destructive_command_allowed"
            )
    try:
        await verify_redis_consumer_group_topology(
            redis_client,
            role=contract_role,
            platforms=_normalize_acl_platforms(platforms),
            include_shared=include_shared,
        )
    except RedisConsumerGroupTopologyError as exc:
        raise RedisACLPreflightError(exc.code) from exc


__all__ = [
    "DEFAULT_DEV_REDIS_PASSWORDS",
    "FORBIDDEN_REDIS_COMMANDS",
    "MIN_REDIS_PASSWORD_LENGTH",
    "REQUIRED_REDIS_COMMANDS_BY_ROLE",
    "RedisACLIdentity",
    "RedisACLPreflightError",
    "configured_redis_acl_identity",
    "forbidden_redis_commands_for_scope",
    "required_redis_commands_for_scope",
    "validate_redis_acl_credentials",
    "verify_redis_acl",
]
