"""Platform runtime identity and secret-scope contracts.

The control plane intentionally keeps an ``all`` identity.  A platform Core
runner or Worker, however, must not silently fall back to that identity in a
production deployment.  This module keeps the check dependency-free so both
images can apply the same fail-closed contract before opening a connection.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from shared.platform_ids import PLATFORM_IDS
from shared.platform_scope import exact_platform_scope
from shared.runtime_secrets import encryption_key_problem


PLATFORM_SECURITY_COMPAT = "compat"
PLATFORM_SECURITY_STRICT = "strict"
PLATFORM_SECURITY_MODES = frozenset(
    {PLATFORM_SECURITY_COMPAT, PLATFORM_SECURITY_STRICT}
)
def _env(environment: Mapping[str, str] | None, key: str) -> str:
    source = os.environ if environment is None else environment
    return str(source.get(key, "") or "").strip()


def scoped_env_name(prefix: str, platform: str) -> str:
    """Return the stable environment name for one exact platform."""

    selected = exact_platform_scope(platform)
    normalized_prefix = str(prefix or "").strip().upper()
    if not normalized_prefix or not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_prefix):
        raise ValueError("platform_security_env_prefix_invalid")
    return f"{normalized_prefix}_{selected.upper()}"


def normalize_platform_security_mode(value: str | None) -> str:
    mode = str(value or PLATFORM_SECURITY_COMPAT).strip().casefold()
    if mode not in PLATFORM_SECURITY_MODES:
        raise ValueError("platform_security_mode_invalid")
    return mode


def expected_platform_database_username(
    platform: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    selected = exact_platform_scope(platform)
    configured = _env(environment, scoped_env_name("MYSQL_RUNTIME_USER", selected))
    return configured or f"dpms_runtime_{selected}"


def expected_platform_redis_username(
    role: str,
    platform: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    selected = exact_platform_scope(platform)
    normalized_role = str(role or "").strip().casefold()
    if normalized_role not in {"core", "worker"}:
        raise ValueError("platform_security_redis_role_invalid")
    # Compose and the Redis entrypoint use REDIS_CORE_BILIBILI_USERNAME. Keep
    # the older REDIS_CORE_USERNAME_BILIBILI spelling as a read-only fallback
    # for rolling upgrades, but always prefer the canonical form.
    canonical_name = (
        f"REDIS_{normalized_role.upper()}_{selected.upper()}_USERNAME"
    )
    legacy_name = scoped_env_name(
        f"REDIS_{normalized_role.upper()}_USERNAME",
        selected,
    )
    configured = _env(environment, canonical_name) or _env(
        environment,
        legacy_name,
    )
    return configured or f"{normalized_role}-{selected}"


def scoped_encryption_key(
    platform: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    selected = exact_platform_scope(platform)
    return _env(environment, scoped_env_name("ENCRYPTION_KEY", selected))


def require_platform_runtime_identity(
    *,
    platform: str,
    role: str,
    deployment_mode: str | None,
    security_mode: str | None,
    database_username: str,
    redis_username: str,
    encryption_key: str | None,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Fail closed when an isolated production lane uses shared credentials.

    ``compat`` remains available for development and rolling preparation.  A
    production platform lane must explicitly opt into ``strict`` and provide
    all three scoped identities.  The database grant itself is provisioned by
    ``docker/mysql/provision-roles.sh``; this check prevents a miswired
    service from accidentally using the control-plane account.
    """

    selected = exact_platform_scope(platform)
    mode = normalize_platform_security_mode(security_mode)
    production = str(deployment_mode or "").strip().casefold() == "production"
    if not production and mode != PLATFORM_SECURITY_STRICT:
        return
    if production and mode != PLATFORM_SECURITY_STRICT:
        raise RuntimeError("platform_security_strict_mode_required")

    expected_db = expected_platform_database_username(
        selected,
        environment=environment,
    )
    if str(database_username or "").strip() != expected_db:
        raise RuntimeError("platform_database_identity_mismatch")

    expected_redis = expected_platform_redis_username(
        role,
        selected,
        environment=environment,
    )
    if str(redis_username or "").strip() != expected_redis:
        raise RuntimeError("platform_redis_identity_mismatch")

    configured_scoped_key = scoped_encryption_key(
        selected,
        environment=environment,
    )
    if not configured_scoped_key:
        raise RuntimeError("platform_encryption_key_missing")
    if not encryption_key or str(encryption_key) != configured_scoped_key:
        raise RuntimeError("platform_encryption_key_binding_mismatch")
    problem = encryption_key_problem(encryption_key)
    if problem:
        raise RuntimeError(f"platform_encryption_key_invalid:{problem}")


def platform_ids() -> tuple[str, ...]:
    """Expose the fixed platform list for provisioning and contract tests."""

    return tuple(PLATFORM_IDS)


__all__ = (
    "PLATFORM_SECURITY_COMPAT",
    "PLATFORM_SECURITY_MODES",
    "PLATFORM_SECURITY_STRICT",
    "expected_platform_database_username",
    "expected_platform_redis_username",
    "normalize_platform_security_mode",
    "platform_ids",
    "require_platform_runtime_identity",
    "scoped_encryption_key",
    "scoped_env_name",
)
