"""Dependency-free secret validation shared by isolated runtime processes."""

from __future__ import annotations

import base64


def encryption_key_problem(value: str | None) -> str | None:
    if not value:
        return "encryption_key_missing"
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return "encryption_key_invalid_base64"
    if len(raw) != 32:
        return "encryption_key_wrong_length"
    return None


def require_production_encryption_key(
    value: str | None,
    *,
    deployment_mode: str | None,
) -> None:
    if str(deployment_mode or "").strip().casefold() != "production":
        return
    problem = encryption_key_problem(value)
    if problem:
        raise RuntimeError(
            f"production_encryption_key_invalid:{problem}"
        )
