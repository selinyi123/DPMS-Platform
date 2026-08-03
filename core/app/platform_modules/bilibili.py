"""Bilibili lottery platform business capabilities."""

from __future__ import annotations

import re
from types import MappingProxyType

from app.platform_modules.base import (
    ExecutionPathMetadata,
    LotteryTargetValidation,
    PlatformCapabilityError,
    PlatformDiscoverySession,
    PlatformModule,
    PlatformPolicyConflict,
    parse_stored_json,
)
from app.platform_modules.catalog import (
    BILIBILI_ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    BILIBILI_COLLECTION_RUN_BUDGET,
    BILIBILI_DYNAMIC_ID_PATTERN,
    BILIBILI_KEYWORD_QUERY_MAX_CHARS,
    BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET,
    BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT,
    BILIBILI_VIDEO_ID_PATTERN,
    AttemptBudget,
    ExpansionBudget,
    KeywordSearchCallBudget,
    bilibili_keyword_tokens,
    split_bilibili_keywords,
)

BILIBILI_SHORT_LINK_HOSTS = frozenset({"b23.tv"})


def validate_discovery_source_config(source_type: str, source_value: str) -> str:
    if source_type == "up" and not re.fullmatch(r"[1-9][0-9]{0,19}", source_value):
        raise PlatformCapabilityError(
            "bilibili_discovery_up_uid_invalid",
            platform="bilibili",
            capability="source_value",
        )
    if source_type == "keyword":
        tokens = bilibili_keyword_tokens(source_value)
        unique_tokens = {token.casefold() for token in tokens}
        if (
            not tokens
            or any(
                len(token) > BILIBILI_KEYWORD_QUERY_MAX_CHARS
                for token in tokens
            )
            or len(unique_tokens) > BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT
        ):
            raise PlatformCapabilityError(
                "bilibili_discovery_keyword_invalid",
                platform="bilibili",
                capability="source_value",
            )
    return source_value


class BilibiliDiscoverySession(PlatformDiscoverySession):
    def __init__(self, platform_module: PlatformModule) -> None:
        super().__init__(platform_module)
        self.expansion_budget = ExpansionBudget(BILIBILI_COLLECTION_RUN_BUDGET)
        self.keyword_search_budget = KeywordSearchCallBudget(
            BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET
        )

    async def should_defer(self, source) -> bool:
        return bool(
            source["source_type"] == "keyword"
            and self.keyword_search_budget.remaining <= 0
        )

    async def fetch_candidates(self, source) -> list[dict]:
        return await self.platform_module.fetch_discovery_candidates(
            source,
            keyword_search_budget=self.keyword_search_budget,
        )

    async def after_candidates(self, source, candidates: list[dict]) -> int:
        from app.services.discovery import expand_bilibili_collection_sources

        return await expand_bilibili_collection_sources(
            source,
            candidates,
            budget=self.expansion_budget,
        )

    async def finalize(self) -> None:
        from app.utils.log import structured_log

        if self.expansion_budget.remaining == 0:
            structured_log(
                "warning",
                "bilibili_collection_expansion_budget_exhausted",
                budget=BILIBILI_COLLECTION_RUN_BUDGET,
            )
        if self.keyword_search_budget.remaining == 0:
            structured_log(
                "warning",
                "bilibili_keyword_search_call_budget_exhausted",
                budget=BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET,
            )


def validate_parsed_target(parsed, host: str) -> LotteryTargetValidation:
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in BILIBILI_SHORT_LINK_HOSTS and path_parts:
        return LotteryTargetValidation(True, kind="short_link")
    if host == "t.bilibili.com":
        if (
            len(path_parts) == 1
            and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(path_parts[0])
        ):
            return LotteryTargetValidation(True, kind="dynamic")
        if (
            len(path_parts) == 2
            and path_parts[0] == "opus"
            and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="dynamic")
    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        if (
            len(path_parts) >= 2
            and path_parts[0] == "video"
            and BILIBILI_VIDEO_ID_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="video")
        if (
            len(path_parts) == 2
            and path_parts[0] == "opus"
            and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="dynamic")
        if (
            len(path_parts) == 2
            and path_parts[0] == "read"
            and re.fullmatch(r"cv[0-9]+", path_parts[1])
        ):
            return LotteryTargetValidation(True, kind="article")
    return LotteryTargetValidation(
        False,
        reason="bilibili_actionable_url_required",
    )


async def canonicalize_target(raw_url: str) -> str:
    from app.utils.canonicalizer import BilibiliCanonicalizer

    return (await BilibiliCanonicalizer.canonicalize(raw_url)).to_uri()


async def discover(source, *, keyword_search_budget=None) -> list[dict]:
    from app.services.discovery import (
        extract_urls,
        fetch_keyword_dynamics,
        fetch_up_dynamics,
    )

    source_type = str(source.get("source_type") or "").strip().casefold()
    source_value = str(source.get("source_value") or "")
    if source_type == "url_list":
        return [{"raw_url": url} for url in extract_urls(source_value)]
    if source_type == "up":
        return await fetch_up_dynamics(source_value)
    if source_type == "keyword":
        return await fetch_keyword_dynamics(
            source_value,
            search_budget=keyword_search_budget,
        )
    return []


def build_dispatch_plan_binding(*, lottery, task_mode, account, **_context):
    if task_mode not in {"shadow_run", "real_run"}:
        return None
    from app.action_plan import (
        ActionPlanV2Error,
        compute_bilibili_api_config_hash,
        compute_target_hash,
        validate_action_plan_v2,
    )

    try:
        plan = validate_action_plan_v2(
            parse_stored_json(lottery["action_plan"]),
            require_executable=(task_mode == "real_run"),
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
                "message": "Bilibili Action Plan v2 is not dispatchable",
                "blockers": [code],
            }
        ) from exc
    if (
        plan.plan.get("platform") != "bilibili"
        or snapshot_id != plan.rule_snapshot_id
        or str(lottery["rule_hash"] or "") != plan.rule_hash
        or str(lottery["action_plan_hash"] or "") != plan.plan_hash
        or plan.execution_path_id != BILIBILI_API_EXECUTION_PATH
    ):
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Bilibili Action Plan v2 binding changed; review and "
                    "preflight again"
                ),
                "blockers": ["action_plan_rule_binding_mismatch"],
            }
        )
    execution_revision = int(account["execution_revision"] or 0)
    try:
        target_hash = compute_target_hash(str(lottery["canonical_url"] or ""))
        config_hash = compute_bilibili_api_config_hash(execution_revision)
    except ActionPlanV2Error as exc:
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Bilibili target or account revision is not hash-bindable"
                ),
                "blockers": [exc.code],
            }
        ) from exc
    return {
        "rule_snapshot_id": plan.rule_snapshot_id,
        "rule_hash": plan.rule_hash,
        "action_plan_hash": plan.plan_hash,
        "execution_path_id": plan.execution_path_id,
        "target_hash": target_hash,
        "config_hash": config_hash,
        "execution_revision": execution_revision,
        "required_actions": plan.required_actions,
        "follow_target_handle": plan.follow_target_handle,
        "action_plan": plan.plan,
    }


async def revalidate_exact_execution_evidence(
    *,
    lottery,
    lottery_id,
    account,
    plan_binding,
    execution_evidence_id,
    **_context,
) -> None:
    from app.services.bilibili_preflight_evidence import (
        BilibiliPreflightEvidenceError,
        extract_bilibili_dynamic_id,
    )
    from app.services.real_run_readiness import (
        load_exact_bilibili_execution_evidence,
        recent_account_risk,
    )

    try:
        dynamic_id = extract_bilibili_dynamic_id(
            str(lottery["canonical_url"] or ""),
            str(lottery["raw_url"] or ""),
        )
    except BilibiliPreflightEvidenceError as exc:
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Bilibili target changed or is not an exact dynamic"
                ),
                "blockers": [exc.code],
            }
        ) from exc
    exact_evidence = await load_exact_bilibili_execution_evidence(
        lottery_id=lottery_id,
        account_id=int(account["id"]),
        rule_snapshot_id=plan_binding["rule_snapshot_id"],
        execution_path_id=plan_binding["execution_path_id"],
        target_hash=plan_binding["target_hash"],
        rule_hash=plan_binding["rule_hash"],
        action_plan_hash=plan_binding["action_plan_hash"],
        config_hash=plan_binding["config_hash"],
        dynamic_id=dynamic_id,
        required_actions=plan_binding["required_actions"],
        execution_revision=plan_binding["execution_revision"],
        follow_target_handle=plan_binding["follow_target_handle"],
        evidence_id=execution_evidence_id,
        for_update=True,
    )
    if not exact_evidence:
        raise PlatformPolicyConflict(
            "Exact execution evidence expired or changed during dispatch"
        )
    account_risk = await recent_account_risk(
        int(account["id"]),
        for_update=True,
    )
    if account_risk.get("has_recent_risk") is True:
        raise PlatformPolicyConflict(
            {
                "message": (
                    "Bilibili account risk changed during dispatch"
                ),
                "blockers": ["recent_account_risk_event"],
            }
        )


async def validate_real_run_readiness(
    *, lottery, account_id=None, evidence_batch=None, **_context
) -> dict:
    from app.services.real_run_readiness import validate_bilibili_v2_evidence

    if (
        evidence_batch is None
        or not getattr(
            evidence_batch, "account_scoped_readiness", False
        )
    ):
        return await validate_bilibili_v2_evidence(lottery, account_id)
    return await validate_bilibili_v2_evidence(
        lottery,
        account_id,
        evidence_batch=evidence_batch,
    )


BILIBILI_PLATFORM = PlatformModule(
    platform_id="bilibili",
    canonical_hosts=frozenset(
        {
            "b23.tv",
            "t.bilibili.com",
            "bilibili.com",
            "www.bilibili.com",
            "m.bilibili.com",
        }
    ),
    discovery_source_types=frozenset({"url_list", "keyword", "up"}),
    action_order=BILIBILI_ACTION_ORDER,
    execution_paths=(
        ExecutionPathMetadata(
            path_id=BILIBILI_API_EXECUTION_PATH,
            adapter_kind="api",
            task_modes=frozenset({"dry_run", "shadow_run", "real_run"}),
            real_actions=True,
            credential_kind="browser_session",
            execution_evidence_kind="exact_execution_evidence",
        ),
    ),
    default_execution_path_id=BILIBILI_API_EXECUTION_PATH,
    canonicalize_target_handler=canonicalize_target,
    validate_parsed_target_handler=validate_parsed_target,
    target_import_short_link_hosts=BILIBILI_SHORT_LINK_HOSTS,
    target_import_short_link_limit=1,
    # Preserve the established frontend compatibility code for Bilibili's
    # legacy delimited importer.
    target_import_short_link_error=(
        "xiaohongshu_import_short_link_batch_unsupported"
    ),
    # The Bilibili API engine journals transport action names.  Reconciliation
    # owns the compatibility translation here instead of teaching shared
    # infrastructure platform-specific names.
    external_action_aliases=(
        ("follow", "followed"),
        ("like", "liked"),
        ("comment", "commented"),
        ("repost", "reposted"),
    ),
    discovery_handler=discover,
    discovery_session_factory=BilibiliDiscoverySession,
    discovery_source_type_error="source_type must be url_list, keyword, or up",
    discovery_source_config_validator=validate_discovery_source_config,
    execution_mode="api",
    adapter_status="configured",
    media_submission_blocker="bilibili_media_submission_unsupported",
    invalid_execution_path_blocker="bilibili_execution_path_not_supported",
    validate_action_plan_execution_path=True,
    # Bilibili rules commonly say only “关注本账号”.  After an operator has
    # reviewed the hydrated author identity, bind that exact @handle into the
    # plan instead of leaving an otherwise complete rule permanently blocked.
    # Explicit source handles still win and mismatches continue to fail closed.
    manual_follow_target_binding=True,
    shadow_phase_contracts=MappingProxyType(
        {
            "followed": "click_and_state",
            "liked": "click_and_state",
            "commented": "input_submit_state",
            "reposted": "click_and_state",
        }
    ),
    notes="real actions require gray calibration",
    discovery_score_bonus=5,
    strategy_real_target_kinds=frozenset({"dynamic"}),
    strategy_target_kind_error="bilibili_dynamic_target_required",
    probe_requires_plan_binding=True,
    probe_plan_error_message=(
        "Bilibili API-path probe requires an attested exact Action Plan v2"
    ),
    probe_ignored_blockers=frozenset(
        {"execution_account_scope_required", "exact_execution_evidence_required"}
    ),
    requires_exact_real_run_evidence=True,
    exact_execution_evidence_revalidator=revalidate_exact_execution_evidence,
    dispatch_plan_binding_handler=build_dispatch_plan_binding,
    real_run_readiness_provider=validate_real_run_readiness,
)
