
import json, sys, asyncio, warnings

from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))



from fastapi import FastAPI, HTTPException, Request

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from contextlib import asynccontextmanager

from app.db import database, redis

from app.services.recovery_daemon import start_recovery_daemon
from app.services.notification_dispatcher import start_notification_dispatcher

from app.services.scheduler import scheduler_loop

from app.api import accounts, lotteries, update, notify, metrics, proxies, events, knowledge, experiments, risk_intel, learning, governance, transitions

from app.config import settings
from app.governance.policy import DEFAULT_REAL_RUN_POLICY

from app.utils.log import structured_log
from app.security import authenticate_request



@asynccontextmanager

async def lifespan(app: FastAPI):

    await database.connect()
    await redis.initialize()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r"Table '.*' already exists")
        await ensure_runtime_schema()

    # 启动时健康检查

    try:

        await database.fetch_one("SELECT 1")

        structured_log("info", "db_connected")

    except Exception as e:

        structured_log("error", "db_connection_failed", exception=e)

        raise



    try:

        await redis.ping()

        structured_log("info", "redis_connected")

    except Exception as e:

        structured_log("error", "redis_connection_failed", exception=e)

        raise

    asyncio.create_task(start_recovery_daemon())

    asyncio.create_task(start_notification_dispatcher())

    asyncio.create_task(scheduler_loop())

    yield

    await database.disconnect()

    await redis.close()


async def ensure_runtime_schema():
    for statement in [
        "ALTER TABLE tracked_sources MODIFY source_type VARCHAR(32) NOT NULL",
        "ALTER TABLE lotteries MODIFY source_type VARCHAR(32) NOT NULL",
    ]:
        try:
            await database.execute(statement)
        except Exception as exc:
            structured_log("warning", "runtime_schema_alter_skipped", statement=statement, error=str(exc))
    await ensure_column("lotteries", "title", "VARCHAR(256) NULL")
    await ensure_column("lotteries", "rule_text", "TEXT NULL")
    await ensure_column("lotteries", "action_plan", "JSON NULL")
    await ensure_column("lotteries", "published_at", "TIMESTAMP NULL")
    await database.execute(
        """CREATE TABLE IF NOT EXISTS operators (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          username VARCHAR(128) NOT NULL,
          role ENUM('owner','admin','operator','viewer') NOT NULL DEFAULT 'viewer',
          active TINYINT DEFAULT 1,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_operator_username (username)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS audit_logs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          actor_id VARCHAR(128) NOT NULL,
          actor_role VARCHAR(32) NOT NULL,
          action VARCHAR(128) NOT NULL,
          resource_type VARCHAR(64) NOT NULL,
          resource_id VARCHAR(128) NULL,
          result VARCHAR(32) NOT NULL,
          risk_level VARCHAR(16) NOT NULL DEFAULT 'low',
          detail JSON NULL,
          ip_address VARCHAR(64) NULL,
          user_agent VARCHAR(255) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_audit_action_created (action, created_at),
          INDEX idx_audit_resource (resource_type, resource_id)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS events (
          id CHAR(36) PRIMARY KEY,
          aggregate VARCHAR(64) NOT NULL,
          aggregate_id VARCHAR(128) NOT NULL,
          event_type VARCHAR(128) NOT NULL,
          payload JSON NULL,
          correlation_id VARCHAR(128) NULL,
          causation_id VARCHAR(128) NULL,
          actor_type VARCHAR(32) NOT NULL DEFAULT 'system',
          actor_id VARCHAR(128) NULL,
          source_service VARCHAR(64) NOT NULL DEFAULT 'core-api',
          occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_events_aggregate (aggregate, aggregate_id, occurred_at),
          INDEX idx_events_type_created (event_type, occurred_at),
          INDEX idx_events_correlation (correlation_id, occurred_at)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS runtime_settings (
          setting_key VARCHAR(128) PRIMARY KEY,
          setting_value VARCHAR(1024) NOT NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """INSERT INTO runtime_settings (setting_key, setting_value)
           VALUES ('real_run_enabled', :real_run_enabled)
           ON DUPLICATE KEY UPDATE setting_value = setting_value""",
        {"real_run_enabled": "true" if settings.real_run_enabled else "false"},
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS circuit_breakers (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          scope VARCHAR(128) NOT NULL,
          status ENUM('closed','open','half_open') NOT NULL DEFAULT 'closed',
          reason VARCHAR(255) NULL,
          opened_at TIMESTAMP NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_circuit_scope (scope),
          INDEX idx_circuit_status (status)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """INSERT INTO circuit_breakers (scope, status)
           VALUES ('global', 'closed')
           ON DUPLICATE KEY UPDATE scope = scope"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS worker_heartbeats (
          worker_id VARCHAR(128) PRIMARY KEY,
          service_name VARCHAR(64) NOT NULL DEFAULT 'worker',
          status VARCHAR(32) NOT NULL DEFAULT 'ok',
          pid INT NULL,
          detail JSON NULL,
          last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          INDEX idx_worker_last_seen (last_seen_at)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS evidence_files (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          evidence_type VARCHAR(64) NOT NULL,
          task_id VARCHAR(64) NULL,
          account_id BIGINT NULL,
          lottery_id BIGINT NULL,
          file_path VARCHAR(512) NOT NULL,
          sha256 VARCHAR(64) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_evidence_task (task_id),
          INDEX idx_evidence_account (account_id)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS proxies (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          proxy_url TEXT NOT NULL,
          proxy_type VARCHAR(32) DEFAULT 'socks5',
          provider VARCHAR(64) NULL,
          country VARCHAR(32) NULL,
          health_score FLOAT DEFAULT 100,
          cooldown_until TIMESTAMP NULL,
          status ENUM('active','degraded','dead') DEFAULT 'active',
          last_check_at TIMESTAMP NULL
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS notification_secrets (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          key_name VARCHAR(64) NOT NULL,
          encrypted_value VARBINARY(4096) NOT NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_notification_secret_key (key_name)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS adapter_selector_configs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          platform VARCHAR(32) NOT NULL,
          config_json JSON NOT NULL,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_adapter_selector_platform (platform)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS login_sessions (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          session_id CHAR(36) NOT NULL,
          platform VARCHAR(32) NOT NULL,
          status ENUM('queued','opening','waiting_scan','confirmed','expired','failed') DEFAULT 'queued',
          login_url VARCHAR(512) NOT NULL,
          provider_key VARCHAR(128) NULL,
          qr_image_path VARCHAR(512) NULL,
          account_id BIGINT NULL,
          error_message TEXT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          expires_at TIMESTAMP NULL,
          completed_at TIMESTAMP NULL,
          UNIQUE KEY uk_login_session (session_id),
          INDEX idx_login_status (status, created_at)
        ) ENGINE=InnoDB"""
    )
    await ensure_column("login_sessions", "provider_key", "VARCHAR(128) NULL", after="login_url")
    try:
        await database.execute(
            """ALTER TABLE login_sessions
               MODIFY status ENUM('queued','opening','waiting_scan','scanned','confirmed','expired','failed')
               DEFAULT 'queued'"""
        )
    except Exception as exc:
        structured_log("warning", "runtime_schema_alter_skipped", statement="ALTER TABLE login_sessions MODIFY status", error=str(exc))
    await ensure_column("task_runs", "screenshot_path", "VARCHAR(512) NULL")
    await ensure_column("task_runs", "task_mode", "VARCHAR(32) NOT NULL DEFAULT 'dry_run'", after="dry_run")
    try:
        await database.execute("UPDATE task_runs SET task_mode = IF(dry_run = 1, 'dry_run', 'real_run') WHERE task_mode IS NULL OR task_mode = ''")
    except Exception as exc:
        structured_log("warning", "runtime_schema_backfill_skipped", statement="UPDATE task_runs task_mode", error=str(exc))
    await database.execute(
        """CREATE TABLE IF NOT EXISTS adapter_calibrations (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          probe_id CHAR(36) NOT NULL,
          platform VARCHAR(32) NOT NULL,
          account_id BIGINT NOT NULL,
          lottery_id BIGINT NULL,
          target_url VARCHAR(1024) NOT NULL,
          status ENUM('queued','running','succeeded','failed') DEFAULT 'queued',
          result JSON NULL,
          screenshot_path VARCHAR(512) NULL,
          error_message TEXT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          started_at TIMESTAMP NULL,
          finished_at TIMESTAMP NULL,
          UNIQUE KEY uk_probe_id (probe_id),
          INDEX idx_adapter_probe_status (status, created_at),
          INDEX idx_adapter_probe_platform (platform, created_at)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS account_calibrations (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          calibration_id CHAR(36) NOT NULL,
          platform VARCHAR(32) NOT NULL,
          account_id BIGINT NOT NULL,
          status ENUM('queued','running','succeeded','failed') DEFAULT 'queued',
          check_url VARCHAR(1024) NOT NULL,
          result JSON NULL,
          screenshot_path VARCHAR(512) NULL,
          error_message TEXT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          started_at TIMESTAMP NULL,
          finished_at TIMESTAMP NULL,
          UNIQUE KEY uk_account_calibration_id (calibration_id),
          INDEX idx_account_calibration_account (account_id, created_at),
          INDEX idx_account_calibration_status (status, created_at)
        ) ENGINE=InnoDB"""
    )
    await ensure_experiment_schema()
    await ensure_governance_schema()


async def ensure_experiment_schema():
    # Experiment Runtime (V5.5 / S3): dry/shadow A/B comparison tables.
    await database.execute(
        """CREATE TABLE IF NOT EXISTS experiments (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          experiment_id CHAR(36) NOT NULL,
          name VARCHAR(256) NOT NULL,
          platform VARCHAR(32) NOT NULL,
          mode VARCHAR(32) NOT NULL DEFAULT 'shadow_run',
          status ENUM('draft','running','stopped','completed') NOT NULL DEFAULT 'running',
          hypothesis TEXT NULL,
          allow_real_run TINYINT NOT NULL DEFAULT 0,
          stopped_reason VARCHAR(255) NULL,
          created_by VARCHAR(128) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          UNIQUE KEY uk_experiment_id (experiment_id),
          INDEX idx_experiment_status (status, created_at),
          INDEX idx_experiment_platform (platform, created_at)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS experiment_branches (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          experiment_id CHAR(36) NOT NULL,
          branch_key VARCHAR(64) NOT NULL,
          label VARCHAR(128) NULL,
          weight FLOAT NOT NULL DEFAULT 1,
          config_json JSON NULL,
          status ENUM('active','stopped') NOT NULL DEFAULT 'active',
          stopped_reason VARCHAR(255) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uk_experiment_branch (experiment_id, branch_key),
          INDEX idx_experiment_branch_eid (experiment_id)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS experiment_assignments (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          experiment_id CHAR(36) NOT NULL,
          branch_key VARCHAR(64) NOT NULL,
          unit_key VARCHAR(128) NOT NULL,
          lottery_id BIGINT NULL,
          task_id VARCHAR(64) NULL,
          account_id BIGINT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uk_experiment_unit (experiment_id, unit_key),
          INDEX idx_experiment_assignment_branch (experiment_id, branch_key),
          INDEX idx_experiment_assignment_task (task_id)
        ) ENGINE=InnoDB"""
    )


async def ensure_governance_schema():
    # Governance Runtime (V7 / S6): versioned policy objects + decision log.
    await database.execute(
        """CREATE TABLE IF NOT EXISTS policy_versions (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          policy_key VARCHAR(64) NOT NULL,
          version INT NOT NULL,
          definition JSON NOT NULL,
          note VARCHAR(255) NULL,
          created_by VARCHAR(128) NULL,
          active TINYINT NOT NULL DEFAULT 1,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uk_policy_version (policy_key, version),
          INDEX idx_policy_active (policy_key, active)
        ) ENGINE=InnoDB"""
    )
    await database.execute(
        """CREATE TABLE IF NOT EXISTS policy_decisions (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          decision_id CHAR(36) NOT NULL,
          policy_key VARCHAR(64) NOT NULL,
          policy_version INT NOT NULL,
          subject_type VARCHAR(64) NOT NULL,
          subject_id VARCHAR(128) NULL,
          inputs JSON NULL,
          outcome VARCHAR(32) NOT NULL,
          reasons JSON NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uk_policy_decision (decision_id),
          INDEX idx_policy_decision_subject (subject_type, subject_id, created_at),
          INDEX idx_policy_decision_policy (policy_key, policy_version, created_at)
        ) ENGINE=InnoDB"""
    )
    # Seed policy_versions with v1 = the built-in default so the first ever
    # transition has a real `from_version` to diff against, instead of a
    # special-cased "no history" branch.
    await database.execute(
        """INSERT INTO policy_versions (policy_key, version, definition, note, active, created_by)
           VALUES (:policy_key, :version, :definition, 'seed: built-in default', 1, 'system')
           ON DUPLICATE KEY UPDATE policy_key = policy_key""",
        {
            "policy_key": DEFAULT_REAL_RUN_POLICY["policy_key"],
            "version": DEFAULT_REAL_RUN_POLICY["version"],
            "definition": json.dumps(DEFAULT_REAL_RUN_POLICY, ensure_ascii=False),
        },
    )
    # Transition Runtime (V8 / S7): append-only audit trail of how policy
    # versions evolved (reason, classification, rollback condition).
    await database.execute(
        """CREATE TABLE IF NOT EXISTS policy_transitions (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          policy_key VARCHAR(64) NOT NULL,
          from_version INT NOT NULL,
          to_version INT NOT NULL,
          reason_code VARCHAR(32) NOT NULL,
          classification VARCHAR(16) NOT NULL,
          trigger_event VARCHAR(128) NULL,
          experiment_id CHAR(36) NULL,
          diff JSON NOT NULL,
          impact_scope JSON NULL,
          rollback_condition TEXT NOT NULL,
          loosening_justification TEXT NULL,
          requires_extra_review TINYINT NOT NULL DEFAULT 0,
          activated_at TIMESTAMP NULL,
          created_by VARCHAR(128) NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_policy_transition_key (policy_key, to_version)
        ) ENGINE=InnoDB"""
    )


async def ensure_column(table: str, column: str, definition: str, after: str | None = None) -> None:
    existing = await database.fetch_one(
        """SELECT 1
           FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE()
             AND TABLE_NAME = :table
             AND COLUMN_NAME = :column
           LIMIT 1""",
        {"table": table, "column": column},
    )
    if existing:
        return
    after_clause = f" AFTER `{after}`" if after else ""
    await database.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}{after_clause}")



app = FastAPI(title="Lottery System v3.0.2", version="0.3.2", lifespan=lifespan)


@app.middleware("http")

async def require_admin_token(request: Request, call_next):

    request.state.actor = None

    is_write = request.method not in {"GET", "HEAD", "OPTIONS"}

    if request.url.path.startswith("/api/") and is_write:

        try:
            await authenticate_request(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)



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

    structured_log("error", "unhandled_exception", exception=str(exc), path=str(request.url))

    return JSONResponse(

        status_code=500,

        content={"detail": "Internal server error"}

    )



app.include_router(accounts.router, prefix="/api/accounts", tags=["accounts"])

app.include_router(lotteries.router, prefix="/api/lotteries", tags=["lotteries"])

app.include_router(update.router, prefix="/api/update", tags=["update"])

app.include_router(notify.router, prefix="/api/notify", tags=["notify"])

app.include_router(proxies.router, prefix="/api/proxies", tags=["proxies"])

app.include_router(metrics.router, prefix="/api/metrics", tags=["metrics"])

app.include_router(events.router, prefix="/api/events", tags=["events"])

app.include_router(knowledge.router, prefix="/api/knowledge", tags=["knowledge"])

app.include_router(experiments.router, prefix="/api/experiments", tags=["experiments"])

app.include_router(risk_intel.router, prefix="/api/risk", tags=["risk-intel"])

app.include_router(learning.router, prefix="/api/learning", tags=["learning"])

app.include_router(governance.router, prefix="/api/governance", tags=["governance"])

app.include_router(transitions.router, prefix="/api/transitions", tags=["transitions"])


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

    return {

        "status": "ok" if (db_ok and redis_ok) else "degraded",

        "version": "0.3.2",

        "db": db_ok,

        "redis": redis_ok

    }
