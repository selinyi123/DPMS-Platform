# Xiaohongshu read-only target pursuit daemon

`core-xiaohongshu-pursuit` is an independent long-running HTTP client for the
Xiaohongshu target-review module. It lists active sources through
`GET /api/xiaohongshu-targets/sources?active=true` and requests due scans through
`POST /api/xiaohongshu-targets/scan`. It does not import Core database code.

The daemon is intentionally limited to discovery. It cannot call candidate
decision endpoints, accept or skip a candidate, create a participation task, or
perform follow, like, favorite, or comment actions. Keyword and author-profile
sources are browser-scannable. `offline_search_result` remains visible in the
same source model but is only populated through the bounded offline ingest API,
so the daemon skips it rather than turning it into a failing browser request.

## Default-closed startup

The service is excluded from normal Compose startup by the `autopilot` profile.
Even inside that profile it makes no API requests until both gates are explicit:

```dotenv
DPMS_XIAOHONGSHU_TARGET_PURSUIT_ENABLED=true
DPMS_XIAOHONGSHU_TARGET_PURSUIT_PLATFORMS=xiaohongshu
```

Start the read-only pursuit lane with the existing Core and Xiaohongshu worker:

```powershell
docker compose --profile autopilot up -d `
  core-api core-xiaohongshu-runner worker-xiaohongshu `
  core-xiaohongshu-pursuit
```

This service is separate from `core-autopilot`. Enabling target pursuit does not
enable strategy dispatch or real-run.

## Cadence and bounds

- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_CADENCE_SECONDS` is compared with Core's
  durable `last_scan_at`; a recent source is not requested again.
- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_SOURCE_LIMIT` bounds the active source page
  read from Core (maximum 200).
- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_SCAN_LIMIT` bounds scans in one round.
- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_MAX_CANDIDATES` bounds one scan result.
- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_FAILURE_LIMIT` stops retrying the same source
  after consecutive failures in the daemon process. A successful scan resets
  that counter; Core's durable `last_scan_at`, status, and error code remain the
  cross-process audit record.
- `DPMS_XIAOHONGSHU_TARGET_PURSUIT_POLL_SECONDS` controls the outer loop.

The container health check watches
`/tmp/xiaohongshu-target-pursuit-health`. Deliberately disabled operation keeps
the liveness marker fresh without calling Core. An enabled round refreshes it
only after the active-source list was read successfully.
