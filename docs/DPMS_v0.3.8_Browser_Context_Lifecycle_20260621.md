# DPMS v0.3.8 Browser Context Lifecycle

Date: 2026-06-21

## Goal

Improve long-running Worker stability by managing persistent Playwright contexts explicitly.

This iteration does not repeat the v0.3.5 recovery work, the v0.3.6 outbox work, or the v0.3.7 schema boundary work. It focuses on browser context lifetime, idle cleanup, capacity control, and memory attribution.

## External research summary

Playwright documents BrowserContext as the unit for isolated browser sessions. Contexts should be closed when no longer needed. Playwright also documents that `launch_persistent_context()` uses a user data directory and returns the only context for that browser instance; closing that context closes the browser.

## Implemented design

### Persistent context metadata

File: `worker/app/browser_pool.py`

Each persistent account context now has metadata:

- profile directory;
- browser process pid;
- created timestamp;
- last used timestamp;
- use count.

### TTL and idle eviction

Persistent contexts are closed when they are idle and exceed:

```text
WORKER_PERSISTENT_CONTEXT_TTL_SECONDS=21600
WORKER_PERSISTENT_CONTEXT_IDLE_SECONDS=1800
```

Contexts with open pages are not evicted by the reaper to avoid interrupting active work.

### Capacity control

The pool now supports:

```text
WORKER_MAX_PERSISTENT_CONTEXTS=20
```

If capacity is reached, the least recently used idle context is closed first.

### Background reaper

File: `worker/app/main.py`

Worker startup now creates `pool.context_reaper_loop(...)`. It logs:

- active persistent context count;
- total browser memory;
- per-account memory snapshot.

### Shutdown fix

`BrowserPool.close_browser()` now exists. This fixes the existing shutdown path in `worker/app/main.py`, which already called `pool.close_browser(bid)`.

## Current limitation

The reaper does not close contexts with open pages. This is intentional for safety. If a page leak leaves pages open indefinitely, a future smoke test should detect it and fail the Worker readiness check.

## Next node

v0.3.9 should add runtime preflight validation:

- migration smoke;
- browser lifecycle smoke;
- worker startup/shutdown smoke;
- controlled shadow-run readiness checks.
