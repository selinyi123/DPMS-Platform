import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

from app.adapters.registry import get_adapter
from app.browser_pool import BrowserPool
from app.db import database, redis
from app.event_store.service import record_event
from app.safety import detect_page_risk, ensure_account_can_run
from app.utils.log import structured_log
from app.utils.cookies import inject_account_cookies
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault


STREAM_KEY = "lottery_tasks"
GROUP_NAME = "workers"
WORKER_ID = os.getenv("HOSTNAME") or f"worker-{os.getpid()}"
CONSUMER_NAME = WORKER_ID
PHASE_ORDER = ["followed", "liked", "commented", "reposted"]
TERMINAL_TASK_STATUSES = {"succeeded", "failed"}
TASK_LEASE_SECONDS = 900
SCREENSHOT_DIR = Path("/profiles/task-failures")
SHADOW_SCREENSHOT_DIR = Path("/profiles/shadow-runs")


class TaskAlreadyTerminal(Exception):
    pass


class InvalidTaskMessage(Exception):
    pass


async def get_latest_phase(task_id: str) -> str:
    row = await database.fetch_one(
        "SELECT phase FROM task_phases WHERE task_id = :tid ORDER BY id DESC LIMIT 1",
        {"tid": task_id},
    )
    return row["phase"] if row else "init"


async def refresh_task_lease(task_id: str):
    await database.execute(
        """UPDATE task_runs
           SET lease_expires_at = DATE_ADD(NOW(), INTERVAL :seconds SECOND)
           WHERE task_id = :task_id AND status = 'running' AND worker_id = :worker_id""",
        {"task_id": task_id, "worker_id": WORKER_ID, "seconds": TASK_LEASE_SECONDS},
    )


async def save_phase(task_id: str, account_id: int, lottery_id: int, phase: str):
    await refresh_task_lease(task_id)
    await database.execute(
        """INSERT INTO task_phases (task_id, account_id, lottery_id, phase)
           VALUES (:tid, :aid, :lid, :phase)
           ON DUPLICATE KEY UPDATE phase = :phase, updated_at = NOW()""",
        {"tid": task_id, "aid": account_id, "lid": lottery_id, "phase": phase},
    )
    await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskPhaseCompleted" if phase != "completed" else "TaskPhasesCompleted",
        payload={"account_id": account_id, "lottery_id": lottery_id, "phase": phase},
        correlation_id=task_id,
    )
    structured_log("info", "phase_saved", task_id=task_id, phase=phase)


async def mark_task_started(task_id: str, account_id: int, lottery_id: int, task_mode: str, stream_message_id: str | None = None):
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT status FROM task_runs WHERE task_id = :task_id FOR UPDATE",
            {"task_id": task_id},
        )
        if not row:
            raise RuntimeError(f"Task row not found: {task_id}")
        status = str(row["status"] or "")
        if status in TERMINAL_TASK_STATUSES:
            raise TaskAlreadyTerminal(f"Task {task_id} is already terminal: {status}")
        await database.execute(
            """UPDATE task_runs
               SET status = 'running', task_mode = :task_mode,
                   started_at = COALESCE(started_at, NOW()),
                   worker_id = :worker_id,
                   stream_message_id = :stream_message_id,
                   lease_expires_at = DATE_ADD(NOW(), INTERVAL :lease_seconds SECOND)
               WHERE task_id = :task_id""",
            {
                "task_id": task_id,
                "task_mode": task_mode,
                "worker_id": WORKER_ID,
                "stream_message_id": stream_message_id,
                "lease_seconds": TASK_LEASE_SECONDS,
            },
        )
        await database.execute(
            "UPDATE lotteries SET status = 'running' WHERE id = :lottery_id AND execution_lock = :task_id",
            {"lottery_id": lottery_id, "task_id": task_id},
        )
        await database.execute(
            """UPDATE accounts
               SET status = 'executing', daily_task_count = daily_task_count + 1, last_active_at = NOW()
               WHERE id = :account_id AND status <> 'executing'""",
            {"account_id": account_id},
        )
    await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskStarted",
        payload={"account_id": account_id, "lottery_id": lottery_id, "mode": task_mode, "worker_id": WORKER_ID},
        correlation_id=task_id,
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountExecutionStarted",
        payload={"task_id": task_id, "lottery_id": lottery_id},
        correlation_id=task_id,
    )


async def mark_task_finished(
    task_id: str,
    account_id: int,
    lottery_id: int,
    task_mode: str,
    success: bool,
    error: str | None = None,
    screenshot_path: str | None = None,
):
    lottery_status = "participated" if success and task_mode == "real_run" else "pending"
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT status FROM task_runs WHERE task_id = :task_id FOR UPDATE",
            {"task_id": task_id},
        )
        if row and str(row["status"] or "") in TERMINAL_TASK_STATUSES:
            return
        await database.execute(
            """UPDATE task_runs
               SET status = :status, error_message = :error, screenshot_path = :screenshot_path,
                   finished_at = NOW(), worker_id = NULL, stream_message_id = NULL, lease_expires_at = NULL
               WHERE task_id = :task_id""",
            {
                "task_id": task_id,
                "status": "succeeded" if success else "failed",
                "error": error,
                "screenshot_path": screenshot_path,
            },
        )
        await database.execute(
            "UPDATE lotteries SET status = :status, execution_lock = NULL, locked_at = NULL WHERE id = :lottery_id AND execution_lock = :task_id",
            {"lottery_id": lottery_id, "status": lottery_status, "task_id": task_id},
        )
        await database.execute(
            "UPDATE accounts SET status = 'ready', updated_at = NOW() WHERE id = :account_id AND status = 'executing'",
            {"account_id": account_id},
        )
        await database.execute(
            """INSERT INTO notify_logs (channel, title, content, success)
               VALUES ('system', :title, :content, :success)""",
            {
                "title": f"Task {task_id[:8]} {task_mode} {'succeeded' if success else 'failed'}",
                "content": error or f"Lottery {lottery_id} handled by account {account_id} in {task_mode}",
                "success": 1 if success else 0,
            },
        )
    await record_event(
        aggregate="task",
        aggregate_id=task_id,
        event_type="TaskFinished" if success else "TaskFailed",
        payload={
            "account_id": account_id,
            "lottery_id": lottery_id,
            "success": success,
            "mode": task_mode,
            "error": error,
            "screenshot_path": screenshot_path,
            "worker_id": WORKER_ID,
        },
        correlation_id=task_id,
    )
    await record_event(
        aggregate="account",
        aggregate_id=account_id,
        event_type="AccountExecutionFinished",
        payload={"task_id": task_id, "lottery_id": lottery_id, "success": success, "mode": task_mode},
        correlation_id=task_id,
    )
    try:
        await redis.xadd(
            "notify_events",
            {
                "title": f"Task {task_id[:8]} {task_mode} {'succeeded' if success else 'failed'}",
                "content": error or f"Lottery {lottery_id} handled by account {account_id} in {task_mode}",
                "task_id": task_id,
                "account_id": str(account_id),
                "lottery_id": str(lottery_id),
                "status": "succeeded" if success else "failed",
                "mode": task_mode,
                "channels": "all",
            },
        )
    except Exception as exc:
        structured_log("warning", "notify_enqueue_failed", task_id=task_id, error=str(exc))


async def execute_dry_run(task_id: str, account_id: int, lottery_id: int, phases: list[str]):
    for phase_name in phases:
        await asyncio.sleep(0.2)
        await save_phase(task_id, account_id, lottery_id, phase_name)
    await save_phase(task_id, account_id, lottery_id, "completed")
    structured_log("info", "dry_run_task_completed", task_id=task_id, account_id=account_id, lottery_id=lottery_id)


async def execute_shadow_run(task: dict, adapter, pool):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    lottery_url = task.get("raw_url") or task.get("canonical_url")
    platform = task.get("platform", "bilibili")
    profile_dir = task.get("profile_dir", f"/profiles/{platform}/account_{account_id}")
    proxy = task.get("proxy")
    phases = requested_phases(task, require_plan=False)
    ctx = await pool.get_account_context(account_id, profile_dir, proxy)
    await prepare_account_login(ctx, account_id, platform)
    page = await ctx.new_page()
    try:
        await page.goto(lottery_url, wait_until="domcontentloaded", timeout=30000)
        await refresh_task_lease(task_id)
        await page.wait_for_timeout(2000)
        await detect_page_risk(page, account_id, platform)
        visible_phases = {}
        selector_probes = getattr(adapter, "SELECTOR_PROBES", {}) or {}
        for phase_name in phases:
            visible_phases[phase_name] = await first_visible_selector(page, selector_probes.get(phase_name, []))
            await refresh_task_lease(task_id)
        screenshot_path = await capture_shadow_screenshot(page, task_id, account_id, lottery_id, visible_phases)
        await save_phase(task_id, account_id, lottery_id, "completed")
        await record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="TaskShadowRunObserved",
            payload={
                "account_id": account_id,
                "lottery_id": lottery_id,
                "platform": platform,
                "visible_phases": visible_phases,
                "screenshot_path": screenshot_path,
                "side_effects": False,
            },
            correlation_id=task_id,
        )
        structured_log(
            "info",
            "shadow_run_task_completed",
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            visible_phase_count=sum(1 for value in visible_phases.values() if value),
        )
    except Exception:
        await capture_failure_screenshot(page, task_id)
        raise
    finally:
        await page.close()


async def first_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=1000):
                return selector
        except Exception:
            continue
    return None


async def execute_real_task(task: dict, adapter, pool):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    lottery_url = task.get("raw_url") or task.get("canonical_url")
    phases = requested_phases(task, require_plan=True)
    current_phase = await get_latest_phase(task_id) or "init"
    if current_phase not in PHASE_ORDER and current_phase != "init":
        return
    phase_fn = {
        "followed": adapter._follow,
        "liked": adapter._like,
        "commented": adapter._comment,
        "reposted": adapter._repost,
    }
    platform = task.get("platform", "bilibili")
    profile_dir = task.get("profile_dir", f"/profiles/{platform}/account_{account_id}")
    proxy = task.get("proxy")
    ctx = await pool.get_account_context(account_id, profile_dir, proxy)
    await prepare_account_login(ctx, account_id, platform)
    page = await ctx.new_page()
    try:
        await page.goto(lottery_url, wait_until="domcontentloaded", timeout=30000)
        await refresh_task_lease(task_id)
        await page.wait_for_timeout(2000)
        await detect_page_risk(page, account_id, platform)
        if current_phase == "init":
            remaining_phases = phases
        else:
            completed_index = PHASE_ORDER.index(current_phase)
            remaining_phases = [phase for phase in phases if PHASE_ORDER.index(phase) > completed_index]
        for phase_name in remaining_phases:
            await refresh_task_lease(task_id)
            await detect_page_risk(page, account_id, platform)
            await phase_fn[phase_name](page)
            await detect_page_risk(page, account_id, platform)
            await save_phase(task_id, account_id, lottery_id, phase_name)
        await save_phase(task_id, account_id, lottery_id, "completed")
    except Exception:
        await capture_failure_screenshot(page, task_id)
        raise
    finally:
        await page.close()


async def capture_failure_screenshot(page, task_id: str) -> str | None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{task_id}.png"
        await page.screenshot(path=str(path), full_page=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        await database.execute("UPDATE task_runs SET screenshot_path = :path WHERE task_id = :task_id", {"path": str(path), "task_id": task_id})
        row = await database.fetch_one("SELECT account_id, lottery_id FROM task_runs WHERE task_id = :task_id", {"task_id": task_id})
        await database.execute(
            """INSERT INTO evidence_files (evidence_type, task_id, account_id, lottery_id, file_path, sha256)
               VALUES ('task_failure_screenshot', :task_id, :account_id, :lottery_id, :file_path, :sha256)""",
            {"task_id": task_id, "account_id": row["account_id"] if row else None, "lottery_id": row["lottery_id"] if row else None, "file_path": str(path), "sha256": digest},
        )
        await record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="EvidenceCaptured",
            payload={"evidence_type": "task_failure_screenshot", "account_id": row["account_id"] if row else None, "lottery_id": row["lottery_id"] if row else None, "file_path": str(path), "sha256": digest},
            correlation_id=task_id,
        )
        return str(path)
    except Exception as e:
        structured_log("error", "failure_screenshot_failed", task_id=task_id, exception=e)
        return None


async def capture_shadow_screenshot(page, task_id: str, account_id: int, lottery_id: int, visible_phases: dict) -> str | None:
    try:
        SHADOW_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHADOW_SCREENSHOT_DIR / f"{task_id}.png"
        await page.screenshot(path=str(path), full_page=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        await database.execute("UPDATE task_runs SET screenshot_path = :path WHERE task_id = :task_id", {"path": str(path), "task_id": task_id})
        await database.execute(
            """INSERT INTO evidence_files (evidence_type, task_id, account_id, lottery_id, file_path, sha256)
               VALUES ('shadow_run_screenshot', :task_id, :account_id, :lottery_id, :file_path, :sha256)""",
            {"task_id": task_id, "account_id": account_id, "lottery_id": lottery_id, "file_path": str(path), "sha256": digest},
        )
        await record_event(
            aggregate="task",
            aggregate_id=task_id,
            event_type="EvidenceCaptured",
            payload={"evidence_type": "shadow_run_screenshot", "account_id": account_id, "lottery_id": lottery_id, "file_path": str(path), "sha256": digest, "visible_phases": visible_phases},
            correlation_id=task_id,
        )
        return str(path)
    except Exception as e:
        structured_log("error", "shadow_screenshot_failed", task_id=task_id, exception=e)
        return None


async def prepare_account_login(ctx, account_id: int, platform: str):
    row = await database.fetch_one("SELECT encrypted_credential FROM accounts WHERE id = :id", {"id": account_id})
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    try:
        credential = cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception:
        credential = credential_blob.decode("utf-8") if isinstance(credential_blob, bytes) else str(credential_blob)
    await inject_account_cookies(ctx, platform, credential)


def _require_int(task: dict, field: str) -> int:
    value = task.get(field)
    try:
        return int(value)
    except Exception as exc:
        raise InvalidTaskMessage(f"{field} must be an integer") from exc


def validate_task_message(task: dict) -> dict:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise InvalidTaskMessage("task_id is required")
    account_id = _require_int(task, "account_id")
    lottery_id = _require_int(task, "lottery_id")
    platform = str(task.get("platform") or "bilibili").strip() or "bilibili"
    mode = normalize_task_mode(task)
    task["task_id"] = task_id
    task["account_id"] = str(account_id)
    task["lottery_id"] = str(lottery_id)
    task["platform"] = platform
    task["mode"] = mode
    return task


async def dead_letter_message(msg_id: str, task: dict, reason: str):
    task_id = str(task.get("task_id") or "") or None
    payload = json.dumps(task, ensure_ascii=False)
    try:
        await database.execute(
            """INSERT INTO failed_task_messages (stream_key, message_id, task_id, reason, payload)
               VALUES (:stream_key, :message_id, :task_id, :reason, :payload)""",
            {"stream_key": STREAM_KEY, "message_id": str(msg_id), "task_id": task_id, "reason": reason[:255], "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_db_failed", message_id=msg_id, task_id=task_id, error=str(exc))
    try:
        await redis.xadd(
            "failed_task_messages",
            {"stream_key": STREAM_KEY, "message_id": str(msg_id), "task_id": task_id or "", "reason": reason[:255], "payload": payload},
        )
    except Exception as exc:
        structured_log("error", "dead_letter_stream_failed", message_id=msg_id, task_id=task_id, error=str(exc))


async def execute_task_with_phases(task: dict, adapter, pool, stream_message_id: str | None = None):
    task_id = task.get("task_id")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    task_mode = normalize_task_mode(task)
    try:
        if task_mode == "real_run" and not getattr(adapter, "REAL_ACTIONS", False):
            raise RuntimeError(f"Real actions for {adapter.PLATFORM} are not implemented")
        await ensure_account_can_run(account_id, task.get("platform", "bilibili"))
        await mark_task_started(task_id, account_id, lottery_id, task_mode, stream_message_id)
        if task_mode == "dry_run":
            await execute_dry_run(task_id, account_id, lottery_id, requested_phases(task, require_plan=False))
        elif task_mode == "shadow_run":
            await execute_shadow_run(task, adapter, pool)
        else:
            await execute_real_task(task, adapter, pool)
        await mark_task_finished(task_id, account_id, lottery_id, task_mode, True)
        return True
    except TaskAlreadyTerminal as e:
        structured_log("info", "task_already_terminal_ack", task_id=task_id, reason=str(e))
        return True
    except Exception as e:
        screenshot_row = await database.fetch_one("SELECT screenshot_path FROM task_runs WHERE task_id = :task_id", {"task_id": task_id})
        await mark_task_finished(task_id, account_id, lottery_id, task_mode, False, str(e), screenshot_row["screenshot_path"] if screenshot_row else None)
        structured_log("error", "task_execution_failed", task_id=task_id, exception=e)
        return False


async def task_loop(pool: BrowserPool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass
    while not shutdown_event.is_set():
        try:
            msgs = await asyncio.wait_for(redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000), timeout=1)
            if not msgs:
                continue
            for msg_id, data in msgs[0][1]:
                task = {k: v for k, v in data.items()}
                structured_log("info", "task_received", task_id=task.get("task_id"), message_id=msg_id)
                try:
                    task = validate_task_message(task)
                except InvalidTaskMessage as exc:
                    await dead_letter_message(msg_id, task, str(exc))
                    await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
                    structured_log("error", "task_message_dead_lettered", message_id=msg_id, reason=str(exc))
                    continue
                selector_config = parse_json_field(task.get("selector_config")) or {}
                success = await execute_task_with_phases(task, get_adapter(task.get("platform", "bilibili"), selector_config), pool, str(msg_id))
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
                if not success:
                    structured_log("error", "task_failed", task_id=task.get("task_id"))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "task_loop_error", exception=e)
            await asyncio.sleep(5)


def parse_json_field(value):
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except Exception:
        return None


def normalize_task_mode(task: dict) -> str:
    raw_mode = str(task.get("mode") or "").strip().lower()
    if raw_mode in {"dry_run", "shadow_run", "real_run"}:
        return raw_mode
    dry_run = str(task.get("dry_run", "1")).lower() in {"1", "true", "yes"}
    return "dry_run" if dry_run else "real_run"


def requested_phases(task: dict, require_plan: bool) -> list[str]:
    plan = parse_json_field(task.get("action_plan"))
    if not isinstance(plan, dict):
        plan = {}
    actions = plan.get("required_actions")
    phases = [phase for phase in PHASE_ORDER if isinstance(actions, list) and phase in actions]
    if require_plan:
        if plan.get("review_required"):
            raise RuntimeError("Lottery rule requires review before real-run")
        if not phases:
            raise RuntimeError("Lottery action plan is missing required actions")
    return phases or list(PHASE_ORDER)
