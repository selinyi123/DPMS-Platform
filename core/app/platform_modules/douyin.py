"""Douyin lottery platform business capabilities."""

from __future__ import annotations

import re
from types import MappingProxyType

from app.platform_modules.base import (
    ExecutionPathMetadata,
    LotteryTargetValidation,
    PlatformModule,
    PlatformPolicyConflict,
    build_manual_shadow_plan_binding,
    parse_stored_json,
)
from app.platform_modules.catalog import (
    DOUYIN_ACTION_ORDER,
    DOUYIN_DEVICE_EVIDENCE_BLOCKER,
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
    DOUYIN_NOTE_ID_PATTERN,
    DOUYIN_NO_OFFICIAL_API_BLOCKER,
    DOUYIN_VIDEO_ID_PATTERN,
)

DOUYIN_SHORT_LINK_HOSTS = frozenset({"v.douyin.com"})


def validate_parsed_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in DOUYIN_SHORT_LINK_HOSTS and path_parts:
        return LotteryTargetValidation(True, kind="short_link")
    if host in {"douyin.com", "www.douyin.com"}:
        if (
            len(path_parts) == 2
            and path_parts[0] == "video"
            and DOUYIN_VIDEO_ID_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="video")
        if (
            host == "www.douyin.com"
            and len(path_parts) == 2
            and path_parts[0] == "note"
            and DOUYIN_NOTE_ID_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="note")
    if (
        host == "www.iesdouyin.com"
        and len(path_parts) == 3
        and path_parts[0] == "share"
        and path_parts[1] == "video"
        and DOUYIN_VIDEO_ID_PATTERN.fullmatch(path_parts[2])
    ):
        return LotteryTargetValidation(True, kind="video")
    return LotteryTargetValidation(False, reason="douyin_actionable_url_required")


async def canonicalize_target(raw_url: str) -> str:
    from app.utils.canonicalizer import DouyinCanonicalizer

    return (await DouyinCanonicalizer.canonicalize(raw_url)).to_uri()


def build_dispatch_plan_binding(
    *,
    lottery,
    task_mode,
    account,
    selector_config,
    stored_execution_path=None,
    **_context,
):
    if task_mode not in {"dry_run", "shadow_run", "real_run"}:
        return None
    if not stored_execution_path:
        raw_plan = parse_stored_json(dict(lottery).get("action_plan"))
        stored_execution_path = (
            str(raw_plan.get("execution_path_id") or "")
            if isinstance(raw_plan, dict)
            else ""
        )
    if stored_execution_path == DOUYIN_MANUAL_EXECUTION_PATH:
        if task_mode != "shadow_run":
            return None
        return build_manual_shadow_plan_binding(
            lottery,
            platform="douyin",
            execution_path_id=DOUYIN_MANUAL_EXECUTION_PATH,
            platform_label="Douyin",
            execution_revision=int(account["execution_revision"] or 0),
            selector_config=selector_config,
        )

    from app.action_plan import (
        ActionPlanV2Error,
        compute_target_hash,
        validate_action_plan_v2,
    )
    from shared.douyin_device_contract import compute_douyin_device_config_hash

    try:
        plan = validate_action_plan_v2(
            parse_stored_json(lottery["action_plan"]),
            require_executable=(task_mode == "real_run"),
        )
        snapshot_id = int(lottery["authoritative_rule_snapshot_id"] or 0)
        execution_revision = int(account["execution_revision"] or 0)
        target_hash = compute_target_hash(str(lottery["canonical_url"] or ""))
        config_hash = compute_douyin_device_config_hash(
            execution_revision, selector_config
        )
    except (ActionPlanV2Error, TypeError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, ActionPlanV2Error) else getattr(
            exc, "code", "action_plan_binding_invalid"
        )
        raise PlatformPolicyConflict(
            {
                "message": "Douyin device Action Plan v2 is not dispatchable",
                "blockers": [code],
            }
        ) from exc
    if (
        plan.plan.get("platform") != "douyin"
        or plan.execution_path_id != DOUYIN_DEVICE_EXECUTION_PATH
        or snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
    ):
        raise PlatformPolicyConflict(
            {
                "message": "Douyin device plan binding changed; preflight again",
                "blockers": ["action_plan_rule_binding_mismatch"],
            }
        )
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": DOUYIN_DEVICE_EXECUTION_PATH,
        "target_hash": target_hash,
        "config_hash": config_hash,
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "selector_config": dict(selector_config),
        "action_plan": plan.plan,
    }


async def revalidate_exact_execution_evidence(
    *, lottery_id, account, plan_binding, execution_evidence_id, **_context
) -> None:
    from app.services.real_run_readiness import (
        load_exact_douyin_device_execution_evidence,
        recent_account_risk,
    )
    from shared.douyin_device_contract import (
        compute_douyin_exact_text_hash,
        normalize_douyin_device_public_config,
    )

    plan = plan_binding["action_plan"]
    actions = tuple(plan_binding["required_actions"])
    follow = plan_binding["follow_target_handle"] if "followed" in actions else ""
    comment = (
        plan.get("action_payloads", {}).get("commented", {}).get("text", "")
        if "commented" in actions
        else ""
    )
    evidence = await load_exact_douyin_device_execution_evidence(
        lottery_id=int(lottery_id),
        account_id=int(account["id"]),
        rule_snapshot_id=plan_binding["rule_snapshot_id"],
        execution_path_id=plan_binding["execution_path_id"],
        target_hash=plan_binding["target_hash"],
        rule_hash=plan_binding["rule_hash"],
        action_plan_hash=plan_binding["action_plan_hash"],
        config_hash=plan_binding["config_hash"],
        required_actions=actions,
        execution_revision=plan_binding["execution_revision"],
        follow_target_handle_hash=compute_douyin_exact_text_hash(follow),
        comment_text_hash=compute_douyin_exact_text_hash(comment),
        public_config=normalize_douyin_device_public_config(
            plan_binding.get("selector_config") or {}
        ),
        evidence_id=execution_evidence_id,
        for_update=True,
    )
    risk = await recent_account_risk(int(account["id"]), for_update=True)
    if evidence is None or risk.get("has_recent_risk"):
        raise PlatformPolicyConflict(
            {
                "message": "Douyin exact device evidence changed",
                "blockers": [
                    "recent_account_risk_event"
                    if risk.get("has_recent_risk")
                    else "exact_execution_evidence_required"
                ],
            }
        )


async def validate_real_run_readiness(
    *, lottery, account_id=None, evidence_batch=None, **_context
) -> dict:
    from app.services.real_run_readiness import (
        validate_douyin_device_contract,
        validate_manual_only_contract,
    )

    raw_plan = parse_stored_json(dict(lottery).get("action_plan"))
    path_id = (
        str(raw_plan.get("execution_path_id") or "")
        if isinstance(raw_plan, dict)
        else ""
    )
    if path_id != DOUYIN_MANUAL_EXECUTION_PATH:
        return await validate_douyin_device_contract(
            lottery, account_id=account_id, evidence_batch=evidence_batch
        )

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="douyin",
        execution_path_id=DOUYIN_MANUAL_EXECUTION_PATH,
        capability_blocker=DOUYIN_NO_OFFICIAL_API_BLOCKER,
        media_capability_blocker="douyin_media_submission_unsupported",
        evidence_batch=evidence_batch,
    )


def account_candidate_supports_execution(
    *, row, execution_path_id, **_context
) -> bool:
    """Bind manual accounts to cookies and device runs to device envelopes."""

    from app.utils.credential_kind import account_credential_kind

    values = dict(row)
    if str(values.get("latest_calibration_status") or "") != "succeeded":
        return False
    credential_kind = account_credential_kind(
        "douyin", values.get("encrypted_credential")
    )
    if execution_path_id == DOUYIN_MANUAL_EXECUTION_PATH:
        return credential_kind == "browser_session"
    if execution_path_id == DOUYIN_DEVICE_EXECUTION_PATH:
        return credential_kind == "device_agent"
    return False


DOUYIN_PLATFORM = PlatformModule(
    platform_id="douyin",
    canonical_hosts=frozenset(
        {
            "v.douyin.com",
            "douyin.com",
            "www.douyin.com",
            "www.iesdouyin.com",
        }
    ),
    discovery_source_types=frozenset({"url_list"}),
    action_order=DOUYIN_ACTION_ORDER,
    execution_paths=(
        ExecutionPathMetadata(
            path_id=DOUYIN_DEVICE_EXECUTION_PATH,
            adapter_kind="device_agent",
            task_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            real_actions=True,
            credential_kind="device_agent",
            execution_evidence_kind="exact_execution_evidence",
        ),
        ExecutionPathMetadata(
            path_id=DOUYIN_MANUAL_EXECUTION_PATH,
            adapter_kind="manual_assisted",
            task_modes=frozenset({"shadow_run"}),
            real_actions=False,
            blocker=DOUYIN_NO_OFFICIAL_API_BLOCKER,
            credential_kind="browser_session",
        ),
    ),
    default_execution_path_id=DOUYIN_DEVICE_EXECUTION_PATH,
    credential_bound_execution_paths=True,
    canonicalize_target_handler=canonicalize_target,
    validate_parsed_target_handler=validate_parsed_target,
    target_import_short_link_hosts=DOUYIN_SHORT_LINK_HOSTS,
    target_import_short_link_limit=1,
    target_import_short_link_error=(
        "douyin_import_short_link_batch_unsupported"
    ),
    external_action_aliases=(
        ("follow", "followed"),
        ("like", "liked"),
        ("comment", "commented"),
        ("favorite", "favorited"),
    ),
    execution_mode="device_agent",
    adapter_status="exact_device_evidence_required",
    configuration_kind="execution",
    real_run_supported=True,
    real_run_blocker=DOUYIN_DEVICE_EVIDENCE_BLOCKER,
    dry_run_supported=True,
    notes=(
        "local Android device agent; exact target/account/config-bound "
        "Probe + Shadow evidence is required"
    ),
    invalid_execution_path_blocker="douyin_execution_path_invalid",
    shadow_required_configured_phases=frozenset({"commented", "favorited"}),
    shadow_phase_contracts=MappingProxyType(
        {
            "followed": "click_or_state",
            "liked": "click_or_state",
            "commented": "input_submit",
            "favorited": "state_only",
        }
    ),
    real_phase_contracts=MappingProxyType(
        {
            "followed": "click_and_state",
            "liked": "click_and_state",
            "commented": "input_submit_state",
            "favorited": "click_and_state",
        }
    ),
    media_submission_blocker="douyin_media_submission_unsupported",
    non_executable_path_errors=(
        (DOUYIN_MANUAL_EXECUTION_PATH, "douyin_manual_plan_must_be_non_executable"),
    ),
    validate_action_plan_execution_path=True,
    allow_empty_repost_text=False,
    manual_follow_target_binding=True,
    probe_requires_plan_binding=True,
    probe_plan_error_message=(
        "Douyin device probe requires an attested exact Action Plan v2"
    ),
    probe_ignored_blockers=frozenset(
        {"execution_account_scope_required", "exact_execution_evidence_required"}
    ),
    account_candidate_validator=account_candidate_supports_execution,
    requires_exact_real_run_evidence=True,
    exact_execution_evidence_revalidator=revalidate_exact_execution_evidence,
    dispatch_plan_binding_handler=build_dispatch_plan_binding,
    real_run_readiness_provider=validate_real_run_readiness,
    strategy_real_target_kinds=frozenset({"video", "note"}),
    strategy_target_kind_error="douyin_video_or_note_target_required",
)
