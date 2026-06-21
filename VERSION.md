# DPMS Version Ledger

## Current Product Snapshot

```text
Product Version: 0.3.9
Architecture Stage: S11 / Runtime Preflight Validation
Runtime Stage: Shadow-run Closed Loop + Migration-Gated Reliability Baseline + Managed Browser Context Pool + Static Preflight Gate
Real-run Status: Gated / Calibration Required
Production Readiness: Not Ready
Primary Platform: Bilibili first, other platforms remain plugin/calibration tracks
```

## Current Capability Boundary

DPMS currently targets a compliant, operator-gated automation runtime:

1. Import or QR-login accounts.
2. Calibrate account login health.
3. Import or discover lottery targets.
4. Parse and review action plans.
5. Run dry-run and shadow-run with evidence capture.
6. Gate real-run behind selector calibration, policy decisions, circuit breakers, confirmation, and runtime settings.
7. Record evidence, events, notifications, audit logs, policy decisions, transition history, recovery state, dead-letter state, and terminal outbox events.
8. Enforce production schema changes through versioned migrations rather than startup self-heal DDL.
9. Manage persistent browser contexts with TTL, idle eviction, capacity control, and per-account memory attribution.
10. Run static runtime preflight before container/browser smoke tests.

Real-run is intentionally not treated as production-ready until the Bilibili selector calibration and evidence gates pass in controlled small-scale validation.

## Version Labels

| Label | Meaning |
| --- | --- |
| Product Version | User-facing release line for repository and deployment status. |
| Architecture Stage | Internal design milestone, allowed to advance faster than product version. |
| Runtime Stage | What is safe to run today. |
| Real-run Status | Whether side-effecting platform actions are allowed. |
| Production Readiness | Whether this can be operated unattended at scale. |

## Browser Lifecycle Environment Variables

```text
WORKER_PERSISTENT_CONTEXT_TTL_SECONDS=21600
WORKER_PERSISTENT_CONTEXT_IDLE_SECONDS=1800
WORKER_MAX_PERSISTENT_CONTEXTS=20
WORKER_CONTEXT_REAPER_INTERVAL_SECONDS=60
```

## Preflight Command

```bash
python scripts/runtime_preflight.py
```

## Next Key Node

```text
Product Version: 0.3.10
Target: Container Runtime Smoke Node
Scope: docker compose config validation, service healthcheck readiness, worker startup/shutdown smoke, and browser lifecycle smoke in controlled environment
```
