"""Shared, platform-neutral helpers for real-run evidence validation."""

from __future__ import annotations

import json
from typing import Any, Mapping


class RealRunGateBlocked(RuntimeError):
    """Raised when a worker cannot prove that a real action is still allowed."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        message = f"real_run_gate_blocked:{code}"
        if detail:
            message = f"{message}:{detail}"
        super().__init__(message)


def row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read one database-row value without depending on a driver row type."""

    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def required_int(value: Any, *, code: str) -> int:
    """Coerce an authoritative integer or raise the caller's stable gate code."""

    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked(code) from exc


def json_object(value: Any, *, code: str) -> dict[str, Any]:
    """Decode a database JSON object using fail-closed gate semantics."""

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RealRunGateBlocked(code) from exc
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RealRunGateBlocked(code) from exc
        if isinstance(parsed, dict):
            return parsed
    raise RealRunGateBlocked(code)


def contains_secret_material(value: Any) -> bool:
    """Detect secret-bearing keys in non-secret calibration evidence."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key or "").strip().casefold()
            if normalized in {
                "access_token",
                "refresh_token",
                "client_secret",
                "oauth_token",
            }:
                return True
            if contains_secret_material(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(contains_secret_material(item) for item in value)
    return False
