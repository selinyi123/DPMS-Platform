"""Fail-closed production checks for role-scoped MySQL connection URLs.

The checks deliberately return stable problem codes and never include the URL,
username, or password in an exception. Database grants remain the authoritative
least-privilege boundary; these checks prevent the shipped development
credentials and an accidentally swapped migration role from reaching startup.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


DEFAULT_RUNTIME_DATABASE_USER = "dpms_runtime"
DEFAULT_MIGRATION_DATABASE_USER = "dpms_migrate"
DEFAULT_RUNTIME_DATABASE_PASSWORD = (
    "dpms-runtime-local-only-change-me-2026"
)
DEFAULT_MIGRATION_DATABASE_PASSWORD = (
    "dpms-migrate-local-only-change-me-2026"
)
LEGACY_DEFAULT_DATABASE_PASSWORD = "password"

DATABASE_ROLES = frozenset({"runtime", "migration"})
DATABASE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
DATABASE_PASSWORD_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
MIN_DATABASE_PASSWORD_LENGTH = 16
MAX_DATABASE_PASSWORD_LENGTH = 128
FORBIDDEN_PRODUCTION_DATABASE_PASSWORDS = frozenset(
    {
        DEFAULT_RUNTIME_DATABASE_PASSWORD,
        DEFAULT_MIGRATION_DATABASE_PASSWORD,
        LEGACY_DEFAULT_DATABASE_PASSWORD,
    }
)


def database_credential_problems(
    database_url: str | None,
    *,
    role: str,
    expected_username: str | None = None,
) -> tuple[str, ...]:
    """Return stable credential problem codes without exposing secret values."""

    normalized_role = str(role or "").strip().casefold()
    if normalized_role not in DATABASE_ROLES:
        raise ValueError("database_credential_role_invalid")

    value = str(database_url or "").strip()
    if not value:
        return ("database_url_missing",)
    try:
        parsed = urlsplit(value)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        hostname = parsed.hostname or ""
    except (TypeError, ValueError):
        return ("database_url_malformed",)

    problems: list[str] = []
    if parsed.scheme != "mysql+aiomysql":
        problems.append("database_scheme_invalid")
    if not hostname:
        problems.append("database_host_missing")
    if not username:
        problems.append("database_username_missing")
    elif not DATABASE_USERNAME_RE.fullmatch(username):
        problems.append("database_username_invalid")
    normalized_expected_username = str(
        expected_username or ""
    ).strip()
    if (
        normalized_expected_username
        and username
        and username != normalized_expected_username
    ):
        problems.append("database_role_username_mismatch")
    if not password:
        problems.append("database_password_missing")
    elif password in FORBIDDEN_PRODUCTION_DATABASE_PASSWORDS:
        problems.append("database_password_is_built_in_default")
    elif not (
        MIN_DATABASE_PASSWORD_LENGTH
        <= len(password)
        <= MAX_DATABASE_PASSWORD_LENGTH
    ):
        problems.append("database_password_length_invalid")
    elif not DATABASE_PASSWORD_RE.fullmatch(password):
        problems.append("database_password_characters_invalid")

    if (
        normalized_role == "runtime"
        and username == DEFAULT_MIGRATION_DATABASE_USER
    ):
        problems.append("runtime_uses_default_migration_role")
    if (
        normalized_role == "migration"
        and username == DEFAULT_RUNTIME_DATABASE_USER
    ):
        problems.append("migration_uses_default_runtime_role")
    return tuple(dict.fromkeys(problems))


def require_production_database_credentials(
    database_url: str | None,
    *,
    deployment_mode: str | None,
    role: str,
    expected_username: str | None = None,
) -> None:
    """Reject an unsafe connection URL before any production network access."""

    if str(deployment_mode or "").strip().casefold() != "production":
        return
    problems = database_credential_problems(
        database_url,
        role=role,
        expected_username=expected_username,
    )
    if problems:
        raise RuntimeError(
            "production_database_credentials_invalid:"
            + ",".join(problems)
        )
