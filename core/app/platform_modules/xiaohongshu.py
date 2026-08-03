"""Xiaohongshu lottery platform business capabilities."""

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
    XIAOHONGSHU_ACTION_ORDER,
    XIAOHONGSHU_BROWSER_EVIDENCE_BLOCKER,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    XIAOHONGSHU_NOTE_PATTERN,
)

XIAOHONGSHU_SHORT_LINK_HOSTS = frozenset({"xhslink.com"})


def validate_parsed_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in XIAOHONGSHU_SHORT_LINK_HOSTS and path_parts:
        return LotteryTargetValidation(True, kind="short_link")
    if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
        if (
            len(path_parts) == 2
            and path_parts[0] == "explore"
            and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="note")
        if (
            len(path_parts) == 3
            and path_parts[0] == "discovery"
            and path_parts[1] == "item"
            and XIAOHONGSHU_NOTE_PATTERN.fullmatch(path_parts[2])
        ):
            return LotteryTargetValidation(True, kind="note")
    return LotteryTargetValidation(
        False,
        reason="xiaohongshu_actionable_url_required",
    )


async def canonicalize_target(raw_url: str) -> str:
    from app.utils.canonicalizer import XiaohongshuCanonicalizer

    return (await XiaohongshuCanonicalizer.canonicalize(raw_url)).to_uri()


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
    if stored_execution_path == XIAOHONGSHU_MANUAL_EXECUTION_PATH:
        if task_mode != "shadow_run":
            return None
        return build_manual_shadow_plan_binding(
            lottery,
            platform="xiaohongshu",
            execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            platform_label="Xiaohongshu",
            execution_revision=int(account["execution_revision"] or 0),
            selector_config=selector_config,
        )

    from app.action_plan import (
        ActionPlanV2Error,
        compute_target_hash,
        compute_xiaohongshu_browser_config_hash,
        validate_action_plan_v2,
    )

    try:
        plan = validate_action_plan_v2(
            parse_stored_json(lottery["action_plan"]),
            require_executable=(task_mode == "real_run"),
        )
        snapshot_id = int(lottery["authoritative_rule_snapshot_id"] or 0)
        execution_revision = int(account["execution_revision"] or 0)
        target_hash = compute_target_hash(
            str(lottery["canonical_url"] or "")
        )
        config_hash = compute_xiaohongshu_browser_config_hash(
            execution_revision,
            selector_config,
        )
    except (ActionPlanV2Error, TypeError, ValueError, KeyError) as exc:
        code = (
            exc.code
            if isinstance(exc, ActionPlanV2Error)
            else "action_plan_binding_invalid"
        )
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Xiaohongshu browser Action Plan v2 is not dispatchable"
                ),
                "blockers": [code],
            }
        ) from exc
    if (
        plan.plan.get("platform") != "xiaohongshu"
        or plan.execution_path_id != XIAOHONGSHU_BROWSER_EXECUTION_PATH
        or snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
    ):
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Xiaohongshu browser plan binding changed; review and "
                    "preflight again"
                ),
                "blockers": ["action_plan_rule_binding_mismatch"],
            }
        )
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": XIAOHONGSHU_BROWSER_EXECUTION_PATH,
        "target_hash": target_hash,
        "config_hash": config_hash,
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "action_plan": plan.plan,
    }


async def revalidate_exact_execution_evidence(
    *,
    lottery_id,
    account,
    plan_binding,
    execution_evidence_id,
    **_context,
) -> None:
    from shared.xiaohongshu_browser_contract import (
        compute_xiaohongshu_comment_text_hash,
    )
    from app.services.real_run_readiness import (
        load_exact_xiaohongshu_browser_execution_evidence,
        recent_account_risk,
    )

    comment_text = str(
        plan_binding["action_plan"]
        .get("action_payloads", {})
        .get("commented", {})
        .get("text", "")
    )
    exact_evidence = (
        await load_exact_xiaohongshu_browser_execution_evidence(
            lottery_id=int(lottery_id),
            account_id=int(account["id"]),
            rule_snapshot_id=plan_binding["rule_snapshot_id"],
            execution_path_id=plan_binding["execution_path_id"],
            target_hash=plan_binding["target_hash"],
            rule_hash=plan_binding["rule_hash"],
            action_plan_hash=plan_binding["action_plan_hash"],
            config_hash=plan_binding["config_hash"],
            required_actions=plan_binding["required_actions"],
            execution_revision=plan_binding["execution_revision"],
            follow_target_handle=plan_binding["follow_target_handle"],
            comment_text_hash=compute_xiaohongshu_comment_text_hash(
                comment_text
            ),
            evidence_id=execution_evidence_id,
            for_update=True,
        )
    )
    if not exact_evidence:
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Xiaohongshu exact browser evidence expired or changed "
                    "during dispatch"
                ),
                "blockers": ["exact_execution_evidence_required"],
            }
        )
    account_risk = await recent_account_risk(
        int(account["id"]),
        for_update=True,
    )
    if account_risk.get("has_recent_risk") is True:
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Xiaohongshu account risk changed during dispatch"
                ),
                "blockers": ["recent_account_risk_event"],
            }
        )


async def validate_real_run_readiness(
    *, lottery, account_id=None, evidence_batch=None, **_context
) -> dict:
    from app.services.real_run_readiness import (
        validate_manual_only_contract,
        validate_xiaohongshu_browser_contract,
    )

    raw_plan = parse_stored_json(dict(lottery).get("action_plan"))
    execution_path_id = (
        str(raw_plan.get("execution_path_id") or "")
        if isinstance(raw_plan, dict)
        else ""
    )
    if execution_path_id != XIAOHONGSHU_MANUAL_EXECUTION_PATH:
        return await validate_xiaohongshu_browser_contract(
            lottery,
            account_id=account_id,
            evidence_batch=evidence_batch,
        )

    return await validate_manual_only_contract(
        lottery,
        account_id=account_id,
        platform="xiaohongshu",
        execution_path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        capability_blocker=XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER,
        execution_path_blocker="xiaohongshu_execution_path_not_supported",
        evidence_batch=evidence_batch,
    )


XIAOHONGSHU_PLATFORM = PlatformModule(
    platform_id="xiaohongshu",
    canonical_hosts=frozenset(
        {"xhslink.com", "xiaohongshu.com", "www.xiaohongshu.com"}
    ),
    discovery_source_types=frozenset({"url_list"}),
    action_order=XIAOHONGSHU_ACTION_ORDER,
    execution_paths=(
        ExecutionPathMetadata(
            path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
            adapter_kind="selector",
            task_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            real_actions=True,
            credential_kind="browser_session",
            execution_evidence_kind="exact_execution_evidence",
        ),
        ExecutionPathMetadata(
            path_id=XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            adapter_kind="manual_assisted",
            task_modes=frozenset({"shadow_run"}),
            real_actions=False,
            blocker=XIAOHONGSHU_MANUAL_EXECUTION_BLOCKER,
            credential_kind="browser_session",
        ),
    ),
    default_execution_path_id=XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    canonicalize_target_handler=canonicalize_target,
    validate_parsed_target_handler=validate_parsed_target,
    target_import_short_link_hosts=XIAOHONGSHU_SHORT_LINK_HOSTS,
    target_import_short_link_limit=1,
    target_import_short_link_error=(
        "xiaohongshu_import_short_link_batch_unsupported"
    ),
    external_action_aliases=(
        ("follow", "followed"),
        ("like", "liked"),
        ("comment", "commented"),
        ("favorite", "favorited"),
    ),
    execution_mode="selector",
    adapter_status="exact_browser_evidence_required",
    configuration_kind="execution",
    real_run_supported=True,
    real_run_blocker=XIAOHONGSHU_BROWSER_EVIDENCE_BLOCKER,
    dry_run_supported=True,
    notes=(
        "browser real actions require an exact account/plan/config-bound "
        "Probe + Shadow pair; the legacy manual shadow path remains available"
    ),
    invalid_execution_path_blocker="xiaohongshu_execution_path_not_supported",
    shadow_required_configured_phases=frozenset({"commented"}),
    shadow_phase_contracts=MappingProxyType(
        {
            "followed": "click_or_state",
            "liked": "click_or_state",
            "commented": "input_submit",
            "favorited": "click_or_state",
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
    media_submission_blocker="xiaohongshu_media_submission_unsupported",
    non_executable_path_errors=(
        (
            XIAOHONGSHU_MANUAL_EXECUTION_PATH,
            "xiaohongshu_manual_plan_must_be_non_executable",
        ),
    ),
    validate_action_plan_execution_path=True,
    empty_content_requirement_errors=(
        ("reposted", "xiaohongshu_repost_content_not_supported"),
    ),
    manual_follow_target_binding=True,
    probe_requires_plan_binding=True,
    probe_plan_error_message=(
        "Xiaohongshu browser probe requires an attested exact Action Plan v2"
    ),
    probe_ignored_blockers=frozenset(
        {"execution_account_scope_required", "exact_execution_evidence_required"}
    ),
    requires_exact_real_run_evidence=True,
    exact_execution_evidence_revalidator=revalidate_exact_execution_evidence,
    dispatch_plan_binding_handler=build_dispatch_plan_binding,
    real_run_readiness_provider=validate_real_run_readiness,
)
