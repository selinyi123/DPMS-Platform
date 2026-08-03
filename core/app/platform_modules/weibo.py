"""Weibo lottery platform business capabilities."""

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
    WEIBO_ACTION_CAPABILITY_REQUIREMENTS,
    WEIBO_ACTION_ORDER,
    WEIBO_MANUAL_EXECUTION_BLOCKER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_MBLOGID_PATTERN,
    WEIBO_MID_MAX,
    WEIBO_MID_PATTERN,
    WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
    WEIBO_OAUTH_EXECUTION_PATH,
    WEIBO_UID_PATTERN,
    is_weibo_status_id,
)


WEIBO_PUBLIC_INGRESS_ACTIONS = frozenset(
    {"followed", "commented", "reposted"}
)
WEIBO_SHORT_LINK_HOSTS = frozenset({"t.cn"})


def validate_parsed_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in WEIBO_SHORT_LINK_HOSTS and path_parts:
        return LotteryTargetValidation(True, kind="short_link")
    if (
        host == "m.weibo.cn"
        and len(path_parts) == 2
        and path_parts[0] in {"status", "detail"}
        and is_weibo_status_id(path_parts[1])
    ):
        return LotteryTargetValidation(True, kind="status")
    if host in {"weibo.com", "www.weibo.com"}:
        if (
            len(path_parts) == 2
            and path_parts[0] == "detail"
            and is_weibo_status_id(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="status")
        if (
            len(path_parts) == 2
            and WEIBO_UID_PATTERN.fullmatch(path_parts[0])
            and is_weibo_status_id(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="status")
    return LotteryTargetValidation(False, reason="weibo_actionable_url_required")


async def canonicalize_target(raw_url: str) -> str:
    from app.utils.canonicalizer import WeiboCanonicalizer

    return (await WeiboCanonicalizer.canonicalize(raw_url)).to_uri()


def build_runtime_capability_requirements(
    required_actions: tuple[str, ...],
    path_id: str,
) -> dict:
    if path_id != WEIBO_OAUTH_EXECUTION_PATH:
        return {}
    selected = set(required_actions)
    return {
        "contract_version": WEIBO_OAUTH_CAPABILITY_CONTRACT_VERSION,
        "actions": {
            action: dict(WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action])
            for action in WEIBO_ACTION_ORDER
            if action in selected
        },
    }


def post_validate_action_plan(
    *,
    payloads,
    content_requirements,
    source_content_requirements,
    **_context,
) -> None:
    from app.action_plan import validate_weibo_preflight_unique_handle_limit

    validate_weibo_preflight_unique_handle_limit(
        payloads,
        content_requirements,
        source_content_requirements,
    )


def build_dispatch_plan_binding(
    *,
    lottery,
    task_mode,
    account,
    selector_config,
    stored_execution_path,
    weibo_rip="",
    execution_required_actions=None,
    **_context,
):
    if task_mode not in {"dry_run", "shadow_run", "real_run"}:
        return None
    if stored_execution_path == WEIBO_MANUAL_EXECUTION_PATH:
        return build_manual_shadow_plan_binding(
            lottery,
            platform="weibo",
            execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
            platform_label="Weibo",
            execution_revision=int(account["execution_revision"] or 0),
            selector_config=selector_config,
        )
    return build_weibo_oauth_plan_binding(
        lottery,
        require_executable=(task_mode == "real_run"),
        execution_revision=int(account["execution_revision"] or 0),
        weibo_rip=weibo_rip,
        execution_required_actions=execution_required_actions,
    )


def build_weibo_oauth_plan_binding(
    lottery,
    *,
    require_executable: bool,
    execution_revision: int,
    weibo_rip: str = "",
    execution_required_actions=None,
) -> dict:
    from app.action_plan import (
        ActionPlanV2Error,
        compute_config_hash,
        compute_target_hash,
        validate_action_plan_v2,
    )
    from app.utils.crypto import weibo_rip_hmac

    try:
        plan = validate_action_plan_v2(
            parse_stored_json(lottery["action_plan"]),
            require_executable=require_executable,
        )
        snapshot_id = int(lottery["authoritative_rule_snapshot_id"] or 0)
    except (ActionPlanV2Error, TypeError, ValueError, KeyError) as exc:
        code = (
            exc.code
            if isinstance(exc, ActionPlanV2Error)
            else "action_plan_binding_invalid"
        )
        raise PlatformPolicyConflict(
            {
                "message": "Weibo OAuth Action Plan v2 is not ready",
                "blockers": [code],
            }
        ) from exc
    if (
        plan.plan.get("platform") != "weibo"
        or plan.execution_path_id != WEIBO_OAUTH_EXECUTION_PATH
        or snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
    ):
        raise PlatformPolicyConflict(
            {
                "message": "Weibo OAuth plan binding changed; review again",
                "blockers": ["action_plan_rule_binding_mismatch"],
            }
        )
    if type(execution_revision) is not int or execution_revision <= 0:
        raise PlatformPolicyConflict(
            {
                "message": "Weibo account revision is invalid",
                "blockers": ["execution_revision_invalid"],
            }
        )
    if execution_required_actions is None:
        bound_execution_actions = plan.required_actions
    else:
        supplied_actions = tuple(execution_required_actions)
        bound_execution_actions = tuple(
            action
            for action in plan.required_actions
            if action in supplied_actions
        )
        if (
            not supplied_actions
            or len(set(supplied_actions)) != len(supplied_actions)
            or bound_execution_actions != supplied_actions
        ):
            raise PlatformPolicyConflict(
                {
                    "message": "Weibo repair action subset is invalid",
                    "blockers": ["execution_intent_requested_actions_invalid"],
                }
            )
    capability_contract = build_runtime_capability_requirements(
        bound_execution_actions,
        WEIBO_OAUTH_EXECUTION_PATH,
    )
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
        "target_hash": compute_target_hash(str(lottery["canonical_url"] or "")),
        "config_hash": compute_config_hash(
            {
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "execution_revision": execution_revision,
                "runtime_capability_requirements": capability_contract,
                "weibo_rip_hash": weibo_rip_hmac(weibo_rip),
            }
        ),
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "runtime_capability_requirements": capability_contract,
        "action_plan": plan.plan,
    }


async def revalidate_exact_execution_evidence(
    *,
    lottery,
    account,
    execution_evidence_id,
    execution_required_actions=None,
    **_context,
) -> None:
    from app.services.real_run_readiness import validate_real_run_evidence

    evidence_context = {
        "account_id": int(account["id"]),
        "for_update": True,
    }
    if execution_required_actions is not None:
        evidence_context["execution_required_actions"] = tuple(
            execution_required_actions
        )
    fresh_oauth_evidence = await validate_real_run_evidence(
        lottery,
        **evidence_context,
    )
    if (
        not fresh_oauth_evidence.get("allowed")
        or str(fresh_oauth_evidence.get("execution_evidence_id") or "")
        != execution_evidence_id
    ):
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Weibo OAuth capability evidence expired or changed "
                    "during dispatch"
                ),
                "blockers": fresh_oauth_evidence.get("blockers")
                or ["weibo_oauth_capability_evidence_required"],
            }
        )


def requires_public_ingress(*, required_actions, **_context) -> bool:
    return bool(set(required_actions).intersection(WEIBO_PUBLIC_INGRESS_ACTIONS))


def account_required_actions_for_dispatch(
    *,
    required_actions,
    task_mode,
    **_context,
) -> tuple[str, ...]:
    if task_mode == "shadow_run":
        return ()
    return tuple(required_actions)


def author_action_plan(
    *,
    action_payloads,
    content_requirements,
    friend_mention_requirements,
    source_content_requirements,
    selected_required_actions,
    payload_validation_errors,
    **_context,
):
    from app.action_plan import (
        ActionPlanV2Error,
        bind_manual_friend_mentions,
        friend_mention_requirements_satisfied,
        validate_weibo_preflight_unique_handle_limit,
    )

    content_requirements = bind_manual_friend_mentions(
        action_payloads,
        content_requirements,
        friend_mention_requirements,
    )
    errors = list(payload_validation_errors)
    missing_constraint_actions = [
        action
        for action in friend_mention_requirements
        if action not in selected_required_actions
    ]
    if missing_constraint_actions:
        errors.append("action_plan_friend_mention_action_missing")
    elif friend_mention_requirements and not friend_mention_requirements_satisfied(
        action_payloads,
        content_requirements,
        friend_mention_requirements,
        source_content_requirements=source_content_requirements,
    ):
        errors.append("action_plan_friend_mention_count_mismatch")
    try:
        validate_weibo_preflight_unique_handle_limit(
            action_payloads,
            content_requirements,
            source_content_requirements,
        )
    except ActionPlanV2Error as exc:
        errors.append(exc.code)
    return content_requirements, list(dict.fromkeys(errors))


def account_candidate_supports_execution(
    *,
    row,
    execution_path_id,
    required_actions,
    require_capability,
    **_context,
) -> bool:
    from app.services.real_run_readiness import (
        validate_weibo_oauth_capability_attestation,
    )
    from app.utils.credential_kind import (
        account_credential_kind,
        decrypt_weibo_oauth_credential,
    )

    values = dict(row)
    if values.get("latest_calibration_status") != "succeeded":
        return False
    encrypted_credential = values.get("encrypted_credential")
    credential_kind = account_credential_kind("weibo", encrypted_credential)
    if execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
        return credential_kind == "browser_session"
    if execution_path_id != WEIBO_OAUTH_EXECUTION_PATH:
        return False
    if credential_kind != "weibo_oauth":
        return False
    try:
        credential = decrypt_weibo_oauth_credential(encrypted_credential)
        if not require_capability:
            return True
        if not required_actions:
            return False
        execution_revision = int(values.get("execution_revision") or 0)
        calibration_id = str(values.get("latest_calibration_id") or "")
        if execution_revision <= 0 or not calibration_id:
            return False
        capability = validate_weibo_oauth_capability_attestation(
            values.get("latest_calibration_result"),
            required_actions=required_actions,
            account_id=int(values["id"]),
            execution_revision=execution_revision,
            calibration_fresh=bool(values.get("latest_calibration_fresh")),
            expected_calibration_id=calibration_id,
            expected_uid=str(credential["uid"]),
        )
    except Exception:
        return False
    return capability.get("ready") is True


async def validate_real_run_readiness(
    *,
    lottery,
    account_id=None,
    execution_required_actions=None,
    evidence_batch=None,
    for_update=False,
    **_context,
) -> dict:
    from app.services.real_run_readiness import (
        validate_manual_only_contract,
        validate_weibo_oauth_contract,
    )

    raw_plan = parse_stored_json(dict(lottery).get("action_plan"))
    execution_path_id = (
        str(raw_plan.get("execution_path_id") or "")
        if isinstance(raw_plan, dict)
        else ""
    )
    if execution_path_id == WEIBO_MANUAL_EXECUTION_PATH:
        return await validate_manual_only_contract(
            lottery,
            account_id=account_id,
            platform="weibo",
            execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
            capability_blocker=WEIBO_MANUAL_EXECUTION_BLOCKER,
            execution_path_blocker="weibo_execution_path_invalid",
            media_capability_blocker="weibo_media_submission_unsupported",
            evidence_batch=evidence_batch,
        )
    contract_context = {
        "account_id": account_id,
        "evidence_batch": evidence_batch,
        "for_update": for_update,
    }
    if execution_required_actions is not None:
        contract_context["execution_required_actions"] = tuple(
            execution_required_actions
        )
    return await validate_weibo_oauth_contract(
        lottery,
        **contract_context,
    )


WEIBO_PLATFORM = PlatformModule(
    platform_id="weibo",
    canonical_hosts=frozenset(
        {"t.cn", "m.weibo.cn", "weibo.com", "www.weibo.com"}
    ),
    discovery_source_types=frozenset({"url_list"}),
    action_order=WEIBO_ACTION_ORDER,
    execution_paths=(
        ExecutionPathMetadata(
            path_id=WEIBO_OAUTH_EXECUTION_PATH,
            adapter_kind="oauth",
            task_modes=frozenset({"dry_run", "real_run"}),
            real_actions=True,
            credential_kind="weibo_oauth",
            execution_evidence_kind="oauth_account_calibration",
        ),
        ExecutionPathMetadata(
            path_id=WEIBO_MANUAL_EXECUTION_PATH,
            adapter_kind="manual_assisted",
            task_modes=frozenset({"shadow_run"}),
            real_actions=False,
            blocker=WEIBO_MANUAL_EXECUTION_BLOCKER,
            credential_kind="browser_session",
        ),
    ),
    default_execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
    canonicalize_target_handler=canonicalize_target,
    validate_parsed_target_handler=validate_parsed_target,
    target_import_short_link_hosts=WEIBO_SHORT_LINK_HOSTS,
    target_import_short_link_limit=1,
    target_import_short_link_error=(
        "weibo_import_short_link_batch_unsupported"
    ),
    execution_mode="oauth",
    adapter_status="oauth_capability_required",
    configuration_kind="observation",
    real_run_supported=True,
    real_run_blocker="weibo_oauth_capability_evidence_required",
    notes=(
        "official OAuth writes require fresh per-account and per-action "
        "capability evidence; selectors are observation-only"
    ),
    invalid_execution_path_blocker="weibo_execution_path_invalid",
    credential_bound_execution_paths=True,
    shadow_account_execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
    shadow_required_configured_phases=frozenset(
        {"commented", "favorited"}
    ),
    shadow_phase_contracts=MappingProxyType(
        {
            "followed": "click_or_state",
            "liked": "click_or_state",
            "commented": "input_submit",
            "favorited": "click_or_state",
            "reposted": "click_or_state",
        }
    ),
    media_submission_blocker="weibo_media_submission_unsupported",
    non_executable_path_errors=(
        (WEIBO_MANUAL_EXECUTION_PATH, "weibo_manual_plan_must_be_non_executable"),
    ),
    validate_action_plan_execution_path=True,
    allow_empty_repost_text=True,
    max_text_utf16_units=140,
    text_too_long_error_template="weibo_{action}_text_too_long",
    action_plan_post_validator=post_validate_action_plan,
    manual_follow_target_binding=True,
    action_plan_authoring_handler=author_action_plan,
    runtime_capability_builder=build_runtime_capability_requirements,
    public_ingress_requirement_handler=requires_public_ingress,
    account_required_actions_handler=account_required_actions_for_dispatch,
    account_candidate_validator=account_candidate_supports_execution,
    requires_exact_real_run_evidence=True,
    exact_execution_evidence_revalidator=revalidate_exact_execution_evidence,
    dispatch_plan_binding_handler=build_dispatch_plan_binding,
    real_run_readiness_provider=validate_real_run_readiness,
)
