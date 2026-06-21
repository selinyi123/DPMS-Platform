#!/usr/bin/env python3
"""Static migration smoke checks for DPMS.

This script is dependency-free and does not connect to MySQL. It validates the
migration file contract that the lightweight runner depends on: ordered unique
4-digit versions, non-empty SQL files, and statement splitting that will not
silently ignore a migration.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "core" / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    for chunk in sql.split(";"):
        lines = [line for line in chunk.splitlines() if not line.strip().startswith("--")]
        cleaned = "\n".join(lines).strip()
        if cleaned:
            statements.append(cleaned)
    return statements


def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"missing migrations dir: {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    errors: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if not match:
            errors.append(f"bad migration filename: {path.name}")
            continue
        version = match.group(1)
        if version in seen:
            errors.append(f"duplicate migration version: {version}")
        seen.add(version)
        sql = path.read_text(encoding="utf-8")
        statements = split_statements(sql)
        if not statements:
            errors.append(f"empty migration: {path.name}")
        if "CREATE TRIGGER" in sql.upper() and "BEGIN" in sql.upper():
            errors.append(f"multi-statement trigger not supported by current runner: {path.name}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"ok: {len(seen)} migrations validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
