# DPMS v0.3.7 Schema Boundary

Date: 2026-06-21

## Goal

Move DPMS from startup-time schema repair toward migration-gated schema discipline.

This iteration does not repeat the v0.3.5 worker lease work or the v0.3.6 outbox work. It focuses on schema migration discipline, drift detection, and production fail-fast behavior.

## External research summary

Mature systems keep database schema changes in version-controlled migrations. Production database change needs discipline, testing, and rollback planning. Drift detection tools such as Bytebase and Atlas emphasize that the actual database state should be compared with the expected state before or during deployment.

## Implemented design

### GuardedDatabase

File: `core/app/db.py`

The database object is wrapped so production runtime paths cannot perform schema-changing writes directly. Such attempts are skipped and logged as `runtime_schema_write_skipped_in_production`.

The migration runner can explicitly enable schema writes by using `allow_schema_writes()`.

### Migration runner boundary

File: `core/app/migrations_runner.py`

`run_migrations()` now executes migration statements inside the schema-write context. In production, it also runs `verify_production_schema()` after migrations.

### Production schema verification

The verifier checks the presence of core tables, key columns, and the terminal outbox trigger. If the database is missing required structures, startup fails with `Production schema drift detected`.

### Static migration smoke script

File: `scripts/migration_smoke.py`

Run with:

```bash
python scripts/migration_smoke.py
```

It checks migration names, duplicate versions, empty SQL files, and trigger files incompatible with the current simple SQL splitter.

## Current limitation

`main.py` still contains legacy schema self-heal functions. This PR does not rewrite that large file. Instead, production writes from those legacy paths are suppressed, and migrations become the authoritative schema path.

## Next node

v0.3.8 should focus on browser context lifecycle: TTL, idle eviction, per-account memory attribution, and leak metrics.