import asyncio
import importlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass

from app.action_plan import (
    compute_config_hash,
    compute_target_hash,
)
from app.adapter_config import STRUCTURED_SELECTOR_PLATFORMS, load_selector_config
from app.adapters.registry import get_adapter
from app.db import database, execute_affected_rows, redis
from app.config import settings
from app.event_store.service import record_event
from app.platform_modules.base import PlatformRoutingError
from app.platform_modules.registry import get_platform_module
from app.platform_modules.services import ProbeExecutionServices
from app.runtime_lane_health import (
    RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS,
    record_runtime_lane_failure,
    record_runtime_lane_progress,
    record_runtime_lane_success,
)
from app.services.task_outbox import enqueue_event_outbox
from app.safety import detect_page_risk
from app.task_streams import SAFE_TERMINAL_STREAM_ACK_DELETE_LUA
from app.utils.cookies import inject_account_cookies
from app.utils.cookies import credential_to_cookie_header
from app.utils.crypto import CREDENTIAL_AAD, cookie_vault
from app.utils.log import structured_log
from app.worker_identity import WORKER_ID
from app.adapter_probe_streams import (
    ADAPTER_PROBE_STREAM_FIELDS,
    AdapterProbeStreamBinding,
    LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
    LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
    LEGACY_ADAPTER_PROBE_GROUP_NAME,
    LEGACY_ADAPTER_PROBE_STREAM_BINDING,
    LEGACY_ADAPTER_PROBE_STREAM_KEY,
    adapter_probe_stream_binding_for_key,
    adapter_probe_stream_binding_for_platform,
    adapter_probe_stream_bindings,
    validate_adapter_probe_stream_message,
)
from app.utils.navigation_safety import (
    install_main_frame_navigation_guard,
    validated_platform_canonical_uri,
    validated_platform_content_url,
    validated_platform_navigation_url,
)
from shared.platform_scope import normalize_platform_scope
from shared.redis_consumer_groups import verify_redis_consumer_group


# Compatibility aliases for operational tooling during the legacy drain.
STREAM_KEY = LEGACY_ADAPTER_PROBE_STREAM_KEY
GROUP_NAME = LEGACY_ADAPTER_PROBE_GROUP_NAME
# Match ``worker_heartbeats.worker_id`` exactly so readiness can distinguish a
# live Worker from a stale Redis consumer/Pending Entry List.
CONSUMER_NAME = WORKER_ID
PROBE_IDLE_THRESHOLD_MS = 300_000
PROBE_RECLAIM_INTERVAL_SECONDS = 60
PROBE_RECOVERY_TIMEOUT_SECONDS = 45
PROBE_LEASE_SECONDS = 900
# Finish or cancel the full post-lane handler before a claimed DB row can
# satisfy the five-minute stale-owner predicate. This covers claim, start event,
# remote observation, settlement, and evidence materialization.
PROBE_EXECUTION_TIMEOUT_SECONDS = 240
PROBE_DISPATCH_MAX_INFLIGHT = 32
PROBE_STREAM_READ_COUNT = 8
PROBE_PENDING_REFRESH_SECONDS = 30
LEGACY_PROBE_FANOUT_READ_COUNT = 50
LEGACY_PROBE_FANOUT_BLOCK_MS = 1000
LEGACY_PROBE_FANOUT_ENTRY_TIMEOUT_SECONDS = 30
LEGACY_PROBE_FANOUT_LUA = """
local fields = {}
for index = 3, #ARGV do
  fields[#fields + 1] = ARGV[index]
end
local target_id = redis.call('XADD', KEYS[2], '*', unpack(fields))
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return target_id
"""


async def _ack_terminal_probe_message(
    binding: AdapterProbeStreamBinding,
    message_id: str,
) -> None:
    """ACK and retire a terminal live-lane entry without skipping other groups."""

    if binding.legacy:
        await redis.xack(binding.stream_key, binding.group_name, message_id)
        return
    await redis.eval(
        SAFE_TERMINAL_STREAM_ACK_DELETE_LUA,
        1,
        binding.stream_key,
        binding.group_name,
        str(message_id),
    )
_PROBE_SOURCE_STREAM = ContextVar(
    "adapter_probe_source_stream",
    default=STREAM_KEY,
)
BILIBILI_API_CONFIG_HASH = None  # compatibility alias; config is execution-revision scoped

_LAZY_BILIBILI_SYMBOLS = {
    "API_PREFLIGHT_KIND": (
        "app.bilibili.preflight",
        "API_PREFLIGHT_KIND",
    ),
    "run_readonly_api_preflight": (
        "app.bilibili.preflight",
        "run_readonly_api_preflight",
    ),
    "extract_bilibili_dynamic_id": (
        "app.bilibili.runtime",
        "extract_bilibili_dynamic_id",
    ),
    "materialize_for_probe": (
        "app.services.execution_evidence",
        "materialize_for_probe",
    ),
}


def _bilibili_runtime_symbol(name: str):
    cached = globals().get(name)
    if cached is not None:
        return cached
    try:
        module_name, export_name = _LAZY_BILIBILI_SYMBOLS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(importlib.import_module(module_name), export_name)
    globals()[name] = value
    return value


def __getattr__(name: str):
    return _bilibili_runtime_symbol(name)

PROBE_STREAM_BINDING_FIELDS = ADAPTER_PROBE_STREAM_FIELDS
PROBE_PERSISTED_BINDING_FIELDS = PROBE_STREAM_BINDING_FIELDS - {
    "canonical_url",
    "execution_revision",
}


def validated_probe_url(platform: str, value: str) -> str:
    """Restrict both queued targets and final redirects to platform hosts."""
    try:
        return validated_platform_navigation_url(platform, value)
    except ValueError as exc:
        raise ValueError("adapter_probe_target_not_allowed") from exc


@dataclass
class _DispatchedProbeMessage:
    message_id: str
    probe: dict
    platform: str
    validation_error: BaseException | None = None
    waiting_for_platform: bool = True
    stream_key: str = STREAM_KEY
    group_name: str = GROUP_NAME


async def _execute_dispatched_probe(
    dispatched: _DispatchedProbeMessage,
    platform_lock: asyncio.Lock,
    pool,
) -> None:
    """Run one probe and ACK only after authoritative terminal settlement."""

    source_token = _PROBE_SOURCE_STREAM.set(dispatched.stream_key)
    try:
        async with platform_lock:
            # Waiting entries have no DB claim yet and are refreshed below.
            # Once execution starts, the existing probe lease/stale-running
            # recovery contract is authoritative and must remain observable.
            dispatched.waiting_for_platform = False
            if dispatched.validation_error is not None:
                acknowledge = await asyncio.wait_for(
                    settle_rejected_probe_claim(
                        dispatched.probe,
                        dispatched.validation_error,
                        source_stream_key=dispatched.stream_key,
                    ),
                    timeout=PROBE_EXECUTION_TIMEOUT_SECONDS,
                )
            else:
                acknowledge = await asyncio.wait_for(
                    handle_probe(pool, dispatched.probe),
                    timeout=PROBE_EXECUTION_TIMEOUT_SECONDS,
                )
            if acknowledge:
                await _ack_terminal_probe_message(
                    adapter_probe_stream_binding_for_key(
                        dispatched.stream_key
                    )
                    or LEGACY_ADAPTER_PROBE_STREAM_BINDING,
                    dispatched.message_id,
                )
            else:
                structured_log(
                    "warning",
                    "adapter_probe_message_retained_for_recovery",
                    task_id=str(dispatched.probe.get("probe_id") or "") or None,
                    message_id=dispatched.message_id,
                    platform=dispatched.platform,
                )
    except asyncio.CancelledError:
        structured_log(
            "info",
            "adapter_probe_dispatch_cancelled_pending_recovery",
            task_id=str(dispatched.probe.get("probe_id") or "") or None,
            message_id=dispatched.message_id,
            platform=dispatched.platform,
        )
        raise
    except Exception as exc:
        # Neither handler nor ACK failures may terminate sibling platform
        # lanes. An unconfirmed entry remains in PEL for stale reconciliation.
        structured_log(
            "error",
            "adapter_probe_dispatch_error",
            task_id=str(dispatched.probe.get("probe_id") or "") or None,
            message_id=dispatched.message_id,
            platform=dispatched.platform,
            exception=exc,
        )
    finally:
        _PROBE_SOURCE_STREAM.reset(source_token)


async def _refresh_waiting_probe_entries(
    inflight: dict[asyncio.Task, _DispatchedProbeMessage],
    shutdown_event: asyncio.Event,
    binding: AdapterProbeStreamBinding = LEGACY_ADAPTER_PROBE_STREAM_BINDING,
) -> None:
    """Keep only locally queued lane waiters below the stale PEL threshold."""

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=PROBE_PENDING_REFRESH_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass

        message_ids = [
            dispatched.message_id
            for execution_task, dispatched in tuple(inflight.items())
            if not execution_task.done() and dispatched.waiting_for_platform
        ]
        if not message_ids:
            continue
        try:
            await redis.xclaim(
                binding.stream_key,
                binding.group_name,
                CONSUMER_NAME,
                min_idle_time=0,
                message_ids=message_ids,
                justid=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "warning",
                "waiting_adapter_probe_pending_refresh_failed",
                message_count=len(message_ids),
                exception=exc,
            )


async def _wait_for_probe_dispatch_capacity(
    binding: AdapterProbeStreamBinding,
    inflight: dict[asyncio.Task, _DispatchedProbeMessage],
    shutdown_event: asyncio.Event,
) -> bool:
    """Bound local PEL ownership while allowing cross-platform read-ahead."""

    while len(inflight) >= PROBE_DISPATCH_MAX_INFLIGHT:
        record_runtime_lane_progress(
            "probe",
            binding.platform,
            saturated=True,
        )
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        try:
            done, _ = await asyncio.wait(
                (*tuple(inflight), shutdown_waiter),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=RUNTIME_LANE_PROGRESS_INTERVAL_SECONDS,
            )
        finally:
            if not shutdown_waiter.done():
                shutdown_waiter.cancel()
                await asyncio.gather(shutdown_waiter, return_exceptions=True)
        for completed in done:
            if completed is not shutdown_waiter:
                inflight.pop(completed, None)
        if shutdown_event.is_set():
            return False
    record_runtime_lane_progress(
        "probe",
        binding.platform,
        saturated=False,
    )
    return not shutdown_event.is_set()


def _probe_platform_for_dispatch(
    probe: dict,
    binding: AdapterProbeStreamBinding = LEGACY_ADAPTER_PROBE_STREAM_BINDING,
) -> tuple[str, BaseException | None]:
    """Validate an untrusted envelope against its already-selected lane."""

    platform = str(probe.get("platform") or "").strip().lower()
    lane_platform = binding.platform or platform or "__invalid__"
    try:
        validate_adapter_probe_stream_message(binding, probe)
        get_platform_module(platform)
    except (PlatformRoutingError, ValueError) as exc:
        # The stream key, not a message field, owns the execution lane.
        return lane_platform, exc
    return platform, None


async def _probe_recovery_loop(
    binding: AdapterProbeStreamBinding,
    shutdown_event: asyncio.Event,
) -> None:
    """Run stale repair independently for one platform PEL."""

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                reclaim_stale_probe_messages(binding),
                timeout=PROBE_RECOVERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            structured_log(
                "error",
                "adapter_probe_recovery_deadline_exceeded",
                stream=binding.stream_key,
                platform=binding.platform,
                exception=exc,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structured_log(
                "error",
                "adapter_probe_recovery_loop_failed",
                stream=binding.stream_key,
                platform=binding.platform,
                exception=exc,
            )
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=PROBE_RECLAIM_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


async def _ensure_probe_group(binding: AdapterProbeStreamBinding) -> None:
    await verify_redis_consumer_group(
        redis,
        stream_key=binding.stream_key,
        group_name=binding.group_name,
    )


async def _consume_probe_lane(
    binding: AdapterProbeStreamBinding,
    pool,
    shutdown_event: asyncio.Event,
) -> None:
    """Consume one platform lane with an independent read and PEL budget."""

    platform_lock = asyncio.Lock()
    inflight: dict[asyncio.Task, _DispatchedProbeMessage] = {}
    pending_refresh_task = asyncio.create_task(
        _refresh_waiting_probe_entries(inflight, shutdown_event, binding)
    )
    recovery_task = asyncio.create_task(
        _probe_recovery_loop(binding, shutdown_event)
    )
    try:
        while not shutdown_event.is_set():
            if not await _wait_for_probe_dispatch_capacity(
                binding,
                inflight,
                shutdown_event,
            ):
                break
            try:
                available = PROBE_DISPATCH_MAX_INFLIGHT - len(inflight)
                messages = await asyncio.wait_for(
                    redis.xreadgroup(
                        binding.group_name,
                        CONSUMER_NAME,
                        {binding.stream_key: ">"},
                        count=min(PROBE_STREAM_READ_COUNT, available),
                        block=5000,
                    ),
                    # Let Redis finish its blocking read normally. Timing out
                    # before the 5s BLOCK value repeatedly cancels commands,
                    # churns pooled connections and can manufacture lane flap.
                    timeout=6,
                )
                record_runtime_lane_success(
                    "probe",
                    binding.platform,
                )
                if not messages:
                    continue
                for stream_name, entries in messages:
                    if str(stream_name) != binding.stream_key:
                        structured_log(
                            "error",
                            "adapter_probe_stream_response_mismatch",
                            expected_stream=binding.stream_key,
                            actual_stream=str(stream_name),
                            platform=binding.platform,
                        )
                        continue
                    for msg_id, data in entries:
                        probe = {k: v for k, v in data.items()}
                        platform, validation_error = _probe_platform_for_dispatch(
                            probe,
                            binding,
                        )
                        dispatched = _DispatchedProbeMessage(
                            message_id=str(msg_id),
                            probe=probe,
                            platform=platform,
                            validation_error=validation_error,
                            stream_key=binding.stream_key,
                            group_name=binding.group_name,
                        )
                        execution_task = asyncio.create_task(
                            _execute_dispatched_probe(
                                dispatched,
                                platform_lock,
                                pool,
                            )
                        )
                        inflight[execution_task] = dispatched

                        def discard_completed(completed: asyncio.Task) -> None:
                            inflight.pop(completed, None)

                        execution_task.add_done_callback(discard_completed)
            except asyncio.TimeoutError as exc:
                record_runtime_lane_failure(
                    "probe",
                    binding.platform,
                    exc,
                )
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "adapter_probe_loop_error",
                    stream=binding.stream_key,
                    platform=binding.platform,
                    exception=exc,
                )
                raise
    finally:
        pending_refresh_task.cancel()
        recovery_task.cancel()
        execution_tasks = tuple(inflight)
        for execution_task in execution_tasks:
            execution_task.cancel()
        await asyncio.gather(
            pending_refresh_task,
            recovery_task,
            *execution_tasks,
            return_exceptions=True,
        )


async def _probe_lane_loop(
    binding: AdapterProbeStreamBinding,
    pool,
    shutdown_event: asyncio.Event,
) -> None:
    """Keep one broken stream/group inside its platform failure domain."""

    while not shutdown_event.is_set():
        try:
            await _ensure_probe_group(binding)
            await _consume_probe_lane(binding, pool, shutdown_event)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_runtime_lane_failure(
                "probe",
                binding.platform,
                exc,
            )
            structured_log(
                "error",
                "adapter_probe_lane_unavailable",
                stream=binding.stream_key,
                platform=binding.platform,
                exception=exc,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass


async def probe_loop(
    pool,
    shutdown_event: asyncio.Event,
    *,
    platforms=None,
    include_legacy_fanout: bool | None = None,
):
    """Supervise only the live lanes owned by this Worker process."""

    selected_platforms = normalize_platform_scope(
        "all" if platforms is None else platforms
    )
    lane_tasks = [
        asyncio.create_task(
            _probe_lane_loop(binding, pool, shutdown_event),
            name=f"adapter-probe:{binding.platform}",
        )
        for binding in adapter_probe_stream_bindings(include_legacy=False)
        if binding.platform in selected_platforms
    ]
    legacy_fanout_enabled = (
        settings.legacy_control_stream_drain_enabled
        if include_legacy_fanout is None
        else bool(include_legacy_fanout)
    )
    if legacy_fanout_enabled:
        lane_tasks.append(
            asyncio.create_task(
                _legacy_probe_fanout_loop(shutdown_event),
                name="adapter-probe:legacy-fanout",
            )
        )
    try:
        await asyncio.gather(*lane_tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for lane_task in lane_tasks:
            lane_task.cancel()
        await asyncio.gather(*lane_tasks, return_exceptions=True)


async def _legacy_probe_fanout_loop(
    shutdown_event: asyncio.Event,
) -> None:
    """Drain historical shared entries into isolated platform lanes."""

    group_ready = False
    last_reclaim_at = 0.0
    while not shutdown_event.is_set():
        try:
            if not group_ready:
                await verify_redis_consumer_group(
                    redis,
                    stream_key=LEGACY_ADAPTER_PROBE_STREAM_KEY,
                    group_name=LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
                )
                group_ready = True
            now = asyncio.get_running_loop().time()
            if now - last_reclaim_at >= PROBE_RECLAIM_INTERVAL_SECONDS:
                await _reclaim_legacy_probe_fanout_messages()
                last_reclaim_at = now
            messages = await asyncio.wait_for(
                redis.xreadgroup(
                    LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
                    LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
                    {LEGACY_ADAPTER_PROBE_STREAM_KEY: ">"},
                    count=LEGACY_PROBE_FANOUT_READ_COUNT,
                    block=LEGACY_PROBE_FANOUT_BLOCK_MS,
                ),
                timeout=2,
            )
            for stream_name, entries in messages or ():
                if str(stream_name) != LEGACY_ADAPTER_PROBE_STREAM_KEY:
                    structured_log(
                        "error",
                        "legacy_adapter_probe_fanout_stream_mismatch",
                        actual_stream=str(stream_name),
                    )
                    continue
                await asyncio.gather(
                    *(
                        _process_legacy_probe_fanout_entry(
                            str(message_id),
                            dict(fields or {}),
                        )
                        for message_id, fields in entries
                    )
                )
            record_runtime_lane_success("legacy_probe_fanout")
        except asyncio.TimeoutError:
            record_runtime_lane_success("legacy_probe_fanout")
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            group_ready = False
            record_runtime_lane_failure(
                "legacy_probe_fanout",
                None,
                exc,
            )
            structured_log(
                "error",
                "legacy_adapter_probe_fanout_loop_failed",
                exception=exc,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass


async def legacy_probe_fanout_loop(
    shutdown_event: asyncio.Event,
) -> None:
    """Public control-worker entrypoint for the legacy shared stream."""

    await _legacy_probe_fanout_loop(shutdown_event)


async def _process_legacy_probe_fanout_entry(
    message_id: str,
    fields: dict,
) -> bool:
    try:
        return await asyncio.wait_for(
            _fanout_legacy_probe_message(message_id, fields),
            timeout=LEGACY_PROBE_FANOUT_ENTRY_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The source remains in the fanout PEL. The Redis transfer itself is
        # atomic, so an unknown script outcome is either fully transferred and
        # ACKed or fully retained.
        structured_log(
            "error",
            "legacy_adapter_probe_fanout_entry_failed",
            message_id=message_id,
            exception=exc,
        )
        return False


async def _fanout_legacy_probe_message(
    message_id: str,
    fields: dict,
) -> bool:
    async with database.transaction():
        state, row, binding = (
            await _lock_authoritative_probe_stream_binding(
                fields,
                source_stream_key=LEGACY_ADAPTER_PROBE_STREAM_KEY,
                allow_legacy_authority_migration=True,
            )
        )
        if state in {"invalid", "missing", "binding_mismatch"}:
            discard_source = True
            status = ""
        elif state != "exact" or row is None or binding is None:
            return False
        else:
            discard_source = False
            status = str(row["status"] or "").strip().casefold()
            if status not in {
                "queued",
                "running",
                "succeeded",
                "failed",
            }:
                return False

    if discard_source:
        await redis.xack(
            LEGACY_ADAPTER_PROBE_STREAM_KEY,
            LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
            message_id,
        )
        return True
    if binding is None:
        return False
    if status in {"succeeded", "failed"}:
        if status == "succeeded":
            platform_module = get_platform_module(binding["platform"])
            await platform_module.materialize_terminal_probe(
                probe_id=binding["probe_id"],
                runtime=_probe_execution_services(),
            )
        await redis.xack(
            LEGACY_ADAPTER_PROBE_STREAM_KEY,
            LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
            message_id,
        )
        return True
    target = adapter_probe_stream_binding_for_platform(
        binding["platform"]
    )
    authoritative_message = binding["authoritative_message"]

    flattened = []
    for key, value in authoritative_message.items():
        flattened.extend((key, value))
    target_id = await redis.eval(
        LEGACY_PROBE_FANOUT_LUA,
        2,
        LEGACY_ADAPTER_PROBE_STREAM_KEY,
        target.stream_key,
        LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
        message_id,
        *flattened,
    )
    if not target_id:
        raise RuntimeError("legacy_adapter_probe_fanout_no_target_id")
    structured_log(
        "info",
        "legacy_adapter_probe_fanned_out",
        task_id=binding["probe_id"],
        platform=binding["platform"],
        source_message_id=message_id,
        target_message_id=str(target_id),
        target_stream=target.stream_key,
    )
    return True


async def _reclaim_legacy_probe_fanout_messages() -> int:
    pending = await redis.xpending_range(
        LEGACY_ADAPTER_PROBE_STREAM_KEY,
        LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
        min="-",
        max="+",
        count=LEGACY_PROBE_FANOUT_READ_COUNT,
        idle=PROBE_IDLE_THRESHOLD_MS,
    )
    reclaimed = 0
    for entry in pending or ():
        message_id = entry.get("message_id")
        if not message_id:
            continue
        claimed = await redis.xclaim(
            LEGACY_ADAPTER_PROBE_STREAM_KEY,
            LEGACY_ADAPTER_PROBE_FANOUT_GROUP_NAME,
            LEGACY_ADAPTER_PROBE_FANOUT_CONSUMER_NAME,
            min_idle_time=PROBE_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        claimed_id, fields = claimed[0]
        if await _process_legacy_probe_fanout_entry(
            str(claimed_id),
            dict(fields or {}),
        ):
            reclaimed += 1
    return reclaimed


async def load_probe_credential(account_id: int) -> str:
    row = await database.fetch_one(
        "SELECT encrypted_credential FROM accounts WHERE id = :id",
        {"id": account_id},
    )
    if not row or not row["encrypted_credential"]:
        raise ValueError(f"Account {account_id} has no imported login Cookie")
    credential_blob = row["encrypted_credential"]
    if isinstance(credential_blob, memoryview):
        credential_blob = credential_blob.tobytes()
    try:
        return cookie_vault.decrypt(credential_blob, aad=CREDENTIAL_AAD)
    except Exception as exc:
        # ``CookieVault.decrypt`` already retains compatibility with legacy
        # AES-GCM ciphertexts that predate purpose-bound AAD. Treating a blob
        # that fails both decryptions as plaintext can send corrupted
        # ciphertext (or a value written outside the credential ingress) to a
        # platform. The probe boundary must therefore fail closed.
        raise ValueError("account_credential_decryption_failed") from exc


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
    success_event_type: str,
    success_event_payload: dict,
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
        await enqueue_event_outbox(
            aggregate="lottery",
            aggregate_id=binding["lottery_id"],
            event_type=success_event_type,
            payload={
                "probe_id": binding["probe_id"],
                "platform": binding["platform"],
                "account_id": binding["account_id"],
                **success_event_payload,
            },
            correlation_id=binding["probe_id"],
            dedup_key=f"adapter-probe-success:{binding['probe_id']}",
            db=database,
        )
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
        await enqueue_event_outbox(
            aggregate="lottery",
            aggregate_id=binding["lottery_id"],
            event_type="AdapterProbeFailed",
            payload={
                "probe_id": binding["probe_id"],
                "platform": binding["platform"],
                "account_id": binding["account_id"],
                "error": str(error or "adapter probe failed")[:2000],
                "screenshot_path": screenshot_path,
            },
            correlation_id=binding["probe_id"],
            dedup_key=f"adapter-probe-failed:{binding['probe_id']}",
            db=database,
        )
        await _release_probe_lease(binding)
        return True


@dataclass(frozen=True)
class ProbeObservation:
    result: dict
    success_event_type: str
    success_event_payload: dict
    observation_kind: str | None = None
    observation_hash: str | None = None
    screenshot_path: str | None = None
    materialize_execution_evidence: bool = False


def _probe_execution_services() -> ProbeExecutionServices:
    """Expose only platform-neutral probe capabilities to a module."""

    return ProbeExecutionServices(
        database=database,
        ProbeObservation=ProbeObservation,
        credential_to_cookie_header=credential_to_cookie_header,
        execute_browser_observation_probe=(
            execute_browser_observation_probe
        ),
        load_probe_credential=load_probe_credential,
    )


async def execute_bilibili_api_probe(binding: dict, pool) -> ProbeObservation:
    """Compatibility facade directed into Bilibili's owned implementation."""

    from app.platform_modules.bilibili import (
        execute_bilibili_api_probe as owned_probe,
    )

    return await owned_probe(
        binding,
        pool,
        runtime=_probe_execution_services(),
    )


async def execute_browser_observation_probe(
    binding: dict,
    pool,
) -> ProbeObservation:
    """Shared selector-observation infrastructure selected by a platform."""

    probe_id = binding["probe_id"]
    platform = binding["platform"]
    account_id = binding["account_id"]
    target_url = validated_probe_url(platform, binding["target_url"])
    canonical_uri = binding["canonical_url"]
    adapter = get_adapter(platform, binding.get("selector_config"))
    ctx = await pool.get_account_context(
        account_id,
        f"/profiles/{platform}/account_{account_id}",
        platform=platform,
    )
    await inject_probe_cookies(ctx, account_id, platform)
    page = await ctx.new_page()
    try:
        await install_main_frame_navigation_guard(page, platform)
        await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        validated_platform_content_url(platform, page.url, canonical_uri)
        await install_main_frame_navigation_guard(page, platform, canonical_uri)
        await page.wait_for_timeout(1500)
        validated_platform_content_url(platform, page.url, canonical_uri)
        await detect_page_risk(page, account_id, platform)

        validated_platform_content_url(platform, page.url, canonical_uri)
        result = await probe_selectors(
            page,
            getattr(adapter, "SELECTOR_PROBES", {}),
        )
        validated_platform_content_url(platform, page.url, canonical_uri)
        result["_summary"] = summarize_probe_result(platform, result)
        result["_recommended_config"] = build_recommended_config(platform, result)
        validated_platform_content_url(platform, page.url, canonical_uri)
        return ProbeObservation(
            result=result,
            success_event_type="AdapterProbeSucceeded",
            success_event_payload={
                "result_summary": result.get("_summary"),
                "screenshot_path": None,
            },
        )
    finally:
        try:
            await page.close()
        except Exception as close_exc:
            structured_log(
                "warning",
                "adapter_probe_page_close_failed",
                task_id=probe_id,
                exception=close_exc,
            )


async def handle_probe(pool, probe: dict) -> bool:
    """Execute a probe and report whether its message is safe to acknowledge."""

    try:
        binding = await claim_probe(probe)
    except (KeyError, TypeError, ValueError) as exc:
        settled = await settle_rejected_probe_claim(
            probe,
            exc,
            source_stream_key=_PROBE_SOURCE_STREAM.get(),
        )
        structured_log(
            "error",
            "adapter_probe_claim_rejected",
            task_id=str(probe.get("probe_id") or ""),
            exception=exc,
        )
        return settled

    probe_id = binding["probe_id"]
    platform = binding["platform"]
    account_id = binding["account_id"]
    lottery_id = binding["lottery_id"]
    target_url = binding["target_url"]
    # Probe images previously bypassed the exclusive, identity-bound evidence
    # writer used by task/shadow screenshots. Until the shared evidence volume
    # and a reusable writer are authorized, persist selector observations only
    # and leave the image path empty rather than creating untrusted evidence.
    screenshot_path = None
    terminal_settled = False
    try:
        await record_event(
            aggregate="lottery",
            aggregate_id=lottery_id,
            event_type="AdapterProbeStarted",
            payload={"probe_id": probe_id, "platform": platform, "account_id": account_id, "target_url": target_url},
            correlation_id=probe_id,
        )
        platform_module = get_platform_module(platform)
        observation = await platform_module.execute_probe(
            binding,
            pool,
            runtime=_probe_execution_services(),
        )
        if not isinstance(observation, ProbeObservation):
            raise RuntimeError("adapter_probe_observation_contract_invalid")
        screenshot_path = observation.screenshot_path
        await settle_probe_success(
            binding,
            result=observation.result,
            success_event_type=observation.success_event_type,
            success_event_payload=observation.success_event_payload,
            observation_kind=observation.observation_kind,
            observation_hash=observation.observation_hash,
            screenshot_path=screenshot_path,
        )
        terminal_settled = True
        if observation.materialize_execution_evidence:
            await platform_module.materialize_terminal_probe(
                probe_id,
                runtime=_probe_execution_services(),
            )
        structured_log(
            "info",
            (
                "adapter_api_probe_completed"
                if observation.observation_kind
                else "adapter_probe_completed"
            ),
            task_id=probe_id,
            phase=platform,
        )
        return True
    except asyncio.CancelledError:
        # The terminal row and event outbox are durable, but evidence
        # materialization may still be incomplete. Retain this PEL entry for
        # the idempotent stale reconciliation path before ACK.
        if terminal_settled:
            return False
        settled = await asyncio.shield(
            settle_probe_failure(
                binding,
                error="probe cancelled during worker shutdown",
            )
        )
        structured_log("warning", "adapter_probe_cancelled", task_id=probe_id)
        return settled
    except Exception as e:
        if terminal_settled:
            structured_log(
                "error",
                "adapter_probe_post_settlement_side_effect_failed",
                task_id=probe_id,
                exception=e,
            )
            return False
        failed = await settle_probe_failure(
            binding,
            error=str(e),
            screenshot_path=screenshot_path,
        )
        if failed:
            structured_log("error", "adapter_probe_failed", task_id=probe_id, exception=e)
        else:
            structured_log(
                "warning",
                "adapter_probe_terminal_state_already_owned",
                task_id=probe_id,
                exception=e,
            )
        return failed


def _normalize_probe_stream_fields(message: dict) -> dict[str, str]:
    """Normalize the scalar Redis/outbox representation without coercing objects."""

    if not isinstance(message, dict):
        raise ValueError("adapter_probe_stream_message_invalid")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in message.items():
        if isinstance(raw_key, memoryview):
            raw_key = raw_key.tobytes()
        if isinstance(raw_key, bytes):
            raw_key = raw_key.decode("utf-8", errors="strict")
        if not isinstance(raw_key, str):
            raise ValueError("adapter_probe_stream_message_invalid")

        if isinstance(raw_value, memoryview):
            raw_value = raw_value.tobytes()
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8", errors="strict")
        if not isinstance(raw_value, (str, int)) or isinstance(raw_value, bool):
            raise ValueError("adapter_probe_stream_message_invalid")
        normalized[raw_key] = str(raw_value)
    return normalized


def _parse_outbox_probe_payload(payload) -> dict[str, str]:
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="strict")
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _normalize_probe_stream_fields(payload)


async def _lock_authoritative_probe_stream_binding(
    probe: dict,
    *,
    source_stream_key: str = STREAM_KEY,
    allow_legacy_authority_migration: bool = False,
) -> tuple[str, dict | None, dict | None]:
    """Bind recovery/rejection to the exact payload committed by Core.

    A Redis PEL entry is untrusted.  Looking up only ``probe_id`` lets a forged
    entry fail an unrelated canonical probe.  The transactional outbox payload
    is immutable after insert and was committed with ``adapter_calibrations``,
    so it is the message authority for recovery as well as initial delivery.
    """

    try:
        incoming = _normalize_probe_stream_fields(probe)
    except (TypeError, ValueError, UnicodeError):
        return "invalid", None, None
    probe_id = incoming.get("probe_id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", probe_id):
        return "invalid", None, None

    source_binding = adapter_probe_stream_binding_for_key(source_stream_key)
    if source_binding is None:
        return "invalid", None, None
    outbox = await database.fetch_one(
        """SELECT stream_key, payload
           FROM outbox_events
           WHERE dedup_key = :dedup_key""",
        {"dedup_key": f"adapter-probe:{probe_id}"},
    )
    authoritative_message = None
    outbox_binding = None
    if outbox:
        outbox_binding = adapter_probe_stream_binding_for_key(
            str(outbox["stream_key"] or "").strip()
        )
        if outbox_binding is None:
            return "unverified", None, None
        try:
            authoritative_message = _parse_outbox_probe_payload(
                outbox["payload"]
            )
            validate_adapter_probe_stream_message(
                outbox_binding,
                authoritative_message,
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            UnicodeError,
        ):
            return "unverified", None, None
        if set(authoritative_message) != PROBE_STREAM_BINDING_FIELDS:
            return "unverified", None, None
        if authoritative_message.get("probe_id", "").strip() != probe_id:
            return "unverified", None, None
        source_matches_authority = (
            source_binding.stream_key == outbox_binding.stream_key
            or (
                outbox_binding.legacy
                and not source_binding.legacy
                and source_binding.platform
                == authoritative_message.get("platform")
            )
        )
        if not source_matches_authority or incoming != authoritative_message:
            # The authoritative outbox row remains untouched; this individual
            # forged/corrupted stream delivery is safe to discard.
            return "binding_mismatch", None, None

    row = await database.fetch_one(
        """SELECT probe_id, platform, account_id, lottery_id, target_url,
                  status, execution_path_id, target_hash, rule_snapshot_id,
                  rule_hash, action_plan_hash, config_hash,
                  account_lease_id, account_lease_generation,
                  l.canonical_url,
                  a.execution_revision,
                  CASE WHEN ac.started_at IS NOT NULL
                             AND ac.started_at < (NOW() - INTERVAL 5 MINUTE)
                       THEN 1 ELSE 0 END AS stale_running
           FROM adapter_calibrations ac
           LEFT JOIN lotteries l ON l.id = ac.lottery_id
           LEFT JOIN accounts a ON a.id = ac.account_id
           WHERE ac.probe_id = :probe_id
           FOR UPDATE""",
        {"probe_id": probe_id},
    )
    if not row:
        return "missing", None, None

    persisted = {
        "probe_id": str(row["probe_id"] or ""),
        "platform": str(row["platform"] or ""),
        "account_id": str(row["account_id"] or ""),
        "lottery_id": str(row["lottery_id"] or ""),
        "target_url": str(row["target_url"] or ""),
        "execution_path_id": str(row["execution_path_id"] or ""),
        "target_hash": str(row["target_hash"] or ""),
        "rule_snapshot_id": str(row["rule_snapshot_id"] or ""),
        "rule_hash": str(row["rule_hash"] or ""),
        "action_plan_hash": str(row["action_plan_hash"] or ""),
        "config_hash": str(row["config_hash"] or ""),
        "account_lease_id": str(row["account_lease_id"] or ""),
        "account_lease_generation": str(row["account_lease_generation"] or ""),
    }
    if authoritative_message is None:
        # Very old shared-stream entries predate transactional Outbox. Probe is
        # read-only, so the legacy fanout may migrate only an exact DB-bound
        # envelope into immutable Outbox authority before forwarding it.
        required_legacy_fields = {
            "probe_id",
            "platform",
            "account_id",
            "lottery_id",
            "target_url",
            "account_lease_id",
            "account_lease_generation",
        }
        if (
            not allow_legacy_authority_migration
            or not source_binding.legacy
            or not required_legacy_fields.issubset(incoming)
            or not set(incoming).issubset(PROBE_STREAM_BINDING_FIELDS)
            or any(
                incoming[key] != persisted[key]
                for key in required_legacy_fields
            )
        ):
            return "unverified", None, None
        authoritative_message = {
            **persisted,
            "canonical_url": str(row["canonical_url"] or ""),
            "execution_revision": str(row["execution_revision"] or ""),
        }
        try:
            validate_adapter_probe_stream_message(
                LEGACY_ADAPTER_PROBE_STREAM_BINDING,
                authoritative_message,
            )
        except ValueError:
            return "unverified", None, None
        # Any optional binding carried by the historical envelope must also
        # agree; ignored extra data can never be promoted to authority.
        if any(
            incoming[key] != authoritative_message[key]
            for key in incoming
        ):
            return "binding_mismatch", None, None

    try:
        execution_revision = int(authoritative_message["execution_revision"])
        lease_generation = int(
            authoritative_message["account_lease_generation"]
        )
        account_id = int(authoritative_message["account_id"])
        lottery_id = int(authoritative_message["lottery_id"])
    except (TypeError, ValueError):
        return "unverified", None, None
    if (
        execution_revision <= 0
        or lease_generation <= 0
        or account_id <= 0
        or lottery_id <= 0
        or not authoritative_message["account_lease_id"].strip()
        or authoritative_message["target_hash"]
        != compute_target_hash(authoritative_message["canonical_url"])
    ):
        return "unverified", None, None
    platform_module = get_platform_module(
        authoritative_message["platform"]
    )
    if not platform_module.validate_probe_authority(
        authoritative_message
    ):
        return "unverified", None, None

    if persisted != {
        key: authoritative_message[key] for key in PROBE_PERSISTED_BINDING_FIELDS
    }:
        return "unverified", None, None

    if not outbox:
        await database.execute(
            """INSERT INTO outbox_events
                 (stream_key, payload, status, dedup_key, sent_at)
               VALUES
                 (:stream_key, :payload, 'sent', :dedup_key, NOW())
               ON DUPLICATE KEY UPDATE id = id""",
            {
                "stream_key": LEGACY_ADAPTER_PROBE_STREAM_KEY,
                "payload": json.dumps(
                    authoritative_message,
                    ensure_ascii=False,
                ),
                "dedup_key": f"adapter-probe:{probe_id}",
            },
        )
        migrated = await database.fetch_one(
            """SELECT stream_key, payload
               FROM outbox_events
               WHERE dedup_key = :dedup_key""",
            {"dedup_key": f"adapter-probe:{probe_id}"},
        )
        if not migrated:
            return "unverified", None, None
        try:
            migrated_message = _parse_outbox_probe_payload(
                migrated["payload"]
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            UnicodeError,
        ):
            return "unverified", None, None
        if (
            str(migrated["stream_key"] or "").strip()
            != LEGACY_ADAPTER_PROBE_STREAM_KEY
            or migrated_message != authoritative_message
        ):
            return "unverified", None, None

    binding = {
        "probe_id": probe_id,
        "platform": authoritative_message["platform"],
        "account_id": account_id,
        "lottery_id": lottery_id,
        "account_lease_id": authoritative_message["account_lease_id"],
        "account_lease_generation": lease_generation,
        "authoritative_message": authoritative_message,
    }
    return "exact", dict(row), binding


async def reclaim_stale_probe_messages(
    binding: AdapterProbeStreamBinding = LEGACY_ADAPTER_PROBE_STREAM_BINDING,
) -> int:
    """Terminally settle abandoned Probe PEL entries without replaying them.

    Probe is read-only, but it shares an account browser context. Replaying an
    ambiguous, ownerless browser operation could race a still-live process, so
    stale work is failed and ACKed; an operator can explicitly queue a fresh
    Probe after inspecting the failure.
    """

    pending = await redis.xpending_range(
        binding.stream_key,
        binding.group_name,
        min="-",
        max="+",
        count=20,
        idle=PROBE_IDLE_THRESHOLD_MS,
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
            binding.stream_key,
            binding.group_name,
            CONSUMER_NAME,
            min_idle_time=PROBE_IDLE_THRESHOLD_MS,
            message_ids=[message_id],
        )
        if not claimed:
            continue
        _claimed_id, fields = claimed[0]
        probe_id = str((fields or {}).get("probe_id") or "").strip()
        state = await settle_stale_probe(
            dict(fields or {}),
            source_stream_key=binding.stream_key,
        )
        if state == "terminal":
            try:
                # Idempotent repair for a crash/cancellation after the probe
                # row and event outbox committed but before evidence pairing.
                platform_module = get_platform_module(
                    str(
                        (fields or {}).get("platform") or ""
                    ).strip().lower()
                )
                await platform_module.materialize_terminal_probe(
                    probe_id,
                    runtime=_probe_execution_services(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                structured_log(
                    "error",
                    "adapter_probe_terminal_materialization_repair_failed",
                    task_id=probe_id or None,
                    message_id=message_id,
                    exception=exc,
                )
                continue
        if state in {
            "failed",
            "terminal",
            "missing",
            "invalid",
            "binding_mismatch",
        }:
            await _ack_terminal_probe_message(binding, message_id)
            settled += 1
            structured_log(
                "warning",
                "adapter_probe_stale_message_settled",
                task_id=probe_id or None,
                message_id=message_id,
                stream=binding.stream_key,
                platform=binding.platform,
                state=state,
            )
    return settled


async def settle_stale_probe(
    probe: dict,
    *,
    source_stream_key: str = STREAM_KEY,
) -> str:
    async with database.transaction():
        state, row, binding = (
            await _lock_authoritative_probe_stream_binding(
                probe,
                source_stream_key=source_stream_key,
            )
        )
        if state != "exact" or row is None or binding is None:
            return state
        probe_id = binding["probe_id"]
        status = str(row["status"] or "").strip().lower()
        if status in {"succeeded", "failed"}:
            if binding["account_lease_id"] and binding["account_lease_generation"] > 0:
                await _release_probe_lease(binding)
            return "terminal"
        if status == "running" and int(row["stale_running"] or 0) != 1:
            return "active"
        if status not in {"queued", "running"}:
            return "active"
        stale_error = "stale probe owner lost; explicit retry required"
        updated = await execute_affected_rows(
            """UPDATE adapter_calibrations
               SET status = 'failed', error_message = :error, finished_at = NOW()
               WHERE probe_id = :probe_id AND status = :status""",
            {"probe_id": probe_id, "status": status, "error": stale_error},
            db=database,
        )
        if updated != 0:
            await enqueue_event_outbox(
                aggregate="lottery",
                aggregate_id=binding["lottery_id"],
                event_type="AdapterProbeFailed",
                payload={
                    "probe_id": probe_id,
                    "platform": binding["platform"],
                    "account_id": binding["account_id"],
                    "error": stale_error,
                    "screenshot_path": None,
                },
                correlation_id=probe_id,
                dedup_key=f"adapter-probe-failed:{probe_id}",
                db=database,
            )
            await _release_probe_lease(binding)
            return "failed"
        return "active"


async def settle_rejected_probe_claim(
    probe: dict,
    exc: BaseException,
    *,
    source_stream_key: str = STREAM_KEY,
) -> bool:
    """Return True only when a rejected message has authoritative terminal state."""

    terminal_materialization_probe_id = None
    async with database.transaction():
        state, row, binding = (
            await _lock_authoritative_probe_stream_binding(
                probe,
                source_stream_key=source_stream_key,
            )
        )
        if state != "exact" or row is None or binding is None:
            # Invalid, missing, or forged deliveries cannot mutate a canonical
            # row and are safe to discard. An unverified legacy/inconsistent
            # authority is retained for operator recovery.
            return state in {"invalid", "missing", "binding_mismatch"}
        probe_id = binding["probe_id"]
        status = str(row["status"] or "").strip().lower()
        if status in {"succeeded", "failed"}:
            if binding["account_lease_id"] and binding["account_lease_generation"] > 0:
                await _release_probe_lease(binding)
            if status == "succeeded":
                # A legacy-running delivery can race its old owner: the old
                # Worker may commit success after fan-out but crash before
                # platform-owned terminal materialization. Do not ACK the
                # target-lane duplicate until that idempotent hook succeeds.
                terminal_materialization_probe_id = probe_id
            else:
                return True
        if status != "queued":
            if terminal_materialization_probe_id is None:
                return False
        else:
            rejection_error = f"probe claim rejected: {type(exc).__name__}"[:255]
            updated = await execute_affected_rows(
                """UPDATE adapter_calibrations
                   SET status = 'failed', error_message = :error, finished_at = NOW()
                   WHERE probe_id = :probe_id AND status = 'queued'""",
                {
                    "probe_id": probe_id,
                    "error": rejection_error,
                },
                db=database,
            )
            if updated != 0:
                await enqueue_event_outbox(
                    aggregate="lottery",
                    aggregate_id=binding["lottery_id"],
                    event_type="AdapterProbeFailed",
                    payload={
                        "probe_id": probe_id,
                        "platform": binding["platform"],
                        "account_id": binding["account_id"],
                        "error": rejection_error,
                        "screenshot_path": None,
                    },
                    correlation_id=probe_id,
                    dedup_key=f"adapter-probe-failed:{probe_id}",
                    db=database,
                )
                await _release_probe_lease(binding)
                return True
            return False

    if terminal_materialization_probe_id is not None:
        try:
            platform_module = get_platform_module(
                binding["platform"]
            )
            await platform_module.materialize_terminal_probe(
                probe_id=terminal_materialization_probe_id,
                runtime=_probe_execution_services(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as materialization_exc:
            structured_log(
                "error",
                "adapter_probe_terminal_materialization_repair_failed",
                task_id=terminal_materialization_probe_id,
                exception=materialization_exc,
            )
            return False
    return False


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
    try:
        get_platform_module(platform)
    except PlatformRoutingError as exc:
        raise ValueError(exc.code) from exc

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

        platform_module = get_platform_module(platform)
        platform_claim_owned = await platform_module.validate_probe_claim(
            runtime=_probe_execution_services(),
            probe=probe,
            row=row,
            authoritative=authoritative,
        )
        if not platform_claim_owned:
            selector_config = load_selector_config()
            platform_selector_config = selector_config.get(platform, {})
            if not isinstance(platform_selector_config, dict):
                platform_selector_config = {}
            config_row = await database.fetch_one(
                """SELECT config_json FROM adapter_selector_configs
                   WHERE platform = :platform
                   FOR UPDATE""",
                {"platform": platform},
            )
            if config_row:
                raw_config = config_row["config_json"]
                if isinstance(raw_config, bytes):
                    raw_config = raw_config.decode("utf-8", errors="strict")
                try:
                    parsed_config = (
                        raw_config
                        if isinstance(raw_config, dict)
                        else json.loads(raw_config)
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_config = None
                if isinstance(parsed_config, dict):
                    platform_selector_config = parsed_config
            try:
                message_execution_revision = int(probe.get("execution_revision"))
            except (TypeError, ValueError) as exc:
                raise ValueError("adapter_probe_selector_binding_invalid") from exc
            execution_revision = int(row["execution_revision"] or 0)
            expected_config_hash = compute_config_hash(
                {
                    "platform": platform,
                    "execution_revision": execution_revision,
                    "selector_config": platform_selector_config,
                }
            )
            expected_target_hash = compute_target_hash(authoritative["canonical_url"])
            expected_execution_path = f"{platform}_selector_v1"
            message_bindings = {
                "execution_path_id": str(probe.get("execution_path_id") or "").strip(),
                "target_hash": str(probe.get("target_hash") or "").strip(),
                "rule_snapshot_id": str(probe.get("rule_snapshot_id") or "").strip(),
                "rule_hash": str(probe.get("rule_hash") or "").strip(),
                "action_plan_hash": str(probe.get("action_plan_hash") or "").strip(),
                "config_hash": str(probe.get("config_hash") or "").strip(),
            }
            if (
                authoritative["execution_path_id"] != expected_execution_path
                or message_bindings["execution_path_id"] != expected_execution_path
                or authoritative["target_hash"] != expected_target_hash
                or message_bindings["target_hash"] != expected_target_hash
                or authoritative["rule_snapshot_id"] != 0
                or message_bindings["rule_snapshot_id"]
                or authoritative["rule_hash"]
                or message_bindings["rule_hash"]
                or authoritative["action_plan_hash"]
                or message_bindings["action_plan_hash"]
                or authoritative["config_hash"] != expected_config_hash
                or message_bindings["config_hash"] != expected_config_hash
                or message_execution_revision != execution_revision
                or execution_revision <= 0
            ):
                raise ValueError("adapter_probe_selector_binding_mismatch")
            authoritative["selector_config"] = platform_selector_config
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
    Core and previously persisted probe results still consume that key. For
    supported platforms its value only means all phase selectors were seen;
    for manual-only platforms it is always false. New consumers should prefer
    ``selector_observation_complete`` plus the capability metadata.
    """
    platform_module = get_platform_module(platform)
    phases = list(platform_module.action_order)
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
    capability_block_reason = platform_module.capability_block_reason
    manual_confirmation_required = capability_block_reason is not None
    real_run_capable = capability_block_reason is None
    return {
        "platform": platform,
        "required_phases": phases,
        "visible_phases": visible_phases,
        "missing_phases": [phase for phase in phases if phase not in visible_phases],
        "ready_phase_count": len(visible_phases),
        "selector_observation_complete": selector_observation_complete,
        # Compatibility only. Selector visibility cannot override a platform
        # capability block or prove that a participant-side write API exists.
        "ready_for_real_actions": (
            selector_observation_complete and real_run_capable
        ),
        "manual_confirmation_required": manual_confirmation_required,
        "real_run_capable": real_run_capable,
        "capability_block_reason": capability_block_reason,
        "phase_status": phase_status,
    }


def build_recommended_config(platform: str, result: dict) -> dict:
    phases = {}
    normalized_platform = str(platform or "").strip().lower()
    platform_module = get_platform_module(normalized_platform)
    non_comment_phases = [
        phase for phase in platform_module.action_order if phase != "commented"
    ]
    for phase in non_comment_phases:
        selector = first_visible_selector(result.get(phase))
        if selector:
            if phase in platform_module.probe_done_selector_phases:
                phases[phase] = {"done": [selector]}
            else:
                phases[phase] = [selector]

    comment_candidates = [item for item in result.get("commented", []) if item.get("visible") and item.get("selector")]
    input_selector = first_selector_matching(comment_candidates, ["textarea", "contenteditable", "placeholder"])
    submit_selector = first_selector_matching(comment_candidates, ["button", "发布", "评论", "发送", "submit", "publish"])
    if input_selector and submit_selector and input_selector != submit_selector:
        phases["commented"] = {"input": [input_selector], "submit": [submit_selector], "text": "\u53c2\u4e0e\u62bd\u5956"}

    return {platform: phases} if phases else {}


def probe_phases_for_platform(platform: str) -> list[str]:
    return list(get_platform_module(platform).action_order)


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
