"""Weibo worker execution module."""

import asyncio
import unicodedata
from typing import Any, Mapping

from app.action_plan import (
    WEIBO_MAX_UNIQUE_HANDLES,
    ValidatedActionPlanV2,
    compute_config_hash,
    compute_target_hash,
)
from app.adapters.weibo import WeiboAdapter
from app.platform_modules.base import (
    ExecutionPath,
    PlatformModule,
    RealRunEvidenceBinding,
)
from app.platform_modules.contracts.weibo import (
    WEIBO_ACTION_PLAN_CONTRACT,
    WEIBO_ACTION_ORDER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
)
from app.platform_modules.evidence import (
    RealRunGateBlocked,
    json_object,
    row_value,
)
from app.platform_modules.errors import ExternalActionOutcomeUnknown
from app.platform_modules.shared_execution import (
    execute_browser_observation_probe,
    execute_browser_observation_shadow,
)
from shared.weibo_oauth_evidence import (
    WeiboOAuthCalibrationEnvelopeError,
    validate_weibo_oauth_calibration_envelope,
)
from app.weibo.client import (
    WeiboApiActionOutcomeUnknown,
    WeiboApiClient,
    WeiboApiRejected,
    build_weibo_mutation_request,
    status_identifier_from_canonical_uri,
)
from app.weibo.credentials import (
    WeiboOAuthCredentialError,
    decrypt_weibo_rip,
    parse_weibo_oauth_credential,
    weibo_rip_required,
)
from app.weibo.executor import (
    WeiboExecutionOutcomeUnknown,
    WeiboOAuthExecutor,
)


WEIBO_PREFLIGHT_TIMEOUT_SECONDS = 300
WEIBO_ACTION_HTTP_BUDGET_SECONDS = 20
WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS = 120


def _weibo_handle_identity_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


WEIBO_REAL_RUN_EVIDENCE_QUERY = """
SELECT
  oauth_cal.calibration_id AS oauth_calibration_id,
  oauth_cal.account_id AS oauth_calibration_account_id,
  oauth_cal.platform AS oauth_calibration_platform,
  oauth_cal.status AS oauth_calibration_status,
  oauth_cal.result AS oauth_calibration_result,
  oauth_cal.created_at AS oauth_calibration_created_at,
  oauth_cal.finished_at AS oauth_calibration_finished_at,
  CASE WHEN oauth_cal.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
             AND oauth_cal.created_at <= NOW()
             AND oauth_cal.finished_at IS NOT NULL
             AND oauth_cal.finished_at <= NOW()
       THEN 1 ELSE 0 END AS oauth_calibration_fresh
FROM task_runs tr
LEFT JOIN account_calibrations oauth_cal
  ON oauth_cal.calibration_id = tr.execution_evidence_id
 AND oauth_cal.account_id = tr.account_id
 AND oauth_cal.platform = 'weibo'
 AND oauth_cal.id = (
   SELECT latest_oauth.id
     FROM account_calibrations latest_oauth
     WHERE latest_oauth.account_id = tr.account_id
       AND latest_oauth.platform = 'weibo'
       AND latest_oauth.status = 'succeeded'
       AND JSON_UNQUOTE(
             JSON_EXTRACT(latest_oauth.result, '$.calibration_scope')
           ) = 'oauth_identity_and_capabilities'
    ORDER BY latest_oauth.id DESC
    LIMIT 1
 )
WHERE tr.task_id = :task_id
"""

_WEIBO_REAL_RUN_EVIDENCE_FIELDS = (
    "oauth_calibration_id",
    "oauth_calibration_account_id",
    "oauth_calibration_platform",
    "oauth_calibration_status",
    "oauth_calibration_result",
    "oauth_calibration_created_at",
    "oauth_calibration_finished_at",
    "oauth_calibration_fresh",
)


async def load_weibo_real_run_evidence_context(
    *,
    db,
    task_id: str,
) -> dict[str, Any]:
    """Load the account-scoped Weibo OAuth capability attestation."""

    row = await db.fetch_one(
        WEIBO_REAL_RUN_EVIDENCE_QUERY,
        {"task_id": task_id},
    )
    return {
        field: row_value(row, field)
        for field in _WEIBO_REAL_RUN_EVIDENCE_FIELDS
    }


async def _load_weibo_oauth_credential_owned(
    account_id: int,
    *,
    expected_uid: str,
    expected_execution_revision: int,
    runtime,
):
    """Decrypt and bind the exact OAuth credential selected by the gate."""

    row = await runtime.database.fetch_one(
        """SELECT platform, encrypted_credential, execution_revision
             FROM accounts
            WHERE id = :account_id AND deleted_at IS NULL""",
        {"account_id": account_id},
    )
    if not row or str(runtime.row_get(row, "platform") or "").strip().lower() != "weibo":
        raise WeiboOAuthCredentialError("weibo_oauth_account_binding_invalid")
    try:
        revision = int(runtime.row_get(row, "execution_revision"))
    except (TypeError, ValueError) as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_execution_revision_mismatch"
        ) from exc
    if revision != expected_execution_revision:
        raise WeiboOAuthCredentialError("weibo_oauth_execution_revision_mismatch")
    encrypted = runtime.row_get(row, "encrypted_credential")
    if not encrypted:
        raise WeiboOAuthCredentialError("weibo_oauth_credential_required")
    try:
        # OAuth credentials were introduced with an explicit purpose binding;
        # unlike legacy browser cookies, they have no unbound ciphertext form.
        decrypted = runtime.cookie_vault.decrypt_strict(encrypted, aad=runtime.CREDENTIAL_AAD)
    except Exception as exc:
        raise WeiboOAuthCredentialError(
            "weibo_oauth_credential_decryption_failed"
        ) from exc
    return parse_weibo_oauth_credential(decrypted, expected_uid=expected_uid)


async def _preflight_weibo_friend_mentions_owned(
    client,
    plan,
    *,
    pre_resolved: dict[str, str] | None = None,
    on_progress=None,
) -> dict[str, str]:
    """Resolve every constrained mention and enforce friend counts by UID.

    This runs before any durable intent or mutation. Source mentions and follow
    targets are excluded by both normalized handle identity and resolved UID so
    aliases cannot be counted as distinct friends or disguise a brand account.
    """

    constraints = dict(plan.friend_mention_requirements or {})
    cache: dict[str, str] = {
        _weibo_handle_identity_key(handle): uid
        for handle, uid in dict(pre_resolved or {}).items()
    }

    async def resolve(handle: str) -> str:
        key = _weibo_handle_identity_key(handle)
        if key not in cache:
            cache[key] = await client.resolve_user_uid(handle)
            if on_progress is not None:
                await on_progress()
        return cache[key]

    source = dict(plan.source_content_requirements or {})
    bound = dict(plan.content_requirements or {})
    excluded_handles = list(source.get("follow_targets") or []) + list(
        bound.get("follow_targets") or []
    )
    all_handles = list(excluded_handles)
    for action in ("commented", "reposted"):
        source_action = source.get(action, {})
        if isinstance(source_action, dict):
            all_handles.extend(source_action.get("mentions") or [])
        all_handles.extend(plan.payload_for(action).get("mentions") or [])
    unique_handle_keys = {
        _weibo_handle_identity_key(handle) for handle in all_handles
    }
    if len(unique_handle_keys) > WEIBO_MAX_UNIQUE_HANDLES:
        raise RuntimeError("weibo_preflight_unique_handle_limit_exceeded")
    excluded_keys = {
        _weibo_handle_identity_key(handle) for handle in excluded_handles
    }
    excluded_uids = {await resolve(handle) for handle in excluded_handles}

    # Mention validity is an independent precondition, not merely an input to
    # the optional friend-count rule.  Resolve every user identity referenced
    # by an executable text action before any durable intent/POST is created.
    # Otherwise a misspelled/non-existent brand mention could be accepted as
    # plain comment text and later recorded as a successful requirement.
    for action in ("commented", "reposted"):
        source_action = source.get(action, {})
        source_mentions = list(
            source_action.get("mentions") or []
            if isinstance(source_action, dict)
            else []
        )
        source_keys = {
            _weibo_handle_identity_key(handle) for handle in source_mentions
        }
        source_uids = {await resolve(handle) for handle in source_mentions}
        payload_mentions = list(plan.payload_for(action).get("mentions") or [])
        resolved_payload = [
            (handle, await resolve(handle)) for handle in payload_mentions
        ]
        constraint = constraints.get(action)
        if constraint is None:
            continue
        friend_uids = {
            uid
            for handle, uid in resolved_payload
            if _weibo_handle_identity_key(handle)
            not in source_keys | excluded_keys
            and uid not in source_uids | excluded_uids
        }
        expected = int(constraint["count"])
        satisfied = (
            len(friend_uids) == expected
            if constraint["mode"] == "exact"
            else len(friend_uids) >= expected
        )
        if not satisfied:
            raise RuntimeError(
                f"weibo_friend_identity_count_mismatch:{action}"
            )
    return dict(cache)


async def _execute_weibo_oauth_real_owned(task: dict, *, runtime) -> None:
    """Execute one immutable official Weibo OAuth plan with durable fencing."""

    task_id = str(task.get("task_id") or "").strip()
    if "weibo_rip" in task:
        raise RuntimeError("weibo_rip_plaintext_forbidden")
    account_id = int(task.get("account_id"))
    lottery_id = int(task.get("lottery_id"))
    try:
        queued_full_plan = runtime.validate_action_plan_v2(
            task.get("action_plan"), reject_media=True
        )
    except runtime.ActionPlanV2Error as exc:
        raise RuntimeError(exc.code) from exc
    if queued_full_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
        raise RuntimeError("weibo_execution_path_not_supported")

    current_phase = await runtime.get_latest_phase(task_id) or "init"
    if current_phase == "completed":
        return
    if current_phase != "init":
        # Weibo actions are journaled only in external_action_intents; the
        # task_phases ENUM is not an action ledger (and may not contain favorite).
        raise RuntimeError("weibo_task_phase_requires_reconciliation")

    gate = await runtime.enforce_task_real_run_gate(task, require_running=True)
    full_plan = gate.action_plan
    validated_plan = runtime.gate_execution_action_plan(gate)
    if (
        gate.platform != "weibo"
        or full_plan.plan_hash != queued_full_plan.plan_hash
        or gate.execution_evidence_id
        != str(task.get("execution_evidence_id") or "").strip()
        or gate.oauth_capabilities is None
        or not gate.weibo_uid
    ):
        raise runtime.RealRunGateBlocked("weibo_oauth_execution_binding_invalid")
    credential = await _load_weibo_oauth_credential_owned(
        account_id,
        expected_uid=gate.weibo_uid,
        expected_execution_revision=gate.execution_revision,
        runtime=runtime,
    )
    rip = decrypt_weibo_rip(
        task.get("weibo_rip_encrypted"),
        required=weibo_rip_required(validated_plan.required_actions),
    )
    canonical_identifier = status_identifier_from_canonical_uri(
        task.get("canonical_url")
    )

    client = WeiboApiClient(
        credential.access_token,
        capability_attestation=gate.oauth_capabilities,
        calibration_id=gate.execution_evidence_id,
        account_id=account_id,
        execution_revision=gate.execution_revision,
        runtime_capability_requirements=(
            validated_plan.runtime_capability_requirements
        ),
    )
    current_intents: dict[str, runtime.StartedActionIntent] = {}
    expected_mutations = {}

    async def renew_preflight_leases() -> None:
        await runtime.refresh_task_lease(task_id)
        await runtime.renew_account_operation_lease(
            db=runtime.database,
            task_id=task_id,
            account_id=account_id,
            lottery_id=lottery_id,
            worker_id=runtime.WORKER_ID,
            execution_intent_kind=gate.execution_intent_kind,
        )

    async def quarantine_unknown(action: str, cause: BaseException) -> None:
        intent = current_intents.get(action)
        if intent is not None:
            try:
                await runtime.await_safety_settlement(
                    runtime.mark_action_intent_unknown(
                        db=runtime.database,
                        intent=intent,
                        reason=f"weibo_{action}_outcome_unknown",
                    )
                )
            except Exception as intent_exc:
                runtime.structured_log(
                    "error",
                    "external_action_intent_unknown_write_failed",
                    task_id=task_id,
                    action=action,
                    exception=intent_exc,
                )
        await runtime.await_safety_settlement(
            runtime.quarantine_external_action_outcome(
                task_id=task_id,
                account_id=account_id,
                platform="weibo",
                action=action,
                cause=cause,
            )
        )

    async def run_readonly_weibo_preflight():
        status_id = await client.resolve_status_id(canonical_identifier)
        await renew_preflight_leases()
        await client.preflight_status(status_id)
        await renew_preflight_leases()
        follow_target_uid = None
        if "followed" in validated_plan.required_actions:
            follow_target_uid = await client.resolve_user_uid(
                validated_plan.follow_target_handle
            )
            await renew_preflight_leases()
        await _preflight_weibo_friend_mentions_owned(
            client,
            validated_plan,
            pre_resolved=(
                {validated_plan.follow_target_handle: follow_target_uid}
                if follow_target_uid
                else None
            ),
            on_progress=renew_preflight_leases,
        )
        return status_id, follow_target_uid

    try:
        await renew_preflight_leases()
        try:
            status_id, follow_target_uid = await asyncio.wait_for(
                run_readonly_weibo_preflight(),
                timeout=WEIBO_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("weibo_preflight_deadline_exceeded") from exc

        # Lease renewal is liveness only. Re-read every authorization binding
        # after the potentially long read-only preflight and before the first
        # intent can be created.
        post_preflight_gate = await runtime.enforce_task_real_run_gate(
            task, require_running=True
        )
        credential.require_fresh(
            min_remaining_seconds=(
                len(validated_plan.required_actions)
                * WEIBO_ACTION_HTTP_BUDGET_SECONDS
                + WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS
            )
        )
        if (
            post_preflight_gate.action_plan.plan_hash != full_plan.plan_hash
            or runtime.gate_execution_action_plan(post_preflight_gate).plan_hash
            != validated_plan.plan_hash
            or runtime.gate_requested_actions(post_preflight_gate)
            != validated_plan.required_actions
            or post_preflight_gate.execution_evidence_id
            != gate.execution_evidence_id
            or post_preflight_gate.execution_revision != gate.execution_revision
            or post_preflight_gate.weibo_uid != gate.weibo_uid
            or post_preflight_gate.oauth_capabilities != gate.oauth_capabilities
        ):
            raise runtime.RealRunGateBlocked(
                "weibo_oauth_binding_changed_during_preflight"
            )

        async def before_action(action: str) -> None:
            action_index = validated_plan.required_actions.index(action)
            remaining_actions = len(validated_plan.required_actions) - action_index
            credential.require_fresh(
                min_remaining_seconds=(
                    remaining_actions * WEIBO_ACTION_HTTP_BUDGET_SECONDS
                    + WEIBO_ACTION_SETTLEMENT_BUFFER_SECONDS
                )
            )
            current_gate = await runtime.enforce_task_real_run_gate(
                task, require_running=True
            )
            if (
                current_gate.action_plan.plan_hash != full_plan.plan_hash
                or runtime.gate_execution_action_plan(current_gate).plan_hash
                != validated_plan.plan_hash
                or runtime.gate_requested_actions(current_gate)
                != validated_plan.required_actions
                or current_gate.execution_evidence_id != gate.execution_evidence_id
                or current_gate.execution_revision != gate.execution_revision
                or current_gate.weibo_uid != gate.weibo_uid
                or current_gate.oauth_capabilities != gate.oauth_capabilities
            ):
                raise runtime.RealRunGateBlocked(
                    "weibo_oauth_binding_changed_during_execution"
                )
            await runtime.refresh_task_lease(task_id)
            await runtime.renew_account_operation_lease(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=(
                    current_gate.execution_intent_kind
                ),
            )
            # Renewals are not authority. Re-read the entire gate immediately
            # before the transaction that marks the external intent started.
            renewed_gate = await runtime.enforce_task_real_run_gate(
                task, require_running=True
            )
            if (
                renewed_gate.action_plan.plan_hash != full_plan.plan_hash
                or runtime.gate_execution_action_plan(renewed_gate).plan_hash
                != validated_plan.plan_hash
                or runtime.gate_requested_actions(renewed_gate)
                != validated_plan.required_actions
                or renewed_gate.execution_evidence_id != gate.execution_evidence_id
                or renewed_gate.execution_revision != gate.execution_revision
                or renewed_gate.weibo_uid != gate.weibo_uid
                or renewed_gate.oauth_capabilities != gate.oauth_capabilities
            ):
                raise runtime.RealRunGateBlocked(
                    "weibo_oauth_binding_changed_during_execution"
                )
            intent_payload = {
                "platform": "weibo",
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "calibration_id": gate.execution_evidence_id,
                "execution_revision": gate.execution_revision,
                "status_id": status_id,
                "action_payload": validated_plan.payload_for(action),
            }
            mutation_target = (
                follow_target_uid if action == "followed" else status_id
            )
            expected_mutation = build_weibo_mutation_request(
                action,
                mutation_target,
                payload=validated_plan.payload_for(action),
                rip=(
                    rip
                    if action in {"followed", "commented", "reposted"}
                    else ""
                ),
            )
            expected_mutations[action] = expected_mutation
            intent_payload["mutation_spec"] = expected_mutation.audit_spec
            if action == "followed":
                intent_payload["follow_target_handle"] = (
                    validated_plan.follow_target_handle
                )
                intent_payload["follow_target_uid"] = follow_target_uid
            current_intents[action] = await runtime.prepare_and_start_action_intent(
                db=runtime.database,
                task_id=task_id,
                account_id=account_id,
                lottery_id=lottery_id,
                worker_id=runtime.WORKER_ID,
                execution_intent_kind=(
                    renewed_gate.execution_intent_kind
                ),
                action=action,
                payload=intent_payload,
            )

        async def operation_key_for(action: str) -> str:
            intent = current_intents.get(action)
            if intent is None:
                raise RuntimeError("weibo_action_intent_missing")
            return f"{intent.intent_id}:{intent.attempt_no}"

        async def after_receipt(action: str, receipt) -> None:
            intent = current_intents.get(action)
            if intent is None:
                raise RuntimeError("weibo_action_intent_missing")
            expected = expected_mutations.get(action)
            if (
                expected is None
                or receipt.action != action
                or receipt.target_id != expected.target_id
                or receipt.operation_key
                != f"{intent.intent_id}:{intent.attempt_no}"
                or receipt.request_payload_hash != expected.audit_spec_hash
            ):
                raise RuntimeError("weibo_action_receipt_binding_invalid")
            await runtime.settle_action_intent(
                db=runtime.database,
                intent=intent,
                succeeded=True,
                outcome="ok",
                remote_ref=receipt.remote_id,
            )
            current_intents.pop(action, None)
            expected_mutations.pop(action, None)

        try:
            result = await WeiboOAuthExecutor(
                client,
                operation_key_for=operation_key_for,
                before_action=before_action,
                after_receipt=after_receipt,
            ).execute(
                validated_plan,
                status_id=status_id,
                follow_target_uid=follow_target_uid,
                rip=rip,
            )
        except WeiboApiRejected as exc:
            if not exc.confirmed_no_effect:
                await quarantine_unknown(exc.action, exc)
                raise ExternalActionOutcomeUnknown(
                    "weibo", exc.action, exc
                ) from exc
            intent = current_intents.get(exc.action)
            if intent is None:
                raise
            try:
                await runtime.await_safety_settlement(
                    runtime.settle_action_intent(
                        db=runtime.database,
                        intent=intent,
                        succeeded=False,
                        outcome="rejected",
                        error_message=f"weibo_api_rejected:{exc.error_code}",
                    )
                )
                current_intents.pop(exc.action, None)
            except BaseException as settlement_exc:
                await quarantine_unknown(exc.action, settlement_exc)
                raise ExternalActionOutcomeUnknown(
                    "weibo", exc.action, settlement_exc
                ) from settlement_exc
            raise
        except (WeiboApiActionOutcomeUnknown, WeiboExecutionOutcomeUnknown) as exc:
            await quarantine_unknown(exc.action, exc)
            raise ExternalActionOutcomeUnknown("weibo", exc.action, exc) from exc
        except asyncio.CancelledError as exc:
            if current_intents:
                action = next(reversed(current_intents))
                await quarantine_unknown(action, exc)
            raise
        except BaseException as exc:
            if current_intents:
                action = next(reversed(current_intents))
                await quarantine_unknown(action, exc)
                raise ExternalActionOutcomeUnknown("weibo", action, exc) from exc
            raise

        try:
            completion_event_id = await runtime.record_event(
                aggregate="task",
                aggregate_id=task_id,
                event_type="WeiboOAuthRealRunExecuted",
                payload={
                    "account_id": account_id,
                    "lottery_id": lottery_id,
                    "calibration_id": gate.execution_evidence_id,
                    "actions": list(result.receipts),
                    "success": result.success,
                },
                correlation_id=task_id,
            )
            if not completion_event_id:
                raise RuntimeError("weibo_completion_event_persistence_failed")
        except BaseException as exc:
            await quarantine_unknown("task_completion", exc)
            raise ExternalActionOutcomeUnknown(
                "weibo", "task_completion", exc
            ) from exc
        try:
            await runtime.save_phase(task_id, account_id, lottery_id, "completed")
        except BaseException as exc:
            await quarantine_unknown("task_completion", exc)
            raise ExternalActionOutcomeUnknown(
                "weibo", "task_completion", exc
            ) from exc
    finally:
        try:
            await client.aclose()
        except Exception as exc:
            # Closing a socket cannot undo a durably settled remote result and
            # must not manufacture an unknown external outcome.
            runtime.structured_log(
                "warning",
                "weibo_http_client_close_failed",
                task_id=task_id,
                exception=exc,
            )


async def execute_weibo_oauth_real(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Invoke Weibo's OAuth strategy through the Weibo-owned path."""

    return await _execute_weibo_oauth_real_owned(
        task,
        runtime=runtime,
    )


def weibo_real_run_precondition(task) -> str | None:
    if not str(task.get("execution_evidence_id") or "").strip():
        return "weibo_oauth_capability_evidence_required"
    return None


def validate_weibo_real_run_evidence(
    *,
    task,
    row,
    account_id: int,
    lottery_id: int,
    platform: str,
    plan,
    execution_plan=None,
) -> RealRunEvidenceBinding:
    """Own the Weibo OAuth evidence branch of the shared gate."""

    (
        evidence_id,
        execution_revision,
        oauth_capabilities,
        uid,
    ) = validate_weibo_oauth_execution_evidence(
        task,
        row,
        account_id=account_id,
        plan=plan,
        execution_plan=execution_plan,
    )
    return RealRunEvidenceBinding(
        evidence_id=evidence_id,
        execution_revision=execution_revision,
        oauth_capabilities=oauth_capabilities,
        account_identity=uid,
    )


def validate_weibo_oauth_execution_evidence(
    task: Mapping[str, Any],
    row: Any,
    *,
    account_id: int,
    plan: ValidatedActionPlanV2,
    execution_plan: ValidatedActionPlanV2 | None = None,
) -> tuple[str, int, dict[str, Any], str]:
    """Bind one Weibo task to its latest admin-attested OAuth calibration."""

    from app.weibo.capabilities import (
        WeiboOAuthCapabilityError,
        validate_weibo_oauth_capability_attestation,
    )
    from app.weibo.credentials import (
        decrypt_weibo_rip,
        weibo_rip_hmac,
        weibo_rip_required,
    )

    if plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
        raise RealRunGateBlocked("weibo_execution_path_not_supported")
    runtime_plan = execution_plan or plan
    if runtime_plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
        raise RealRunGateBlocked("weibo_execution_path_not_supported")
    if "weibo_rip" in task:
        raise RealRunGateBlocked("weibo_rip_plaintext_forbidden")
    evidence_id = str(task.get("execution_evidence_id") or "").strip()
    if (
        not evidence_id
        or evidence_id
        != str(
            row_value(row, "task_execution_evidence_id") or ""
        ).strip()
        or evidence_id
        != str(row_value(row, "oauth_calibration_id") or "").strip()
    ):
        raise RealRunGateBlocked(
            "weibo_oauth_calibration_binding_invalid"
        )
    try:
        calibration_account_id = int(
            row_value(row, "oauth_calibration_account_id")
        )
        execution_revision = int(
            row_value(row, "account_execution_revision")
        )
        message_revision = int(task.get("execution_revision"))
    except (TypeError, ValueError) as exc:
        raise RealRunGateBlocked(
            "weibo_oauth_execution_revision_mismatch"
        ) from exc
    if (
        calibration_account_id != account_id
        or execution_revision <= 0
        or message_revision != execution_revision
        or str(row_value(row, "oauth_calibration_platform") or "")
        .strip()
        .lower()
        != "weibo"
        or str(row_value(row, "oauth_calibration_status") or "")
        .strip()
        .lower()
        != "succeeded"
        or int(row_value(row, "oauth_calibration_fresh", 0) or 0) != 1
    ):
        raise RealRunGateBlocked(
            "weibo_oauth_capability_evidence_stale"
        )
    if int(row_value(row, "account_credential_present", 0) or 0) != 1:
        raise RealRunGateBlocked("weibo_oauth_credential_required")

    rip_required = weibo_rip_required(runtime_plan.required_actions)
    try:
        rip = decrypt_weibo_rip(
            task.get("weibo_rip_encrypted"),
            required=rip_required,
        )
    except ValueError as exc:
        code = getattr(exc, "code", "weibo_rip_invalid")
        raise RealRunGateBlocked(code) from exc
    expected_target_hash = compute_target_hash(
        str(row_value(row, "lottery_canonical_url") or "").strip()
    )
    expected_config_hash = compute_config_hash(
        {
            "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
            "execution_revision": execution_revision,
            "runtime_capability_requirements": (
                runtime_plan.runtime_capability_requirements
            ),
            "weibo_rip_hash": weibo_rip_hmac(rip) if rip else "",
        }
    )
    if (
        str(task.get("target_hash") or "").strip()
        != expected_target_hash
        or str(row_value(row, "task_target_hash") or "").strip()
        != expected_target_hash
        or str(task.get("config_hash") or "").strip()
        != expected_config_hash
        or str(row_value(row, "task_config_hash") or "").strip()
        != expected_config_hash
    ):
        raise RealRunGateBlocked("weibo_oauth_task_binding_invalid")

    result = json_object(
        row_value(row, "oauth_calibration_result"),
        code="weibo_oauth_capability_evidence_required",
    )
    try:
        result = validate_weibo_oauth_calibration_envelope(result)
    except WeiboOAuthCalibrationEnvelopeError as exc:
        raise RealRunGateBlocked(exc.code) from exc
    uid = result["identity"]["uid"]
    try:
        capabilities = validate_weibo_oauth_capability_attestation(
            result.get("oauth_capabilities"),
            calibration_id=evidence_id,
            account_id=account_id,
            execution_revision=execution_revision,
            runtime_capability_requirements=(
                runtime_plan.runtime_capability_requirements
            ),
        )
    except WeiboOAuthCapabilityError as exc:
        raise RealRunGateBlocked(exc.code) from exc
    return evidence_id, execution_revision, capabilities, uid


WEIBO = PlatformModule(
    platform_id="weibo",
    adapter_factory=lambda selectors=None: WeiboAdapter(selector_config=selectors),
    probe_handler=execute_browser_observation_probe,
    action_order=tuple(WEIBO_ACTION_ORDER),
    real_target_kinds=frozenset({"status"}),
    execution_paths=(
        ExecutionPath(
            path_id=WEIBO_MANUAL_EXECUTION_PATH,
            credential_kind="browser_session",
            supported_modes=frozenset({"shadow_run"}),
            shadow_executor="browser_observation",
            shadow_handler=execute_browser_observation_shadow,
            selector_binding_modes=frozenset({"shadow_run"}),
            unsupported_mode_error="weibo_manual_shadow_only",
        ),
        ExecutionPath(
            path_id=WEIBO_OAUTH_EXECUTION_PATH,
            credential_kind="weibo_oauth",
            supported_modes=frozenset({"dry_run", "real_run"}),
            real_executor="weibo_oauth",
            real_handler=execute_weibo_oauth_real,
            unsupported_mode_error="weibo_oauth_shadow_not_supported",
            dry_run_requires_executable_plan=True,
            confirmed_intent_settlement=True,
            execution_evidence_kind="oauth_account_calibration",
        ),
    ),
    default_execution_path=WEIBO_OAUTH_EXECUTION_PATH,
    invalid_execution_path_error=WEIBO_ACTION_PLAN_CONTRACT.execution_path_error,
    capability_block_reason="weibo_selector_observation_only",
    # Shadow is read-only browser observation even when the immutable reviewed
    # plan remains bound to OAuth for dry/real execution. Runtime channel
    # selection must not rewrite or re-hash that saved plan.
    mode_execution_path_overrides=(("shadow_run", WEIBO_MANUAL_EXECUTION_PATH),),
    real_run_task_precondition=weibo_real_run_precondition,
    real_run_evidence_context_loader=load_weibo_real_run_evidence_context,
    real_run_evidence_validator=validate_weibo_real_run_evidence,
)
