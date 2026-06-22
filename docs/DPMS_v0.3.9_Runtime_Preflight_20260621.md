# DPMS v0.3.9 Runtime Preflight Validation

Date: 2026-06-21

## Goal

Convert repeated "not run" risks into explicit preflight checks before container and browser smoke testing.

This iteration does not repeat the v0.3.5 recovery work, v0.3.6 outbox work, v0.3.7 schema boundary work, or v0.3.8 browser lifecycle work. It adds a static validation gate above those components.

## External research summary

Docker Compose supports service startup order based on health checks. GitHub Actions service containers support health options. Playwright documents that contexts should be closed and that persistent contexts are tied to a user data directory. These patterns support a staged readiness model:

1. static preflight;
2. container config validation;
3. service health readiness;
4. browser lifecycle smoke;
5. controlled shadow-run readiness.

## Implemented design

### Static runtime preflight

File: `scripts/runtime_preflight.py`

Run:

```bash
python scripts/runtime_preflight.py
```

The script checks:

- migration smoke still passes;
- required migration files exist;
- production schema boundary wiring exists;
- browser lifecycle wiring exists;
- worker main wires the context reaper and shutdown cleanup;
- VERSION reflects v0.3.9 and gated real-run status.

## Safety scope

The preflight script does not:

- start browsers;
- open pages;
- login accounts;
- dispatch tasks;
- perform platform actions.

It is a structural gate only.

## Current limitation

This is not a full integration test. It does not replace Docker Compose smoke, MySQL migration execution, Redis readiness, or Playwright browser startup smoke.

## Next node

v0.3.10 should move to Container Runtime Smoke Node:

- docker compose config validation;
- MySQL / Redis health readiness;
- worker startup/shutdown smoke;
- browser lifecycle smoke in a controlled environment.