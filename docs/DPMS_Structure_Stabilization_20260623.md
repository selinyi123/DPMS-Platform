# DPMS Structure Stabilization Record

Date: 2026-06-23
Product version baseline: 0.3.13

## Scope

This round is a structure-stabilization pass. It prioritizes deployability,
configuration ownership, version consistency, and low-risk module boundaries
before deeper feature expansion.

## Completed Changes

- Added a Docker empty-database bootstrap SQL file at `docker/mysql/001-bootstrap.sql`.
- Mounted the bootstrap file into MySQL through `docker-compose.yml`.
- Updated `deploy.sh` to use the new bootstrap SQL instead of legacy `init.sql`.
- Made account soft-delete migration `0006` idempotent for databases that already contain those columns.
- Added `0007_worker_lease_contract_repair.sql` to repair historical `0002` version drift and guarantee worker lease/dead-letter schema.
- Added `.dockerignore` because root build context is now used by `core-api` and `worker`.
- Switched core/worker Dockerfiles to root build context so both images can copy shared assets.
- Added `shared/platforms.json` and made core/worker platform modules load the same platform manifest.
- Added `core/app/version.py` and wired FastAPI title/health version to the product version source.
- Updated frontend package/version display to use Vite-injected `VITE_DPMS_VERSION`.
- Extracted backend router registration to `core/app/routers.py`.
- Extracted real-run readiness/gate helper logic to `core/app/services/real_run_readiness.py`.
- Made runtime schema compatibility checks quieter by checking existing task-run generated columns and indexes before ALTER.
- Added `authenticatedApiPath()` for browser-native GET resources such as SSE, QR images, and evidence screenshots.
- Restricted query-token authentication to GET/HEAD requests only.
- Extracted frontend template formatting to `frontend/src/i18n/format.js`.
- Extracted frontend translation dictionaries to `frontend/src/i18n/dictionaries.js`, leaving `uiContext.jsx` focused on context state.
- Split `frontend/src/index.css` into ordered style modules under `frontend/src/styles/`.
- Fixed worker test harness UTF-8 reading on Windows.
- Hardened worker task status reads with `row_get()` to prevent secondary crashes on partial row mappings.
- Suppressed expected worker event-table bootstrap warnings and added worker startup DB/Redis connection retry.

## Verification

Passed:

- `python scripts/migration_smoke.py`
- `python scripts/runtime_preflight.py`
- `python scripts/container_runtime_smoke.py`
- `python -m compileall core\app worker\app scripts\migration_smoke.py scripts\container_runtime_smoke.py scripts\runtime_preflight.py`
- `$env:PYTHONPATH='core'; .venv312\Scripts\python.exe -m unittest discover core\tests` (337 tests)
- `$env:PYTHONPATH='worker'; .venv312\Scripts\python.exe -m unittest discover worker\tests` (39 tests)
- `npm run build`
- `docker compose up -d --build`
- `docker compose ps` showed `core-api`, `worker`, `mysql`, `redis`, and `nginx` healthy.
- `GET http://localhost/api/health` returned `status=ok`, `version=0.3.13`, `db=True`, `redis=True`.
- `GET http://localhost/` returned HTTP 200 and the React root.
- Authenticated checks returned 4 readiness platforms, 4 adapter platforms, and 11 real-run evidence items.

Runtime observations:

- Docker Desktop was manually restarted once during verification; containers automatically recovered to healthy after the daemon returned.
- `core-api` startup logs are clean except for the expected development warning about the default database password.
- `worker` startup logs are clean after the retry/warning-filter update.

## Remaining Structural Risks

- `core/app/api/lotteries.py` is smaller but still the largest backend congestion point and needs additional staged extraction.
- `core/app/main.py` still owns several runtime schema compatibility functions; migrations are improved but not yet the only schema authority.
- Frontend styling now has module files, but feature page components such as `Deploy.jsx` and `Lotteries.jsx` still need component-level splits.
- README contains historical mojibake and should be rewritten or regenerated from clean documentation.
- The current local deployment is a dev posture: `DATABASE_URL` still uses the default database password and should be changed before production exposure.

## Recommended Next Refactor Loop

1. Split more lottery API concerns into `core/app/services/` and smaller API modules.
2. Move schema self-heal blocks from `core/app/main.py` into versioned migrations or a clearly named bootstrap compatibility module.
3. Split feature page components such as `Deploy.jsx` and `Lotteries.jsx` into panels/hooks.
4. Rewrite README from the clean docs source and remove mojibake.
5. Add a release verification script that runs the tested commands above and records the local deployment evidence.
