# DPMS v0.3.11 Controlled Worker Lifecycle Smoke

Date: 2026-06-21

## Goal

Move from Compose contract checking to an explicit, controlled worker lifecycle smoke harness.

This iteration does not repeat v0.3.5 recovery, v0.3.6 outbox, v0.3.7 schema boundary, v0.3.8 browser lifecycle, v0.3.9 runtime preflight, or v0.3.10 container smoke. It adds a guarded up/down harness for container lifecycle validation.

## External research summary

Docker Compose project names isolate the resources created for a Compose application. Healthcheck-based readiness, Compose config validation, and wait-strategy patterns support a staged smoke model: validate configuration, start a minimal service set, wait for service health, verify an internal readiness signal, then shut down and remove test volumes.

## Implemented design

### Controlled worker lifecycle smoke harness

File: `scripts/controlled_worker_lifecycle_smoke.py`

Default dry-run:

```bash
python scripts/controlled_worker_lifecycle_smoke.py
```

Explicit execution:

```bash
python scripts/controlled_worker_lifecycle_smoke.py --execute --project-name dpms-worker-smoke
```

The harness executes this sequence only when `--execute` is passed:

1. run `scripts/runtime_preflight.py`;
2. run `scripts/container_runtime_smoke.py`;
3. run `docker compose config --quiet`;
4. start only mysql, redis, core-api, worker;
5. wait for those services to become healthy;
6. verify `/tmp/worker-health` inside the worker container;
7. run `docker compose down -v --remove-orphans`.

## Safety scope

The harness does not:

- create accounts;
- login accounts;
- enqueue lottery tasks;
- open platform pages;
- perform platform actions.

It also uses an explicit Compose project name and removes project volumes on shutdown.

## Fix included

`runtime_preflight.py` no longer hard-codes `Product Version: 0.3.9`. It now checks that VERSION contains a `Product Version: 0.3.x` value plus the gated / not-ready runtime status. This prevents future version bumps from breaking the preflight gate.

## Current limitation

This harness is still opt-in and was not executed in this PR. It does not yet run a browser launch/close smoke inside the worker container.

## Next node

v0.3.12 should add Controlled Browser Lifecycle Smoke:

- launch and close a browser inside worker container;
- do not login accounts;
- do not open platform pages;
- do not enqueue tasks;
- assert browser process cleanup.