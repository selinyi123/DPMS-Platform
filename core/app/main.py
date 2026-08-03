
import sys, asyncio

from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))



from fastapi import FastAPI, HTTPException, Request

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from contextlib import asynccontextmanager

from app.db import database, redis

from app.services.recovery_daemon import start_recovery_daemon
from app.services.notification_dispatcher import start_notification_dispatcher
from app.services.outbox import (
    reconcile_owned_stream_epochs,
    start_outbox_dispatcher,
)

from app.services.scheduler import scheduler_loop

from app.config import settings
from app.runtime_scope import (
    CoreRuntimePlan,
    build_core_runtime_plan,
    validate_core_deployment_plan,
)
from app.routers import include_api_routers
from app.security_posture import format_posture_problems, secret_posture
from app.migrations_runner import verify_migrations_current
from app.runtime_schema import (
    column_exists,
    ensure_column,
    ensure_consistency_schema,
    ensure_experiment_schema,
    ensure_governance_schema,
    ensure_orchestration_schema,
    ensure_runtime_schema,
    index_exists,
)
from app.version import API_TITLE, PRODUCT_VERSION

from app.utils.log import structured_log
from app.security import authenticate_request
from shared.redis_acl import verify_redis_acl


CORE_BACKGROUND_TASKS = (
    ("recovery", start_recovery_daemon),
    ("notification-dispatcher", start_notification_dispatcher),
    ("outbox-dispatcher", start_outbox_dispatcher),
    ("scheduler", scheduler_loop),
)


async def prepare_runtime_schema(deployment_mode: str) -> list[str]:
    """Verify only; every environment uses the explicit migration command.

    Runtime Core always connects with the least-privilege role. Letting a
    development startup perform DDL would either fail under that role or tempt
    operators to give it migration privileges, so there is no mode exception.
    """

    del deployment_mode
    await verify_migrations_current()
    return []


def core_background_tasks_for_plan(
    runtime_plan: CoreRuntimePlan,
):
    """Return mutually-exclusive shared/platform loop ownership."""

    if runtime_plan.owns_platform_lanes:
        return CORE_BACKGROUND_TASKS
    return (
        (
            "recovery-shared",
            lambda: start_recovery_daemon(
                platforms=(),
                include_shared=True,
            ),
        ),
        ("notification-dispatcher", start_notification_dispatcher),
        (
            "outbox-shared",
            lambda: start_outbox_dispatcher(
                platforms=(),
                include_shared=True,
            ),
        ),
        (
            "scheduler-shared",
            lambda: scheduler_loop(
                platforms=(),
                include_global=True,
            ),
        ),
    )


def _record_core_background_task_exit(app: FastAPI, task: asyncio.Task) -> None:
    """Expose every unexpected critical-loop exit through Core health."""

    if getattr(app.state, "background_shutdown_started", False):
        return

    task_name = task.get_name()
    exception = None
    if task.cancelled():
        outcome = "cancelled"
    else:
        exception = task.exception()
        outcome = "exception" if exception is not None else "unexpected_return"

    failures = dict(getattr(app.state, "background_task_failures", {}))
    failures[task_name] = {
        "outcome": outcome,
        "exception_type": (
            type(exception).__name__ if exception is not None else None
        ),
    }
    app.state.background_task_failures = failures
    structured_log(
        "critical",
        "core_background_task_exited",
        task=task_name,
        outcome=outcome,
        exception=exception,
    )


def _start_core_background_tasks(
    app: FastAPI,
    task_specs=None,
) -> tuple[asyncio.Task, ...]:
    app.state.background_shutdown_started = False
    app.state.background_task_failures = {}
    task_specs = CORE_BACKGROUND_TASKS if task_specs is None else task_specs
    tasks = tuple(
        asyncio.create_task(factory(), name=f"core:{name}")
        for name, factory in task_specs
    )
    app.state.background_tasks = tasks
    for task in tasks:
        task.add_done_callback(
            lambda completed, current_app=app: (
                _record_core_background_task_exit(current_app, completed)
            )
        )
    return tasks


async def _stop_core_background_tasks(
    app: FastAPI,
    tasks: tuple[asyncio.Task, ...],
) -> None:
    app.state.background_shutdown_started = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    app.state.background_tasks = ()



@asynccontextmanager

async def lifespan(app: FastAPI):

    deployment_mode = str(
        settings.deployment_mode or ""
    ).strip().casefold()
    runtime_plan = validate_core_deployment_plan(
        build_core_runtime_plan(),
        deployment_mode=deployment_mode,
    )
    app.state.runtime_role = runtime_plan.role
    app.state.platform_scope = runtime_plan.platforms

    # Production secret-posture guard (Phase 4): never run a real deployment on
    # shipped default secret or database credential. Run it before opening any
    # network connection so a rejected configuration cannot authenticate first.
    posture_problems = secret_posture(
        admin_token=settings.admin_token,
        update_secret=settings.update_secret,
        encryption_key=settings.encryption_key,
        database_url=settings.database_url,
        database_runtime_user=settings.mysql_runtime_user,
    )
    if posture_problems:
        summary = format_posture_problems(posture_problems)
        if deployment_mode == "production":
            structured_log("error", "insecure_secret_posture", mode="production", problems=summary)
            raise RuntimeError(f"Refusing to start in production with insecure secrets: {summary}")
        structured_log("warning", "insecure_secret_posture", mode=settings.deployment_mode, problems=summary)

    background_tasks: tuple[asyncio.Task, ...] = ()
    try:
        await database.connect()
        await redis.initialize(
            platforms=runtime_plan.platforms,
            include_shared=True,
        )
        # DDL is an explicit release/setup step in every mode. Runtime Core
        # only verifies the complete migration history and live schema.
        await prepare_runtime_schema(deployment_mode)

        # 启动时健康检查
        await database.fetch_one("SELECT 1")
        structured_log("info", "db_connected")
        await redis.ping()
        structured_log("info", "redis_connected")

        if (
            settings.redis_acl_preflight_required
            or deployment_mode == "production"
        ):
            await verify_redis_acl(
                redis,
                redis_url=settings.redis_url,
                expected_username="core",
                role="core",
                configured_username=settings.redis_username,
                configured_password=settings.redis_password,
                reject_development_passwords=(
                    deployment_mode == "production"
                ),
                platforms=runtime_plan.platforms,
                include_shared=True,
            )
            structured_log(
                "info",
                "redis_acl_preflight_succeeded",
                role="core",
            )

        # Task-stream delivery depends on Redis INFO server/run_id.  Refuse to
        # accept dispatch requests when the configured ACL cannot provide the
        # epoch needed to detect acknowledged writes lost across a restart.
        await reconcile_owned_stream_epochs(
            platforms=runtime_plan.platforms,
            include_shared=True,
            require_all_owned_lanes=(
                not runtime_plan.owns_platform_lanes
            ),
        )

        background_tasks = _start_core_background_tasks(
            app,
            core_background_tasks_for_plan(runtime_plan),
        )
        yield
    finally:
        if background_tasks:
            # These loops own DB/Redis work. Await their cancellation before
            # closing either client so shutdown cannot race a relay.
            await _stop_core_background_tasks(app, background_tasks)
        await database.disconnect()
        await redis.close()


app = FastAPI(title=API_TITLE, version=PRODUCT_VERSION, lifespan=lifespan)


# Paths under /api that are intentionally unauthenticated. Keep this minimal:
# only the container health probe (docker healthcheck curls /api/health).
PUBLIC_API_PATHS = {"/api/health"}


@app.middleware("http")

async def require_admin_token(request: Request, call_next):

    request.state.actor = None

    path = request.url.path

    if (
        request.method != "OPTIONS"
        and path.startswith("/api/")
        and path not in PUBLIC_API_PATHS
        and getattr(app.state, "background_task_failures", {})
    ):
        # Docker exposes the failed liveness state through /api/health, but a
        # restart is orchestrator-dependent. Fail closed here as well so a
        # Core that lost recovery/outbox/scheduling cannot continue accepting
        # apparently healthy reads or writes while waiting to be replaced.
        return JSONResponse(
            status_code=503,
            content={"detail": "Core background service unavailable"},
        )

    # Default-closed (P0-1): every /api request is authenticated, not just
    # writes — read endpoints expose account/proxy/schedule/governance state
    # and must not be public. CORS preflight (OPTIONS) and the health probe
    # are the only exemptions.
    if (
        request.method != "OPTIONS"
        and path.startswith("/api/")
        and path not in PUBLIC_API_PATHS
    ):

        try:
            await authenticate_request(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)


# Security response headers (Phase 4). Registered after the auth middleware so it
# is the outermost layer and applies to every response, including 401s. API
# responses are JSON, never embedded HTML, so the CSP can be maximally strict and
# caches must never store the (often sensitive) bodies.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cache-Control": "no-store",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response



cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

# CORS 配置（生产环境应限制 origins）

app.add_middleware(

    CORSMiddleware,

    allow_origins=cors_origins,

    allow_methods=["*"],

    allow_headers=["*"],

)



# 全局异常处理器

@app.exception_handler(Exception)

async def global_exception_handler(request, exc):

    structured_log(
        "error",
        "unhandled_exception",
        exception=str(exc),
        path=request.url.path,
    )

    return JSONResponse(

        status_code=500,

        content={"detail": "Internal server error"}

    )



include_api_routers(app)


@app.get("/api/auth/me")

async def auth_me(request: Request):

    actor = await authenticate_request(request)

    return {
        "actor_id": actor["actor_id"],
        "role": actor["role"],
        "auth_type": actor["auth_type"],
    }



@app.get("/api/health")

async def health():

    # 健康检查包含 DB 和 Redis 状态

    db_ok = False

    redis_ok = False

    try:

        await database.fetch_one("SELECT 1")

        db_ok = True

    except Exception:

        pass

    try:

        await redis.ping()

        redis_ok = True

    except Exception:

        pass

    background_failures = dict(
        getattr(app.state, "background_task_failures", {})
    )
    payload = {

        "status": (
            "ok"
            if (db_ok and redis_ok and not background_failures)
            else "degraded"
        ),

        "version": PRODUCT_VERSION,

        "db": db_ok,

        "redis": redis_ok,

        "background_tasks": not background_failures,

    }
    if background_failures:
        # This endpoint is public. Exception detail stays in the structured
        # log; health exposes only stable task names and outcomes.
        payload["failed_background_tasks"] = {
            name: failure["outcome"]
            for name, failure in sorted(background_failures.items())
        }
    if background_failures or not db_ok or not redis_ok:
        return JSONResponse(status_code=503, content=payload)
    return payload
