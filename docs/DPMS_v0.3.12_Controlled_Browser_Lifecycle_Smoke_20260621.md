# DPMS v0.3.12 Controlled Browser Lifecycle Smoke

Date: 2026-06-21

## Goal

Move from controlled worker lifecycle smoke to controlled browser lifecycle smoke inside the worker container.

This iteration does not repeat recovery, outbox, schema boundary, browser context lifecycle, runtime preflight, Compose smoke, or worker lifecycle smoke. It adds an explicit browser launch/context/close harness that validates browser process cleanup without touching platform accounts or pages.

## External research summary

Playwright documents that explicitly created browser contexts should be closed before closing the browser, and that browser close disposes the browser and its pages. Docker exec and inspect allow a harness to execute a bounded smoke command inside the already-started worker container and verify health without changing application task state.

## Implemented design

### Controlled browser lifecycle smoke harness

File: `scripts/controlled_browser_lifecycle_smoke.py`

Default dry-run:

```bash
python scripts/controlled_browser_lifecycle_smoke.py
```

Explicit execution:

```bash
python scripts/controlled_browser_lifecycle_smoke.py --execute --project-name dpms-browser-smoke
```

The harness executes this sequence only when `--execute` is passed:

1. run `scripts/runtime_preflight.py`;
2. run `scripts/container_runtime_smoke.py`;
3. run `scripts/controlled_worker_lifecycle_smoke.py` in dry-run mode;
4. run `docker compose config --quiet`;
5. start mysql, redis, core-api, worker;
6. wait for those services to become healthy;
7. execute a Python Playwright smoke inside worker:
   - capture Chromium process count before;
   - `chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])`;
   - `browser.new_context()`;
   - `context.close()`;
   - `browser.close()`;
   - assert Chromium process count does not increase;
8. run `docker compose down -v --remove-orphans`.

## Safety scope

The harness does not:

- create accounts;
- login accounts;
- enqueue lottery tasks;
- open platform pages;
- perform platform actions.

The browser smoke does not navigate to any external URL.

## Fix included

`runtime_preflight.py` now includes a smoke script contract check. It verifies that the controlled browser lifecycle smoke harness exists and includes the required dry-run, explicit execution, Playwright close, and safety-boundary markers.

## Current limitation

This harness is opt-in and was not executed in this PR. It validates raw Playwright browser lifecycle inside the worker container, not account persistent-context behavior with real profiles.

## Next node

v0.3.13 should add Controlled Shadow-Run Readiness:

- verify queue idle state;
- verify DB readiness without seeding accounts;
- verify selector calibration prerequisites;
- do not login accounts;
- do not perform platform actions.