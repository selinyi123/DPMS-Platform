import asyncio
import json
import os
import re
import socket

from app.action_plan import (
    BILIBILI_API_EXECUTION_PATH,
    ActionPlanV2Error,
    canonical_json_bytes,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
    validate_action_plan_v2,
)
from app.adapter_config import STRUCTURED_SELECTOR_PLATFORMS
from app.adapters.registry import get_adapter
from app.bilibili.preflight import API_PREFLIGHT_KIND, run_readonly_api_preflight
from app.bilibili.runtime import (
    extract_bilibili_dynamic_id,
)
from app.db import database, execute_affected_rows, redis
from app.event_store.service import record_event
from app.services.execution_evidence import API_PROBE_KIND, materialize_for_probe
from app.safety import detect_page_risk
from app.utils.cookies import inject_account_cookies
from app.utils.cookies import credential_to_cookie_header
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log
from app.utils.navigation_safety import (
    install_main_frame_navigation_guard,
    validated_platform_canonical_uri,
    validated_platform_content_url,
    validated_platform_navigation_url,
)


STREAM_KEY = "adapter_probe_requests"
GROUP_NAME = "adapter-probers"
CONSUMER_NAME = f"adapter-prober-{socket.gethostname()}-{os.getpid()}"
PROBE_IDLE_THRESHOLD_MS = 300_000
PROBE_RECLAIM_INTERVAL_SECONDS = 60
PROBE_LEASE_SECONDS = 900
BILIBILI_API_CONFIG_HASH = None  # compatibility alias; config is execution-revision scoped


def validated_probe_url(platform: str, value: str) -> str:
    """Restrict both queued targets and final redirects to platform hosts."""
    try:
        return validated_platform_navigation_url(platform, value)
    except ValueError as exc:
        raise ValueError("adapter_probe_target_not_allowed") from exc


async def probe_loop(pool, shutdown_event: asyncio.Event):
    try:
        await redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    last_reclaim_at = 0.0
    while not shutdown_event.is_set():
        try:
            now = asyncio.get_running_loop().time()
            if now - last_reclaim_at >= PROBE_RECLAIM_INTERVAL_SECONDS:
                await reclaim_stale_probe_messages()
                last_reclaim_at = now
            messages = await asyncio.wait_for(
                redis.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_KEY: ">"}, count=1, block=5000),
                timeout=1,
            )
            if not messages:
                continue
            for msg_id, data in messages[0][1]:
                await handle_probe(pool, {k: v for k, v in data.items()})
                await redis.xack(STREAM_KEY, GROUP_NAME, msg_id)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            structured_log("error", "adapter_probe_loop_error", exception=e)
            await asyncio.sleep(3)


async def load_probe_credential(account_id: int) -> str:
    row = await database.fetch_one(
        "SELECT encrypted_credential FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    try:
        return cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception:
        return (
            credential_blob.decode("utf-8")
            if isinstance(credential_blob, bytes)
            else str(credential_blob)
        )


async def _release_probe_lease(binding: dict) -> None:
    await database.execute(
        """UPDATE account_operation_leases
           SET released_at = NOW()
           WHERE lease_id = :lease_id AND account_id = :account_id
             AND generation = :generation AND operation_kind = 'adapter_probe'
             AND owner_id = :probe_id AND task_id IS NULL
             AND released_at IS NULL""",
        {
            "lease_id": binding["account_lease_id"],
            "account_id": binding["account_id"],
            "generation": binding["account_lease_generation"],
            "probe_id": binding["probe_id"],
        },
    )
    released = await database.fetch_one(
        """SELECT released_at FROM account_operation_leases
           WHERE lease_id = :lease_id AND account_id = :account_id
             AND generation = :generation""",
        {
            "lease_id": binding["account_lease_id"],
            "account_id": binding["account_id"],
            "generation": binding["account_lease_generation"],
        },
    )
    if not released or released["released_at"] is None:
        raise RuntimeError("adapter_probe_lease_release_failed")


async def settle_probe_success(
    binding: dict,
    *,
    result: dict,
    observation_kind: str | None = None,
    observation_hash: str | None = None,
    screenshot_path: str | None = None,
) -> None:
    encoded_result = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    async with database.transaction():
        lease_window = await database.fetch_one(
            """SELECT ac.status, ac.started_at, lease.acquired_at,
                       CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                       CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                       CASE WHEN lease.generation = (
                         SELECT MAX(newest.generation)
                         FROM account_operation_leases newest
                         WHERE newest.account_id = ac.account_id
                       ) THEN 1 ELSE 0 END AS lease_latest_generation,
                       (SELECT COUNT(*) FROM account_operation_leases live
                        WHERE live.account_id = ac.account_id
                          AND live.released_at IS NULL
                          AND live.expires_at > NOW()) AS active_account_lease_count
               FROM adapter_calibrations ac
               JOIN account_operation_leases lease
                 ON lease.lease_id = ac.account_lease_id
                AND lease.account_id = ac.account_id
                AND lease.generation = ac.account_lease_generation
               WHERE ac.probe_id = :probe_id
               FOR UPDATE""",
            {"probe_id": binding["probe_id"]},
        )
        if (
            not lease_window
            or str(lease_window["status"] or "").strip().lower() != "running"
            or lease_window["started_at"] is None
            or lease_window["acquired_at"] is None
            or lease_window["acquired_at"] > lease_window["started_at"]
            or int(lease_window["lease_active"] or 0) != 1
            or int(lease_window["lease_unreleased"] or 0) != 1
            or int(lease_window["lease_latest_generation"] or 0) != 1
            or int(lease_window["active_account_lease_count"] or 0) != 1
        ):
            raise RuntimeError("adapter_probe_lease_window_expired")
        settled = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'succeeded', result = :result,
                   observation_kind = :observation_kind,
                   observation_hash = :observation_hash,
                   screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE probe_id = :probe_id AND account_id = :account_id
                 AND lottery_id = :lottery_id AND status = 'running'
                 AND account_lease_id = :lease_id
                 AND account_lease_generation = :lease_generation""",
            {
                "probe_id": binding["probe_id"],
                "account_id": binding["account_id"],
                "lottery_id": binding["lottery_id"],
                "lease_id": binding["account_lease_id"],
                "lease_generation": binding["account_lease_generation"],
                "result": encoded_result,
                "observation_kind": observation_kind,
                "observation_hash": observation_hash,
                "screenshot_path": screenshot_path,
            },
            db=database,
        )
        if settled == 0:
            raise RuntimeError("adapter_probe_settlement_ownership_lost")
        await _release_probe_lease(binding)


async def settle_probe_failure(
    binding: dict,
    *,
    error: str,
    screenshot_path: str | None = None,
) -> bool:
    async with database.transaction():
        failed = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'failed', error_message = :error,
                   screenshot_path = :screenshot_path, finished_at = NOW()
               WHERE probe_id = :probe_id AND account_id = :account_id
                 AND lottery_id = :lottery_id AND status = 'running'
                 AND account_lease_id = :lease_id
                 AND account_lease_generation = :lease_generation""",
            {
                "probe_id": binding["probe_id"],
                "account_id": binding["account_id"],
                "lottery_id": binding["lottery_id"],
                "lease_id": binding["account_lease_id"],
                "lease_generation": binding["account_lease_generation"],
                "error": str(error or "adapter probe failed")[:2000],
                "screenshot_path": screenshot_path,
            },
            db=database,
        )
        if failed == 0:
            return False
        await _release_probe_lease(binding)
        return True


async def handle_probe(pool, probe: dict):
    try:
        binding = await claim_probe(probe)
    except (KeyError, TypeError, ValueError) as exc:
        await settle_rejected_probe_claim(probe, exc)
        structured_log(
            "error",
            "adapter_probe_claim_rejected",
            task_id=str(probe.get("probe_id") or ""),
            exception=exc,
        )
        return

    probe_id = binding["probe_id"]
    platform = binding["platform"]
    account_id = binding["account_id"]
    lottery_id = binding["lottery_id"]
    target_url = binding["target_url"]
    canonical_uri = binding["canonical_url"]
    adapter = get_adapter(platform)
    # Probe images previously bypassed the exclusive, identity-bound evidence
    # writer used by task/shadow screenshots. Until the shared evidence volume
    # and a reusable writer are authorized, persist selector observations only
    # and leave the image path empty rather than creating untrusted evidence.
    screenshot_path = None

    ctx = None
    page = None
    try:
        await record_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="AdapterProbeStarted",
            payload={"probe_id": probe_id, "platform": platform, "account_id": account_id, "target_url": target_url},
            correlation_id=probe_id,
        )
        if (
            platform == "bilibili"
            and binding.get("execution_path_id") == BILIBILI_API_EXECUTION_PATH
        ):
            credential = await load_probe_credential(account_id)
            cookie_header = credential_to_cookie_header(credential)
            if not cookie_header:
                raise RuntimeError("bilibili_api_preflight_cookie_missing")
            plan = validate_action_plan_v2(binding["action_plan"], reject_media=True)
            dynamic_id = extract_bilibili_dynamic_id(target_url, canonical_uri)
            preflight = await run_readonly_api_preflight(
                cookie_header=cookie_header,
                dynamic_id=dynamic_id,
                required_actions=plan.required_actions,
                execution_revision=binding["execution_revision"],
                config_hash=binding["config_hash"],
                expected_follow_handle=(
                    plan.follow_target_handle
                    if "followed" in plan.required_actions
                    else None
                ),
            )
            await settle_probe_success(
                binding,
                result=preflight.observation,
                observation_kind=API_PREFLIGHT_KIND,
                observation_hash=preflight.observation_hash,
            )
            await record_event(
                aggregate="lottery",
                aggregate_id=lottery_id,
                event_type="AdapterApiProbeSucceeded",
                payload={
                    "probe_id": probe_id,
                    "platform": platform,
                    "account_id": account_id,
                    "probe_kind": API_PREFLIGHT_KIND,
                    "observation_hash": preflight.observation_hash,
                    "target_identity": preflight.observation["target_identity"],
                    "required_actions": list(plan.required_actions),
                    "side_effects": False,
                },
                correlation_id=probe_id,
            )
            await materialize_for_probe(db=database, probe_id=probe_id)
            structured_log(
                "info",
                "adapter_api_probe_completed",
                task_id=probe_id,
                phase=platform,
            )
            return
        target_url = validated_probe_url(platform, target_url)
        ctx = await pool.get_account_context(account_id, f"/profiles/{platform}/account_{account_id}")
        await inject_probe_cookies(ctx, account_id, platform)
        page = await ctx.new_page()
        await install_main_frame_navigation_guard(page, platform)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await install_main_frame_navigation_guard(page, platform, canonical_uri)
        await page.wait_for_timeout(1500)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await detect_page_risk(page, account_id, platform)

        validated_platform_content_url(platform, page.url, canonical_uri)
        result = await probe_selectors(page, getattr(adapter, "SELECTOR_PROBES", {}))
        validated_platform_content_url(platform, page.url, canonical_uri)
        result["_summary"] = summarize_probe_result(platform, result)
        result["_recommended_config"] = build_recommended_config(platform, result)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await settle_probe_success(
            binding,
            result=result,
            screenshot_path=screenshot_path,
        )
        await record_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="AdapterProbeSucceeded",
            payload={
                "probe_id": probe_id,
                "platform": platform,
                "account_id": account_id,
                "result_summary": result.get("_summary"),
                "screenshot_path": screenshot_path,
            },
            correlation_id=probe_id,
        )
        structured_log("info", "adapter_probe_completed", task_id=probe_id, phase=platform)
    except asyncio.CancelledError:
        # A graceful shutdown must not leave the canonical calibration in
        # running forever. Returning lets probe_loop ACK this message when the
        # cancellation arrived inside handle_probe; if cancellation races the
        # ACK, the next worker's stale-PENDING reconciler closes it.
        await asyncio.shield(
            settle_probe_failure(
                binding,
                error="probe cancelled during worker shutdown",
            )
        )
        structured_log("warning", "adapter_probe_cancelled", task_id=probe_id)
        return
    except Exception as e:
        failed = await settle_probe_failure(
            binding,
            error=str(e),
            screenshot_path=screenshot_path,
        )
        if failed:
            try:
                await record_event(
                    aggregate="lottery",
                    aggregate_id=lottery_id,
                    event_type="AdapterProbeFailed",
                    payload={"probe_id": probe_id, "platform": platform, "account_id": account_id, "error": str(e), "screenshot_path": screenshot_path},
                    correlation_id=probe_id,
                )
            except Exception as event_exc:
                structured_log(
                    "error",
                    "adapter_probe_failure_event_failed",
                    task_id=probe_id,
                    exception=event_exc,
                )
            structured_log("error", "adapter_probe_failed", task_id=probe_id, exception=e)
        else:
            structured_log(
                "warning",
                "adapter_probe_terminal_state_already_owned",
                task_id=probe_id,
                exception=e,
            )
    finally:
        if page:
            try:
                await page.close()
            except Exception as close_exc:
                structured_log(
                    "warning",
                    "adapter_probe_page_close_failed",
                    task_id=probe_id,
                    exception=close_exc,
                )


async def reclaim_stale_probe_messages() -> int:
    """Terminally settle abandoned Probe PEL entries without replaying them.

    Probe is read-only, but it shares an account browser context. Replaying an
    ambiguous, ownerless browser operation could race a still-live process, so
    stale work is failed and ACKed; an operator can explicitly queue a fresh
    Probe after inspecting the failure.
    """

    pending = await redis.xpending_range(
        STREAM_KEY,
        GROUP_NAME,
        min="-",
        max="+",
        count=20,
    )
    settled = 0
    for entry in pending or []:
        idle_ms = int(entry.get("time_since_delivered") or 0)
        if idle_ms < PROBE_IDLE_THRESHOLD_MS:
            continue
        message_id = entry.get("message_id")
        if not message_id:
            continue
        claimed = await redis.xclaim(
            STREAM_KEY,
            GROUP_NAME,
            CONSUMER_NAME,
            min_idle_time=PROBE_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        _claimed_id, fields = claimed[0]
        probe_id = str((fields or {}).get("probe_id") or "").strip()
        state = await settle_stale_probe(probe_id)
        if state in {"failed", "terminal", "missing", "invalid"}:
            await redis.xack(STREAM_KEY, GROUP_NAME, message_id)
            settled += 1
            structured_log(
                "warning",
                "adapter_probe_stale_message_settled",
                task_id=probe_id or None,
                message_id=message_id,
                state=state,
            )
    return settled


async def settle_stale_probe(probe_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", probe_id or ""):
        return "invalid"
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT probe_id, account_id, lottery_id, status,
                      account_lease_id, account_lease_generation,
                      CASE WHEN started_at IS NOT NULL
                                 AND started_at < (NOW() - INTERVAL 5 MINUTE)
                           THEN 1 ELSE 0 END AS stale_running
               FROM adapter_calibrations
               WHERE probe_id = :probe_id
               FOR UPDATE""",
            {"probe_id": probe_id},
        )
        if not row:
            return "missing"
        status = str(row["status"] or "").strip().lower()
        binding = {
            "probe_id": str(row["probe_id"] or "").strip(),
            "account_id": int(row["account_id"] or 0),
            "lottery_id": int(row["lottery_id"] or 0),
            "account_lease_id": str(row["account_lease_id"] or "").strip(),
            "account_lease_generation": int(row["account_lease_generation"] or 0),
        }
        if status in {"succeeded", "failed"}:
            if binding["account_lease_id"] and binding["account_lease_generation"] > 0:
                await _release_probe_lease(binding)
            return "terminal"
        if status == "running" and int(row["stale_running"] or 0) != 1:
            return "active"
        if status not in {"queued", "running"}:
            return "active"
        updated = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'failed', error_message = 'stale probe owner lost; explicit retry required', finished_at = NOW()
               WHERE probe_id = :probe_id AND status = :status""",
            {"probe_id": probe_id, "status": status},
            db=database,
        )
        if updated != 0:
            await _release_probe_lease(binding)
            return "failed"
        return "active"


async def settle_rejected_probe_claim(probe: dict, exc: BaseException) -> None:
    """Do not ACK a rejected queued calibration without terminal DB state."""

    probe_id = str(probe.get("probe_id") or "").strip()
    platform = str(probe.get("platform") or "").strip().lower()
    target_url = str(probe.get("target_url") or "").strip()
    try:
        account_id = int(probe.get("account_id"))
        lottery_id = int(probe.get("lottery_id"))
    except (TypeError, ValueError):
        return
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", probe_id)
        or not platform
        or account_id <= 0
        or lottery_id <= 0
        or not target_url
    ):
        return
    async with database.transaction():
        row = await database.fetch_one(
            """SELECT probe_id, platform, account_id, lottery_id, target_url,
                      status, account_lease_id, account_lease_generation
               FROM adapter_calibrations
               WHERE probe_id = :probe_id
               FOR UPDATE""",
            {"probe_id": probe_id},
        )
        if (
            not row
            or str(row["platform"] or "").strip().lower() != platform
            or int(row["account_id"] or 0) != account_id
            or int(row["lottery_id"] or 0) != lottery_id
            or str(row["target_url"] or "").strip() != target_url
            or str(row["status"] or "").strip().lower() != "queued"
        ):
            return
        binding = {
            "probe_id": probe_id,
            "account_id": account_id,
            "lottery_id": lottery_id,
            "account_lease_id": str(row["account_lease_id"] or "").strip(),
            "account_lease_generation": int(row["account_lease_generation"] or 0),
        }
        updated = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'failed', error_message = :error, finished_at = NOW()
               WHERE probe_id = :probe_id AND status = 'queued'""",
            {
                "probe_id": probe_id,
                "error": f"probe claim rejected: {type(exc).__name__}"[:255],
            },
            db=database,
        )
        if updated != 0:
            await _release_probe_lease(binding)


async def claim_probe(probe: dict) -> dict:
    """Claim one queued calibration only when the stream binding is exact."""
    probe_id = str(probe["probe_id"] or "").strip()
    platform = str(probe["platform"] or "").strip().lower()
    account_id = int(probe["account_id"])
    lottery_id = int(probe["lottery_id"])
    target_url = str(probe["target_url"] or "").strip()
    lease_id = str(probe.get("account_lease_id") or "").strip()
    lease_generation = int(probe.get("account_lease_generation"))
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", probe_id)
        or not platform
        or account_id <= 0
        or lottery_id <= 0
        or not target_url
        or not lease_id
        or lease_generation <= 0
    ):
        raise ValueError("adapter_probe_binding_invalid")

    async with database.transaction():
        row = await database.fetch_one(
            """SELECT ac.probe_id, ac.platform, ac.account_id, ac.lottery_id,
                      ac.target_url, ac.status, ac.execution_path_id,
                      ac.rule_snapshot_id, ac.target_hash, ac.rule_hash,
                      ac.action_plan_hash, ac.config_hash,
                      ac.account_lease_id, ac.account_lease_generation,
                      l.platform AS lottery_platform,
                      l.raw_url AS lottery_raw_url,
                      l.canonical_url AS canonical_url,
                      l.rule_text AS lottery_rule_text,
                      l.action_plan AS lottery_action_plan,
                      l.authoritative_rule_snapshot_id,
                      l.rule_hash AS lottery_rule_hash,
                      l.action_plan_hash AS lottery_action_plan_hash,
                      rs.rule_text AS snapshot_rule_text,
                      rs.rule_hash AS snapshot_rule_hash,
                      rs.is_complete AS snapshot_complete,
                      rs.attested_by AS snapshot_attested_by,
                      rs.attested_at AS snapshot_attested_at,
                      a.platform AS account_platform,
                      a.status AS account_status,
                      a.execution_revision,
                      CASE WHEN a.encrypted_credential IS NOT NULL
                                 AND OCTET_LENGTH(a.encrypted_credential) > 0
                           THEN 1 ELSE 0 END AS credential_present,
                      lease.lease_id, lease.generation AS lease_generation,
                      lease.operation_kind, lease.owner_id,
                      lease.task_id AS lease_task_id,
                      CASE WHEN lease.expires_at > NOW() THEN 1 ELSE 0 END AS lease_active,
                      CASE WHEN lease.released_at IS NULL THEN 1 ELSE 0 END AS lease_unreleased,
                      CASE WHEN lease.generation = (
                        SELECT MAX(newest.generation)
                        FROM account_operation_leases newest
                        WHERE newest.account_id = ac.account_id
                      ) THEN 1 ELSE 0 END AS lease_latest_generation,
                      (SELECT COUNT(*) FROM account_operation_leases live
                       WHERE live.account_id = ac.account_id
                         AND live.released_at IS NULL
                         AND live.expires_at > NOW()) AS active_account_lease_count
               FROM adapter_calibrations ac
               JOIN lotteries l ON l.id = ac.lottery_id
               JOIN accounts a ON a.id = ac.account_id
               LEFT JOIN lottery_rule_snapshots rs
                 ON rs.id = l.authoritative_rule_snapshot_id
                AND rs.lottery_id = l.id
               LEFT JOIN account_operation_leases lease
                 ON lease.lease_id = ac.account_lease_id
                AND lease.account_id = ac.account_id
                AND lease.generation = ac.account_lease_generation
               WHERE ac.probe_id = :probe_id
               FOR UPDATE""",
            {"probe_id": probe_id},
        )
        if not row:
            raise ValueError("adapter_probe_binding_missing")
        authoritative = {
            "probe_id": str(row["probe_id"] or "").strip(),
            "platform": str(row["platform"] or "").strip().lower(),
            "account_id": int(row["account_id"]),
            "lottery_id": int(row["lottery_id"]),
            "target_url": str(row["target_url"] or "").strip(),
            "status": str(row["status"] or "").strip().lower(),
            "lottery_platform": str(row["lottery_platform"] or "").strip().lower(),
            "lottery_raw_url": str(row["lottery_raw_url"] or "").strip(),
            "canonical_url": str(row["canonical_url"] or "").strip(),
            "account_platform": str(row["account_platform"] or "").strip().lower(),
            "account_status": str(row["account_status"] or "").strip().lower(),
            "execution_path_id": str(row["execution_path_id"] or "").strip(),
            "rule_snapshot_id": int(row["rule_snapshot_id"] or 0),
            "target_hash": str(row["target_hash"] or "").strip(),
            "rule_hash": str(row["rule_hash"] or "").strip(),
            "action_plan_hash": str(row["action_plan_hash"] or "").strip(),
            "config_hash": str(row["config_hash"] or "").strip(),
            "account_lease_id": str(row["account_lease_id"] or "").strip(),
            "account_lease_generation": int(row["account_lease_generation"] or 0),
        }
        expected = {
            "probe_id": probe_id,
            "platform": platform,
            "account_id": account_id,
            "lottery_id": lottery_id,
            "target_url": target_url,
        }
        if authoritative["status"] != "queued":
            raise ValueError("adapter_probe_not_queued")
        if any(authoritative[key] != value for key, value in expected.items()):
            raise ValueError("adapter_probe_binding_mismatch")
        if (
            authoritative["platform"] != authoritative["lottery_platform"]
            or authoritative["target_url"] != authoritative["lottery_raw_url"]
        ):
            raise ValueError("adapter_probe_lottery_binding_mismatch")
        if (
            authoritative["platform"] != authoritative["account_platform"]
            or authoritative["account_status"] != "ready"
            or int(row["credential_present"] or 0) != 1
        ):
            raise ValueError("adapter_probe_account_not_ready")
        if (
            authoritative["account_lease_id"] != lease_id
            or authoritative["account_lease_generation"] != lease_generation
            or str(row["lease_id"] or "").strip() != lease_id
            or int(row["lease_generation"] or 0) != lease_generation
            or str(row["operation_kind"] or "").strip().lower() != "adapter_probe"
            or str(row["owner_id"] or "").strip() != probe_id
            or row["lease_task_id"] is not None
            or int(row["lease_active"] or 0) != 1
            or int(row["lease_unreleased"] or 0) != 1
            or int(row["lease_latest_generation"] or 0) != 1
            or int(row["active_account_lease_count"] or 0) != 1
        ):
            raise ValueError("adapter_probe_account_lease_binding_invalid")
        authoritative["target_url"] = validated_probe_url(platform, authoritative["target_url"])
        authoritative["canonical_url"] = validated_platform_canonical_uri(
            platform,
            authoritative["canonical_url"],
        )
        if str(probe.get("canonical_url") or "").strip() != authoritative["canonical_url"]:
            raise ValueError("adapter_probe_canonical_target_mismatch")

        if platform == "bilibili":
            try:
                plan = validate_action_plan_v2(row["lottery_action_plan"], reject_media=True)
            except ActionPlanV2Error as exc:
                raise ValueError(f"adapter_probe_{exc.code}") from exc
            try:
                message_snapshot_id = int(probe.get("rule_snapshot_id"))
                execution_revision = int(probe.get("execution_revision"))
            except (TypeError, ValueError) as exc:
                raise ValueError("adapter_probe_api_binding_invalid") from exc
            expected_config_hash = compute_bilibili_api_config_hash(
                int(row["execution_revision"] or 0)
            )
            expected_target_hash = compute_target_hash(authoritative["canonical_url"])
            message_bindings = {
                "execution_path_id": str(probe.get("execution_path_id") or "").strip(),
                "target_hash": str(probe.get("target_hash") or "").strip(),
                "rule_hash": str(probe.get("rule_hash") or "").strip(),
                "action_plan_hash": str(probe.get("action_plan_hash") or "").strip(),
                "config_hash": str(probe.get("config_hash") or "").strip(),
            }
            if (
                plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
                or authoritative["execution_path_id"] != BILIBILI_API_EXECUTION_PATH
                or message_bindings["execution_path_id"] != BILIBILI_API_EXECUTION_PATH
                or authoritative["target_hash"] != expected_target_hash
                or message_bindings["target_hash"] != expected_target_hash
                or authoritative["rule_snapshot_id"] != plan.rule_snapshot_id
                or message_snapshot_id != plan.rule_snapshot_id
                or int(row["authoritative_rule_snapshot_id"] or 0) != plan.rule_snapshot_id
                or authoritative["rule_hash"] != plan.rule_hash
                or message_bindings["rule_hash"] != plan.rule_hash
                or str(row["lottery_rule_hash"] or "").strip() != plan.rule_hash
                or str(row["snapshot_rule_hash"] or "").strip() != plan.rule_hash
                or authoritative["action_plan_hash"] != plan.plan_hash
                or message_bindings["action_plan_hash"] != plan.plan_hash
                or str(row["lottery_action_plan_hash"] or "").strip() != plan.plan_hash
                or authoritative["config_hash"] != expected_config_hash
                or message_bindings["config_hash"] != expected_config_hash
                or execution_revision != int(row["execution_revision"] or 0)
                or int(row["snapshot_complete"] or 0) != 1
                or not str(row["snapshot_attested_by"] or "").strip()
                or row["snapshot_attested_at"] is None
            ):
                raise ValueError("adapter_probe_api_binding_mismatch")
            lottery_rule_text = row["lottery_rule_text"]
            snapshot_rule_text = row["snapshot_rule_text"]
            if isinstance(lottery_rule_text, bytes):
                lottery_rule_text = lottery_rule_text.decode("utf-8", errors="strict")
            if isinstance(snapshot_rule_text, bytes):
                snapshot_rule_text = snapshot_rule_text.decode("utf-8", errors="strict")
            if (
                not isinstance(lottery_rule_text, str)
                or lottery_rule_text != snapshot_rule_text
                or compute_rule_hash(lottery_rule_text) != plan.rule_hash
            ):
                raise ValueError("adapter_probe_rule_snapshot_mismatch")
            message_plan = probe.get("action_plan")
            if message_plan is not None:
                try:
                    parsed_message_plan = validate_action_plan_v2(
                        message_plan, reject_media=True
                    )
                except ActionPlanV2Error as exc:
                    raise ValueError(f"adapter_probe_message_{exc.code}") from exc
                if canonical_json_bytes(parsed_message_plan.plan) != canonical_json_bytes(plan.plan):
                    raise ValueError("adapter_probe_action_plan_mismatch")
            authoritative["action_plan"] = plan.plan
            authoritative["execution_revision"] = execution_revision

        claimed = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'running', started_at = NOW()
               WHERE probe_id = :probe_id AND status = 'queued'""",
            {"probe_id": probe_id},
            db=database,
        )
        if claimed == 0:
            raise ValueError("adapter_probe_claim_lost")
    return authoritative


async def inject_probe_cookies(ctx, account_id: int, platform: str):
    credential = await load_probe_credential(account_id)
    await inject_account_cookies(ctx, platform, credential)


async def probe_selectors(page, selector_groups: dict[str, list[str]]) -> dict:
    output = {}
    for phase, selectors in selector_groups.items():
        phase_result = []
        for selector in selectors:
            item = {"selector": selector, "visible": False, "count": 0, "error": None}
            try:
                locator = page.locator(selector)
                item["count"] = await locator.count()
                if item["count"]:
                    item["visible"] = await locator.first.is_visible(timeout=1000)
            except Exception as e:
                item["error"] = str(e)
            phase_result.append(item)
        output[phase] = phase_result
    return output


def summarize_probe_result(platform: str, result: dict) -> dict:
    """Summarize selector visibility without claiming execution readiness.

    ``ready_for_real_actions`` is retained as a compatibility alias because
    Core and previously persisted probe results still consume that key. Its
    value only means that selectors for every required phase were observed; it
    is not evidence that an action succeeded or that the real-run gate passed.
    New consumers should prefer ``selector_observation_complete``.
    """
    phases = ["followed", "liked", "commented", "reposted"]
    phase_status = {}
    visible_phases = []
    for phase in phases:
        candidates = result.get(phase) if isinstance(result.get(phase), list) else []
        visible = [item for item in candidates if item.get("visible") and item.get("selector")]
        ready = bool(visible)
        if platform in STRUCTURED_SELECTOR_PLATFORMS and phase == "commented":
            ready = bool(
                first_selector_matching(visible, ["textarea", "contenteditable", "placeholder", "textbox"])
                and first_selector_matching(visible, ["button", "\u53d1\u5e03", "\u8bc4\u8bba", "\u53d1\u9001", "submit", "publish"])
            )
        phase_status[phase] = {
            "candidate_count": len(candidates),
            "visible_count": len(visible),
            "ready": ready,
            "visible_selectors": [item["selector"] for item in visible],
        }
        if ready:
            visible_phases.append(phase)
    selector_observation_complete = len(visible_phases) == len(phases)
    return {
        "platform": platform,
        "required_phases": phases,
        "visible_phases": visible_phases,
        "missing_phases": [phase for phase in phases if phase not in visible_phases],
        "ready_phase_count": len(visible_phases),
        "selector_observation_complete": selector_observation_complete,
        # Compatibility only. Do not present this legacy name to operators.
        "ready_for_real_actions": selector_observation_complete,
        "phase_status": phase_status,
    }


def build_recommended_config(platform: str, result: dict) -> dict:
    phases = {}
    for phase in ["followed", "liked", "reposted"]:
        selector = first_visible_selector(result.get(phase))
        if selector:
            phases[phase] = [selector]

    comment_candidates = [item for item in result.get("commented", []) if item.get("visible") and item.get("selector")]
    input_selector = first_selector_matching(comment_candidates, ["textarea", "contenteditable", "placeholder"])
    submit_selector = first_selector_matching(comment_candidates, ["button", "发布", "评论", "发送", "submit", "publish"])
    if input_selector and submit_selector and input_selector != submit_selector:
        phases["commented"] = {"input": [input_selector], "submit": [submit_selector], "text": "\u53c2\u4e0e\u62bd\u5956"}

    return {platform: phases} if phases else {}


def first_visible_selector(candidates) -> str | None:
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if item.get("visible") and item.get("selector"):
            return item["selector"]
    return None


def first_selector_matching(candidates: list[dict], markers: list[str]) -> str | None:
    for item in candidates:
        selector = str(item.get("selector") or "")
        lower = selector.lower()
        if any(marker.lower() in lower for marker in markers):
            return selector
    return None
