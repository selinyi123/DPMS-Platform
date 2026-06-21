# DPMS v0.3.10 Container Runtime Smoke

Date: 2026-06-21

## Goal

Move from static repository preflight to container runtime smoke preparation without dispatching platform tasks.

This iteration does not repeat v0.3.5 recovery, v0.3.6 outbox, v0.3.7 schema boundary, v0.3.8 browser lifecycle, or v0.3.9 runtime preflight. It focuses on Docker Compose service contracts and safe container readiness checks.

## External research summary

Docker Compose provides `docker compose config` to validate and render the Compose model. Compose also supports healthcheck-based dependency ordering through `depends_on.condition: service_healthy`. Testcontainers wait strategies and GitHub Actions service container health options reinforce the same staged readiness model: validate configuration first, then wait for service health, then run higher-level smoke tests.

## Implemented design

### Container runtime smoke script

File: `scripts/container_runtime_smoke.py`

Run:

```bash
python scripts/container_runtime_smoke.py
```

Static-only mode:

```bash
python scripts/container_runtime_smoke.py --skip-docker
```

The script checks:

- required services exist: nginx, core-api, worker, mysql, redis;
- each service has a healthcheck with interval, timeout, retries;
- nginx depends on healthy core-api;
- core-api depends on healthy mysql and redis;
- worker depends on healthy core-api;
- worker keeps Chromium-specific shm sizing and no-new-privileges;
- worker does not run privileged and does not add SYS_ADMIN;
- browser profile and release volumes remain mounted;
- if Docker is available, `docker compose config --quiet` passes.

## Safety scope

The script does not:

- start containers by default;
- start browsers;
- open pages;
- login accounts;
- dispatch tasks;
- perform platform actions.

## Current limitation

This is still not a full runtime integration test. It verifies Compose contract and optional `docker compose config --quiet`, but does not yet run `docker compose up`, wait on health, or perform worker startup/shutdown smoke.

## Next node

v0.3.11 should add Controlled Worker Lifecycle Smoke:

- `docker compose up -d mysql redis core-api worker` in a controlled test path;
- wait for health;
- inspect worker heartbeat;
- shutdown cleanly;
- do not login accounts;
- do not run platform actions.