# DPMS Autopilot

`core-autopilot` is an independent long-running client of the authenticated
Core API. It reads the strategy queue and advances each target through the
recommended validation ladder without importing Core database internals.

## Default-closed startup

The service is excluded from normal Compose startup by the `autopilot` profile.
It also dispatches nothing unless both `DPMS_AUTOPILOT_ENABLED=true` and a
non-empty `DPMS_AUTOPILOT_PLATFORMS` allowlist are configured.

For Bilibili, keep the platform services running as well:

```powershell
docker compose --profile autopilot up -d `
  core-api core-bilibili-runner worker-bilibili core-autopilot
```

`worker-bilibili` owns Bilibili account calibration, read-only probes and task
execution. After official QR login, the account remains `warming` until that
worker consumes and successfully completes its calibration request.

## Platform progression

1. The strategy queue recommends `dry_run` until one succeeds.
2. It then recommends `shadow_run`; active task rows prevent concurrent repeat
   dispatch, and Autopilot never repeats a successful validation rung.
3. A successful shadow that still needs exact execution evidence is paired
   with an account-bound, read-only probe. Autopilot requests that probe once
   for the same platform and account, then waits while it is queued/running or
   already fresh. This covers both Bilibili and the XHS browser path.
4. Core/Worker materialize exact execution evidence from the matching shadow
   and probe. Only then may the strategy queue recommend `real_run`.
5. A successful real-run changes the lottery to `participated`, removing it
   from the pending/claimed strategy queue. Failed tasks stop being retried when
   `DPMS_AUTOPILOT_FAILURE_LIMIT` is reached.

Real-run remains disabled unless all of the following are true:

- deployment and runtime real-run switches are enabled;
- `DPMS_AUTOPILOT_ENABLED=true`;
- `DPMS_AUTOPILOT_REAL_RUN_ACK=I_ACKNOWLEDGE_DPMS_AUTOPILOT_REAL_RUN`;
- the existing account, action-plan, evidence, risk and circuit-breaker gates
  all pass.

The container health check watches `/tmp/autopilot-health`. A successful queue
round refreshes it; repeated Core API failures eventually make the container
unhealthy. A deliberately disabled Autopilot still refreshes the liveness file
but never calls a dispatch endpoint.

The independent read-only Xiaohongshu source scanner is documented in
`DPMS_Xiaohongshu_Target_Pursuit_Daemon.md`. It shares the Compose `autopilot`
profile for opt-in deployment, but it does not share strategy dispatch or
real-run authority with `core-autopilot`.
