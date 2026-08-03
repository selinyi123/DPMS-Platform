# Douyin Android device-agent foundation

`device_agent` is a standalone, standard-library-only Android/ADB execution
service. DPMS `worker-douyin` reaches it through an authenticated local HTTP
bridge; the Windows process itself has no Core, Redis, scheduler, or Docker
dependency.

Safety properties:

- An absolute ADB executable path and a device serial are mandatory.
- The foreground package must be `com.ss.android.ugc.aweme` before every read
  or mutation.
- Controls are uniquely and exactly matched from `uiautomator dump` XML by the
  calibrated `text`, `resource-id`, and/or `content-desc`. There is no fixed
  coordinate or fuzzy selector fallback.
- Follow, like, comment, and favorite read their done-state before acting and
  confirm it again after acting. Missing post-state is an `unknown` outcome and
  halts the resident loop.
- Comment text is read back exactly before submit. If the device IME cannot
  enter the supplied Unicode text, the comment is not submitted.
- Configured CAPTCHA, face-verification, account-risk, or rate-limit text stops
  execution. This package contains no bypass behavior.
- Account-scoped file locking, persisted rate limits, and cooldowns prevent
  concurrent or rapid mutation.
- HTTP mutations additionally require a lowercase 64-hex `target_hash` whose
  reviewed `target_markers` all match exactly one UI node. Follow also binds an
  exact `follow_target_handle` and its separate author markers. A request can
  never submit selectors of its own.

The example manifest contains placeholders and cannot be treated as a live
calibration. Build a manifest from a reviewed read-only UI dump for the exact
Douyin app version and screen:

```text
device_agent/examples/calibration.example.json
```

## Offline fixture runner

A click action normally supplies `before.xml` and `after.xml`. A comment action
normally supplies four ordered frames: before, focused input, exact typed
read-back, and submitted/done.

```powershell
python -m device_agent fixture `
  --manifest D:\path\calibration.json `
  --account-id test-account `
  --state-dir D:\path\device-agent-state `
  --action like `
  --frame D:\path\before.xml `
  --frame D:\path\after.xml
```

The runner never launches ADB. Its JSON output includes only simulated
operations and the verified result.

## Read-only device snapshot

This command only reads the current real-device UI and never invokes an action
primitive:

```powershell
python -m device_agent snapshot `
  --manifest D:\path\calibration.json `
  --adb-path C:\Android\platform-tools\adb.exe `
  --serial DEVICE_SERIAL `
  --account-id local-account `
  --state-dir D:\path\device-agent-state
```

The authenticated service below is the reviewed DPMS integration point. Core
and Worker bind account, target, rule snapshot, action plan, calibration hashes,
Probe, and Shadow evidence before a real-run task can call it. Worker persists
an external-action intent before every call and quarantines an unknown result.
See `docs/DPMS_Douyin_Device_Agent.md` for the end-to-end startup sequence.

## Loopback HTTP service

The service is a standard-library-only Windows host process. It is hard-bound
to `127.0.0.1`; there is no `--host` option. Construction and startup do not
read the screen or run an action. Every endpoint requires the same Bearer token,
and the token is never included in health output or request logs. Prefer an
environment variable so it is not visible in the command line:

```powershell
$env:DPMS_DEVICE_AGENT_BEARER_TOKEN = '<at-least-32-random-characters>'
python -m device_agent serve `
  --manifest D:\path\calibration.json `
  --adb-path C:\Android\platform-tools\adb.exe `
  --serial DEVICE_SERIAL `
  --account-id local-account `
  --state-dir D:\path\device-agent-state `
  --port 8765 `
  --adb-timeout-seconds 15 `
  --operation-timeout-seconds 60 `
  --request-body-limit-bytes 16384
```

HTTP calibration requires a non-empty `target_markers` object. Each key is the
exact target identity hash produced by DPMS. Its `markers` identify the note,
while `author_handle` plus `follow_markers` bind a follow click to the reviewed
author. Every configured selector is equality-only and must match exactly one
node before a mutation. The example hash and selectors are placeholders and
will fail closed until replaced from a reviewed read-only UI dump.

Endpoints:

- `GET /health` returns `status: ok`, readiness, package, a stable hashed
  `agent_id`, and only SHA-256 values for the manifest, device serial, and
  account identifier, plus busy state and supported actions.
- `POST /v1/snapshot` accepts `target_hash` and optional exact `comment` /
  `follow_target_handle`. It is read-only and returns
  `target_identity_verified` and `follow_target_verified`.
- `POST /v1/execute` accepts one exact `request_id`, `target_hash`, `action`,
  and the action-specific exact `comment` or `follow_target_handle`. It returns
  top-level target/action binding plus `before_snapshot`, `result`, and
  `after_snapshot`. Each returned snapshot repeats `agent_id`,
  `manifest_sha256`, `device_serial_sha256`, `account_id_sha256`, and
  `target_hash`; each action state has an explicit `calibrated` flag.

The stable `agent_id` is `sha256("dpms-device-agent-v1:" + manifest_sha256 +
":" + device_serial_sha256)` so the Worker can bind evidence without learning
the raw serial.

Example authenticated snapshot request:

```powershell
$headers = @{
  Authorization = "Bearer $env:DPMS_DEVICE_AGENT_BEARER_TOKEN"
}
$body = @{
  target_hash = '<lowercase-64-hex-target-hash>'
  follow_target_handle = '@exact-reviewed-author'
} | ConvertTo-Json -Compress
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/v1/snapshot `
  -Headers $headers `
  -ContentType application/json `
  -Body $body
```

Only one snapshot or execution task runs at a time. Concurrent work receives
HTTP 409. Request bodies are bounded. If the configured total timeout is
reached during an execution, the response is HTTP 504 with an `unknown`,
halting result; the task lock remains held until the underlying worker actually
stops. CAPTCHA, face verification, risk text, target drift, author drift, and
missing post-action confirmation remain blocked/unknown outcomes. The service
does not attempt to solve or bypass any verification challenge.
