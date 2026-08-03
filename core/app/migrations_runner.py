"""Versioned SQL migration runner (Phase 4 / ops baseline).

Replaces ad-hoc, untracked schema evolution with an ordered, recorded sequence.
Migration files live in ``core/migrations/NNNN_name.sql`` and are applied once
each, in version order, with every applied version recorded in
``schema_migrations``.

Production schema writes must go through this runner. Runtime self-heal schema
hooks are blocked by ``app.db.GuardedDatabase`` when DEPLOYMENT_MODE=production.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import settings
from app.db import allow_schema_writes, database
from app.utils.log import structured_log

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_.+\.sql$")
MIGRATION_LOCK_NAME = "dpms:schema_migrations"
MIGRATION_PROCESS_LOCK_NAME = "dpms:schema_upgrade_process"
MIGRATION_LOCK_TIMEOUT_SECONDS = 30
MIN_INNODB_PAGE_SIZE = 16_384
CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")

PRODUCTION_REQUIRED_TABLES = {
    "account_calibrations",
    "account_operation_leases",
    "adapter_calibrations",
    "schema_migrations",
    "task_runs",
    "task_phases",
    "lotteries",
    "lottery_rule_snapshots",
    "accounts",
    "events",
    "execution_evidence_bindings",
    "external_action_intents",
    "lottery_execution_intents",
    "lottery_execution_intent_heads",
    "task_execution_intent_bindings",
    "outbox_events",
    "task_outbox_events",
    "failed_task_messages",
    "runtime_settings",
    "worker_heartbeats",
    "bilibili_action_ledger",
    "account_active_risk_states",
    "account_profile_cleanup_intents",
    "account_profile_context_leases",
    "login_profile_cleanup_intents",
    "notification_channel_revisions",
    "notification_delivery_attempts",
    "outbox_archive_watermarks",
    "outbox_event_archive",
    "platform_runtime_security_domains",
    "dpms_schema_baselines",
    "notify_logs",
    "tracked_sources",
    "xiaohongshu_target_sources",
    "xiaohongshu_target_candidates",
    "xiaohongshu_target_candidate_source_hits",
}

PRODUCTION_REQUIRED_COLUMNS = {
    "notification_channel_revisions": {
        "channel",
        "revision",
        "updated_at",
    },
    "notification_delivery_attempts": {
        "delivery_key",
        "channel",
        "status",
        "attempts",
        "created_at",
        "updated_at",
    },
    "outbox_archive_watermarks": {
        "stream_key",
        "continuity_epoch",
        "safe_outbox_id",
        "updated_at",
    },
    "outbox_event_archive": {
        "source_table",
        "source_id",
        "stream_key",
        "payload",
        "delivery_epoch",
        "archived_at",
    },
    "platform_runtime_security_domains": {
        "platform",
        "status",
        "database_user",
        "database_name",
        "core_redis_user",
        "worker_redis_user",
        "encryption_key_fingerprint",
        "generation",
        "created_at",
        "updated_at",
    },
    "dpms_schema_baselines": {
        "baseline_key",
        "migration_version",
        "mysql_major",
        "contract_revision",
        "applied_at",
    },
    "notify_logs": {
        "id",
        "channel",
        "success",
        "config_revision",
        "created_at",
    },
    "task_phases": {"phase"},
    "task_runs": {
        "task_id",
        "status",
        "task_mode",
        "worker_id",
        "stream_message_id",
        "lease_expires_at",
        "rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
        "execution_evidence_id",
        "execution_path_id",
        "target_hash",
        "config_hash",
        "preflight_observation",
        "preflight_observation_kind",
        "preflight_observation_hash",
        "account_lease_id",
        "account_lease_generation",
        "reconciliation_required",
    },
    "lotteries": {
        "id",
        "status",
        "execution_lock",
        "locked_at",
        "authoritative_rule_snapshot_id",
        "rule_hash",
        "action_plan_hash",
    },
    "lottery_rule_snapshots": {
        "id",
        "lottery_id",
        "platform",
        "source_kind",
        "source_locator",
        "fetch_method",
        "rule_text",
        "rule_hash",
        "is_complete",
        "attested_by",
        "attested_at",
        "created_at",
    },
    "execution_evidence_bindings": {
        "id",
        "lottery_id",
        "account_id",
        "platform",
        "rule_snapshot_id",
        "execution_path_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "probe_id",
        "shadow_task_id",
        "probe_observation_kind",
        "probe_observation_hash",
        "shadow_observation_kind",
        "shadow_observation_hash",
        "status",
        "verified_at",
        "expires_at",
        "created_at",
    },
    "account_operation_leases": {
        "account_id",
        "lease_id",
        "generation",
        "operation_kind",
        "owner_id",
        "task_id",
        "acquired_at",
        "expires_at",
        "released_at",
    },
    "external_action_intents": {
        "intent_id",
        "task_id",
        "account_id",
        "lottery_id",
        "lease_id",
        "lease_generation",
        "action",
        "payload_hash",
        "status",
        "effect_certainty",
        "attempt_no",
        "started_at",
        "completed_at",
        "outcome",
        "remote_ref",
        "error_message",
        "reconciliation_note",
        "created_at",
        "updated_at",
    },
    "lottery_execution_intents": {
        "contract_version",
        "intent_id",
        "intent_hash",
        "lottery_id",
        "source_task_id",
        "source_account_id",
        "platform",
        "raw_url",
        "canonical_url",
        "full_action_plan",
        "full_action_plan_hash",
        "full_required_actions",
        "full_required_actions_hash",
        "rule_snapshot_id",
        "rule_hash",
        "execution_path_id",
        "target_hash",
        "created_at",
    },
    "lottery_execution_intent_heads": {
        "lottery_id",
        "current_intent_id",
        "generation",
        "created_at",
        "updated_at",
    },
    "task_execution_intent_bindings": {
        "contract_version",
        "task_id",
        "intent_id",
        "lottery_id",
        "account_id",
        "binding_kind",
        "requested_actions",
        "requested_actions_hash",
        "bound_action_plan",
        "bound_action_plan_hash",
        "evidence_action_plan_hash",
        "rule_snapshot_id",
        "rule_hash",
        "execution_evidence_id",
        "execution_evidence_kind",
        "exact_execution_evidence_id",
        "oauth_calibration_id",
        "execution_path_id",
        "target_hash",
        "config_hash",
        "execution_revision",
        "account_lease_id",
        "account_lease_generation",
        "binding_hash",
        "created_at",
    },
    "adapter_calibrations": {
        "probe_id",
        "platform",
        "account_id",
        "lottery_id",
        "execution_path_id",
        "rule_snapshot_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "observation_kind",
        "observation_hash",
        "account_lease_id",
        "account_lease_generation",
    },
    "account_calibrations": {
        "calibration_id",
        "platform",
        "account_id",
        "status",
        "result",
        "created_at",
    },
    "events": {
        "id",
        "aggregate",
        "aggregate_id",
        "event_type",
        "payload",
        "correlation_id",
        "causation_id",
        "actor_type",
        "actor_id",
        "source_service",
        "occurred_at",
    },
    "schema_migrations": {"version", "checksum", "applied_at"},
    "accounts": {"id", "status", "execution_revision"},
    "task_outbox_events": {
        "event_kind",
        "status",
        "dedup_key",
        "payload",
        "archived_at",
        "redis_delivery_epoch",
    },
    "outbox_events": {
        "stream_key",
        "status",
        "dedup_key",
        "archived_at",
        "redis_delivery_epoch",
    },
    "runtime_settings": {"setting_key", "setting_value", "updated_at"},
    "failed_task_messages": {"stream_key", "message_id", "reason", "payload"},
    "bilibili_action_ledger": {"task_id", "account_id", "lottery_id", "action", "outcome", "ok"},
    "account_active_risk_states": {
        "account_id",
        "risk_event_id",
        "event_type",
        "detail",
        "event_created_at",
        "active_until",
        "updated_at",
    },
    "account_profile_cleanup_intents": {
        "id",
        "account_id",
        "platform",
        "status",
        "attempts",
        "claim_token",
        "worker_id",
        "claimed_at",
        "next_attempt_at",
        "completed_at",
        "last_error_code",
        "created_at",
        "updated_at",
    },
    "account_profile_context_leases": {
        "account_id",
        "platform",
        "lease_token",
        "owner_id",
        "acquired_at",
        "renewed_at",
        "lease_expires_at",
        "created_at",
        "updated_at",
    },
    "login_profile_cleanup_intents": {
        "id",
        "session_id",
        "status",
        "attempts",
        "claim_token",
        "worker_id",
        "claimed_at",
        "next_attempt_at",
        "completed_at",
        "last_error_code",
        "created_at",
        "updated_at",
    },
    "tracked_sources": {
        "id",
        "platform",
        "source_type",
        "source_value",
    },
    "xiaohongshu_target_sources": {
        "id",
        "source_type",
        "source_value",
        "active",
        "last_scan_at",
        "status",
        "last_error_code",
        "version",
        "created_at",
        "updated_at",
    },
    "xiaohongshu_target_candidates": {
        "id",
        "platform",
        "raw_url",
        "canonical_url",
        "url_hash",
        "title",
        "evidence",
        "rule",
        "classification",
        "published_at",
        "value_score",
        "expires_at",
        "decision_status",
        "decision_reason",
        "accepted_lottery_id",
        "version",
        "first_seen_at",
        "last_seen_at",
        "decided_at",
        "decision_actor_id",
        "created_at",
        "updated_at",
    },
    "xiaohongshu_target_candidate_source_hits": {
        "id",
        "candidate_id",
        "source_id",
        "tracked_source_id",
        "source_type",
        "source_value",
        "evidence",
        "hit_count",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    },
}

# These ordered signatures are part of the safety contract, not merely
# performance indexes.  MySQL requires the referenced and referencing column
# order to agree; checking names alone would let a drifted schema start while
# silently weakening exact evidence/lease binding.
PRODUCTION_REQUIRED_UNIQUE_INDEXES = {
    ("outbox_events", "uk_outbox_dedup"): (
        "dedup_key",
    ),
    ("account_calibrations", "uk_account_calibration_id"): (
        "calibration_id",
    ),
    ("lottery_rule_snapshots", "uk_rule_snapshot_id_lottery"): (
        "id",
        "lottery_id",
    ),
    ("adapter_calibrations", "uk_adapter_probe_exact_binding_v2"): (
        "probe_id",
        "lottery_id",
        "account_id",
        "platform",
        "rule_snapshot_id",
        "execution_path_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "observation_kind",
        "observation_hash",
    ),
    ("account_operation_leases", "uk_account_operation_generation"): (
        "account_id",
        "generation",
    ),
    ("account_operation_leases", "uk_account_operation_lease_binding"): (
        "lease_id",
        "account_id",
    ),
    ("account_operation_leases", "uk_account_operation_lease_fence"): (
        "lease_id",
        "account_id",
        "generation",
    ),
    ("task_runs", "uk_task_run_entity_binding"): (
        "task_id",
        "lottery_id",
        "account_id",
    ),
    ("task_runs", "uk_task_run_shadow_binding_v2"): (
        "task_id",
        "lottery_id",
        "account_id",
        "rule_snapshot_id",
        "execution_path_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
        "preflight_observation_kind",
        "preflight_observation_hash",
    ),
    ("execution_evidence_bindings", "uk_execution_evidence_task_binding"): (
        "id",
        "lottery_id",
        "account_id",
        "rule_snapshot_id",
        "execution_path_id",
        "target_hash",
        "rule_hash",
        "action_plan_hash",
        "config_hash",
    ),
    ("execution_evidence_bindings", "uk_execution_evidence_probe_shadow"): (
        "probe_id",
        "shadow_task_id",
    ),
    ("external_action_intents", "uk_external_action_task_action"): (
        "task_id",
        "action",
    ),
    (
        "lottery_execution_intents",
        "uk_lottery_execution_intent_identity",
    ): ("intent_id", "lottery_id"),
    (
        "lottery_execution_intents",
        "uk_lottery_execution_intent_source_binding",
    ): ("source_task_id", "lottery_id", "source_account_id"),
    (
        "lottery_execution_intent_heads",
        "uk_lottery_execution_intent_head_identity",
    ): ("current_intent_id", "lottery_id"),
    (
        "task_execution_intent_bindings",
        "uk_task_execution_intent_binding_identity",
    ): ("task_id", "intent_id", "lottery_id", "account_id"),
    (
        "account_active_risk_states",
        "uk_account_active_risk_event",
    ): ("risk_event_id",),
    (
        "account_profile_cleanup_intents",
        "uk_account_profile_cleanup_account",
    ): ("account_id",),
    (
        "login_profile_cleanup_intents",
        "uk_login_profile_cleanup_session",
    ): ("session_id",),
    (
        "account_profile_context_leases",
        "uk_account_profile_context_lease_token",
    ): ("lease_token",),
    (
        "xiaohongshu_target_sources",
        "uk_xhs_target_source_identity",
    ): ("source_type", "source_value"),
    (
        "xiaohongshu_target_candidates",
        "uk_xhs_target_candidate_url_hash",
    ): ("url_hash",),
    (
        "xiaohongshu_target_candidate_source_hits",
        "uk_xhs_target_candidate_source_hit",
    ): ("candidate_id", "source_id"),
    (
        "notification_delivery_attempts",
        "uk_notification_delivery_message_channel",
    ): ("stream_message_id", "channel"),
    (
        "platform_runtime_security_domains",
        "uk_platform_security_database_user",
    ): ("database_user",),
    (
        "platform_runtime_security_domains",
        "uk_platform_security_core_redis_user",
    ): ("core_redis_user",),
    (
        "platform_runtime_security_domains",
        "uk_platform_security_worker_redis_user",
    ): ("worker_redis_user",),
}

# These keys preserve one immutable root per intent and, critically, one
# authorization binding per task.  A same-named pre-created table without its
# PRIMARY KEY could otherwise pass the named-index checks while accepting
# multiple conflicting repair bindings for one task.
PRODUCTION_REQUIRED_PRIMARY_KEYS = {
    "lottery_execution_intents": ("intent_id",),
    "lottery_execution_intent_heads": ("lottery_id",),
    "task_execution_intent_bindings": ("task_id",),
    "account_active_risk_states": ("account_id",),
    "account_profile_cleanup_intents": ("id",),
    "account_profile_context_leases": ("account_id",),
    "login_profile_cleanup_intents": ("id",),
    "notification_channel_revisions": ("channel",),
    "notification_delivery_attempts": ("delivery_key",),
    "outbox_archive_watermarks": ("stream_key",),
    "outbox_event_archive": ("source_table", "source_id"),
    "platform_runtime_security_domains": ("platform",),
    "dpms_schema_baselines": ("baseline_key",),
    "notify_logs": ("id",),
    "xiaohongshu_target_sources": ("id",),
    "xiaohongshu_target_candidates": ("id",),
    "xiaohongshu_target_candidate_source_hits": ("id",),
}

# 0019 replaces this one-root-per-lottery uniqueness fence with an explicit
# current-head row.  Merely omitting it from the required set would let a
# partially applied migration pass verification while silently rejecting every
# future immutable generation.
PRODUCTION_FORBIDDEN_INDEXES = {
    (
        "lottery_execution_intents",
        "uk_lottery_execution_intent_lottery",
    ),
}

# Non-unique indexes which are required for bounded operational work rather
# than referential integrity. The four independent Outbox relays would
# otherwise rescan another platform's entire pending prefix every five seconds.
PRODUCTION_REQUIRED_INDEXES = {
    ("notify_logs", "idx_notify_delivery_revision"): (
        "channel",
        "config_revision",
        "success",
        "created_at",
        "id",
    ),
    ("accounts", "idx_account_strategy_candidate"): (
        "platform",
        "status",
        "deleted_at",
        "daily_task_count",
        "id",
    ),
    ("risk_events", "idx_risk_account_created_id"): (
        "account_id",
        "created_at",
        "id",
    ),
    ("risk_events", "idx_risk_created_account_id"): (
        "created_at",
        "account_id",
        "id",
    ),
    ("lotteries", "idx_lottery_extracted_platform_id"): (
        "extracted_at",
        "platform",
        "id",
    ),
    ("adapter_calibrations", "idx_adapter_probe_status"): (
        "status",
        "created_at",
    ),
    ("account_calibrations", "idx_account_calibration_status"): (
        "status",
        "created_at",
    ),
    (
        "account_calibrations",
        "idx_account_calibration_account_platform_id",
    ): (
        "account_id",
        "platform",
        "id",
    ),
    (
        "account_calibrations",
        "idx_account_calibration_platform_queued",
    ): (
        "platform",
        "status",
        "created_at",
        "id",
    ),
    (
        "account_calibrations",
        "idx_account_calibration_platform_running",
    ): (
        "platform",
        "status",
        "started_at",
        "created_at",
        "id",
    ),
    ("task_runs", "idx_task_run_status"): (
        "status",
        "created_at",
    ),
    ("task_runs", "idx_task_run_account_created_id"): (
        "account_id",
        "created_at",
        "id",
    ),
    ("task_runs", "idx_task_run_created_lottery_id"): (
        "created_at",
        "lottery_id",
        "id",
    ),
    ("task_runs", "idx_task_run_stale_running"): (
        "status",
        "lease_expires_at",
        "task_id",
    ),
    ("task_runs", "idx_task_run_lottery_stale"): (
        "lottery_id",
        "status",
        "lease_expires_at",
        "task_id",
    ),
    ("lotteries", "idx_lottery_platform_recovery"): (
        "platform",
        "status",
        "id",
    ),
    ("outbox_events", "idx_outbox_stream_status_id"): (
        "stream_key",
        "status",
        "id",
    ),
    (
        "account_active_risk_states",
        "idx_account_active_risk_until",
    ): (
        "active_until",
        "account_id",
    ),
    (
        "account_profile_cleanup_intents",
        "idx_account_profile_cleanup_pending",
    ): (
        "platform",
        "status",
        "next_attempt_at",
        "id",
    ),
    (
        "account_profile_cleanup_intents",
        "idx_account_profile_cleanup_running",
    ): (
        "platform",
        "status",
        "claimed_at",
        "id",
    ),
    (
        "login_profile_cleanup_intents",
        "idx_login_profile_cleanup_pending",
    ): ("status", "next_attempt_at", "id"),
    (
        "login_profile_cleanup_intents",
        "idx_login_profile_cleanup_running",
    ): ("status", "claimed_at", "id"),
    (
        "account_profile_context_leases",
        "idx_account_profile_context_lease_expiry",
    ): ("platform", "lease_expires_at", "account_id"),
    (
        "xiaohongshu_target_sources",
        "idx_xhs_target_source_scan_queue",
    ): ("active", "status", "last_scan_at", "id"),
    (
        "xiaohongshu_target_candidates",
        "idx_xhs_target_candidate_review_queue",
    ): ("decision_status", "last_seen_at", "id"),
    (
        "xiaohongshu_target_candidates",
        "idx_xhs_target_candidate_accepted_lottery",
    ): ("accepted_lottery_id", "id"),
    (
        "xiaohongshu_target_candidate_source_hits",
        "idx_xhs_target_source_hit_queue",
    ): ("source_id", "last_seen_at", "candidate_id"),
    (
        "xiaohongshu_target_candidate_source_hits",
        "idx_xhs_target_hit_tracked_source",
    ): ("tracked_source_id", "id"),
    (
        "platform_runtime_security_domains",
        "idx_platform_security_status",
    ): ("status", "updated_at"),
    (
        "notification_delivery_attempts",
        "idx_notification_delivery_status",
    ): ("status", "updated_at"),
    (
        "notification_delivery_attempts",
        "idx_notification_delivery_log",
    ): ("notify_log_id", "channel"),
    (
        "outbox_event_archive",
        "idx_outbox_archive_stream_time",
    ): ("stream_key", "archived_at"),
    (
        "outbox_event_archive",
        "idx_outbox_archive_dedup",
    ): ("dedup_key",),
    (
        "outbox_events",
        "idx_outbox_archive_ready",
    ): ("stream_key", "status", "archived_at", "id"),
    (
        "task_outbox_events",
        "idx_task_outbox_archive_ready",
    ): ("stream_key", "status", "archived_at", "id"),
}

PRODUCTION_REQUIRED_FOREIGN_KEYS = {
    (
        "account_profile_context_leases",
        "fk_account_profile_context_lease_account",
    ): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    (
        "login_profile_cleanup_intents",
        "fk_login_profile_cleanup_session",
    ): (
        ("session_id",),
        "login_sessions",
        ("session_id",),
    ),
    (
        "account_profile_cleanup_intents",
        "fk_account_profile_cleanup_account",
    ): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    (
        "account_active_risk_states",
        "fk_account_active_risk_account",
    ): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    (
        "account_active_risk_states",
        "fk_account_active_risk_event",
    ): (
        ("risk_event_id",),
        "risk_events",
        ("id",),
    ),
    ("lottery_rule_snapshots", "fk_rule_snapshot_lottery"): (
        ("lottery_id",),
        "lotteries",
        ("id",),
    ),
    ("lotteries", "fk_lottery_authoritative_rule_snapshot"): (
        ("authoritative_rule_snapshot_id", "id"),
        "lottery_rule_snapshots",
        ("id", "lottery_id"),
    ),
    ("account_operation_leases", "fk_account_operation_lease_task"): (
        ("task_id",),
        "task_runs",
        ("task_id",),
    ),
    ("account_operation_leases", "fk_account_operation_lease_account"): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    ("task_runs", "fk_task_run_rule_snapshot"): (
        ("rule_snapshot_id", "lottery_id"),
        "lottery_rule_snapshots",
        ("id", "lottery_id"),
    ),
    ("task_runs", "fk_task_run_account_lease"): (
        ("account_lease_id", "account_id", "account_lease_generation"),
        "account_operation_leases",
        ("lease_id", "account_id", "generation"),
    ),
    ("adapter_calibrations", "fk_adapter_probe_rule_snapshot"): (
        ("rule_snapshot_id", "lottery_id"),
        "lottery_rule_snapshots",
        ("id", "lottery_id"),
    ),
    ("adapter_calibrations", "fk_adapter_probe_account_lease"): (
        ("account_lease_id", "account_id", "account_lease_generation"),
        "account_operation_leases",
        ("lease_id", "account_id", "generation"),
    ),
    ("execution_evidence_bindings", "fk_execution_evidence_rule_snapshot"): (
        ("rule_snapshot_id", "lottery_id"),
        "lottery_rule_snapshots",
        ("id", "lottery_id"),
    ),
    ("execution_evidence_bindings", "fk_execution_evidence_lottery"): (
        ("lottery_id",),
        "lotteries",
        ("id",),
    ),
    ("execution_evidence_bindings", "fk_execution_evidence_account"): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    ("execution_evidence_bindings", "fk_execution_evidence_probe_v2"): (
        (
            "probe_id",
            "lottery_id",
            "account_id",
            "platform",
            "rule_snapshot_id",
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
            "probe_observation_kind",
            "probe_observation_hash",
        ),
        "adapter_calibrations",
        (
            "probe_id",
            "lottery_id",
            "account_id",
            "platform",
            "rule_snapshot_id",
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
            "observation_kind",
            "observation_hash",
        ),
    ),
    ("execution_evidence_bindings", "fk_execution_evidence_shadow_v2"): (
        (
            "shadow_task_id",
            "lottery_id",
            "account_id",
            "rule_snapshot_id",
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
            "shadow_observation_kind",
            "shadow_observation_hash",
        ),
        "task_runs",
        (
            "task_id",
            "lottery_id",
            "account_id",
            "rule_snapshot_id",
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
            "preflight_observation_kind",
            "preflight_observation_hash",
        ),
    ),
    ("external_action_intents", "fk_external_action_task_binding"): (
        ("task_id", "lottery_id", "account_id"),
        "task_runs",
        ("task_id", "lottery_id", "account_id"),
    ),
    ("external_action_intents", "fk_external_action_account"): (
        ("account_id",),
        "accounts",
        ("id",),
    ),
    ("external_action_intents", "fk_external_action_lottery"): (
        ("lottery_id",),
        "lotteries",
        ("id",),
    ),
    ("external_action_intents", "fk_external_action_lease_binding"): (
        ("lease_id", "account_id", "lease_generation"),
        "account_operation_leases",
        ("lease_id", "account_id", "generation"),
    ),
    (
        "lottery_execution_intents",
        "fk_lottery_execution_intent_lottery",
    ): (
        ("lottery_id",),
        "lotteries",
        ("id",),
    ),
    (
        "lottery_execution_intents",
        "fk_lottery_execution_intent_source_task",
    ): (
        ("source_task_id", "lottery_id", "source_account_id"),
        "task_runs",
        ("task_id", "lottery_id", "account_id"),
    ),
    (
        "lottery_execution_intents",
        "fk_lottery_execution_intent_rule_snapshot",
    ): (
        ("rule_snapshot_id", "lottery_id"),
        "lottery_rule_snapshots",
        ("id", "lottery_id"),
    ),
    (
        "lottery_execution_intent_heads",
        "fk_lottery_execution_intent_head_root",
    ): (
        ("current_intent_id", "lottery_id"),
        "lottery_execution_intents",
        ("intent_id", "lottery_id"),
    ),
    (
        "task_execution_intent_bindings",
        "fk_task_execution_intent_task",
    ): (
        ("task_id", "lottery_id", "account_id"),
        "task_runs",
        ("task_id", "lottery_id", "account_id"),
    ),
    (
        "task_execution_intent_bindings",
        "fk_task_execution_intent_root",
    ): (
        ("intent_id", "lottery_id"),
        "lottery_execution_intents",
        ("intent_id", "lottery_id"),
    ),
    (
        "task_execution_intent_bindings",
        "fk_task_execution_intent_exact_evidence",
    ): (
        ("exact_execution_evidence_id",),
        "execution_evidence_bindings",
        ("id",),
    ),
    (
        "task_execution_intent_bindings",
        "fk_task_execution_intent_oauth_calibration",
    ): (
        ("oauth_calibration_id",),
        "account_calibrations",
        ("calibration_id",),
    ),
    (
        "task_execution_intent_bindings",
        "fk_task_execution_intent_lease",
    ): (
        ("account_lease_id", "account_id", "account_lease_generation"),
        "account_operation_leases",
        ("lease_id", "account_id", "generation"),
    ),
    (
        "xiaohongshu_target_candidates",
        "fk_xhs_target_candidate_lottery",
    ): (
        ("accepted_lottery_id",),
        "lotteries",
        ("id",),
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "fk_xhs_target_hit_candidate",
    ): (
        ("candidate_id",),
        "xiaohongshu_target_candidates",
        ("id",),
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "fk_xhs_target_hit_source",
    ): (
        ("source_id",),
        "xiaohongshu_target_sources",
        ("id",),
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "fk_xhs_target_hit_tracked_source",
    ): (
        ("tracked_source_id",),
        "tracked_sources",
        ("id",),
    ),
}

PRODUCTION_REQUIRED_CHECK_CONSTRAINTS = {
    ("accounts", "chk_account_execution_revision"),
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_platform",
    ),
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_status",
    ),
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_attempts",
    ),
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_lifecycle",
    ),
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_status",
    ),
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_attempts",
    ),
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_lifecycle",
    ),
    (
        "account_profile_context_leases",
        "chk_account_profile_context_lease_platform",
    ),
    (
        "account_profile_context_leases",
        "chk_account_profile_context_lease_lifecycle",
    ),
    ("lottery_rule_snapshots", "chk_rule_snapshot_complete"),
    ("account_operation_leases", "chk_account_operation_lease_generation"),
    ("task_runs", "chk_task_run_reconciliation_required"),
    ("execution_evidence_bindings", "chk_execution_evidence_status"),
    ("execution_evidence_bindings", "chk_execution_evidence_pair"),
    ("execution_evidence_bindings", "chk_execution_evidence_verified"),
    ("execution_evidence_bindings", "chk_execution_evidence_expiry"),
    ("execution_evidence_bindings", "chk_execution_evidence_observation_hashes_v2"),
    ("external_action_intents", "chk_external_action_status"),
    ("external_action_intents", "chk_external_action_effect_certainty_v2"),
    ("external_action_intents", "chk_external_action_lifecycle_v2"),
    ("external_action_intents", "chk_external_action_lease_generation"),
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_contract",
    ),
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_actions",
    ),
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_hashes",
    ),
    (
        "lottery_execution_intent_heads",
        "chk_lottery_execution_intent_head_generation",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_contract",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_kind",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_evidence_kind",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_actions",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_hashes",
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_revision",
    ),
    ("xiaohongshu_target_sources", "chk_xhs_target_source_type"),
    ("xiaohongshu_target_sources", "chk_xhs_target_source_active"),
    ("xiaohongshu_target_sources", "chk_xhs_target_source_status"),
    ("xiaohongshu_target_sources", "chk_xhs_target_source_version"),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_platform",
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_decision",
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_accept_binding",
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_version",
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_seen_order",
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_source_type",
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_count",
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_seen_order",
    ),
}

# Column presence alone is not sufficient for values that fence real external
# mutations.  A nullable revision or a permissive default would turn a schema
# drift into a silent authorization bypass. Values are compared against
# information_schema after normalising MySQL's string representation.
PRODUCTION_REQUIRED_COLUMN_DEFINITIONS = {
    ("task_phases", "phase"): (
        "enum('init','followed','liked','commented','favorited','reposted','completed')",
        "YES",
        "init",
    ),
    ("accounts", "execution_revision"): ("bigint unsigned", "NO", "1"),
    ("account_operation_leases", "generation"): ("bigint unsigned", "NO", None),
    ("task_runs", "reconciliation_required"): ("tinyint unsigned", "NO", "0"),
    ("external_action_intents", "lease_generation"): ("bigint unsigned", "NO", None),
    ("external_action_intents", "effect_certainty"): (
        "varchar(32)",
        "NO",
        "not_started",
    ),
    ("lottery_execution_intents", "contract_version"): (
        "tinyint unsigned",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "intent_id"): ("char(36)", "NO", None),
    ("lottery_execution_intents", "intent_hash"): ("char(64)", "NO", None),
    ("lottery_execution_intents", "lottery_id"): ("bigint", "NO", None),
    ("lottery_execution_intents", "source_task_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "source_account_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "platform"): ("varchar(32)", "NO", None),
    ("lottery_execution_intents", "raw_url"): ("varchar(512)", "NO", None),
    ("lottery_execution_intents", "canonical_url"): (
        "varchar(512)",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "full_action_plan"): ("json", "NO", None),
    ("lottery_execution_intents", "full_action_plan_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "full_required_actions"): (
        "json",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "full_required_actions_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "rule_snapshot_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "rule_hash"): ("char(64)", "NO", None),
    ("lottery_execution_intents", "execution_path_id"): (
        "varchar(128)",
        "NO",
        None,
    ),
    ("lottery_execution_intents", "target_hash"): ("char(64)", "NO", None),
    ("lottery_execution_intents", "created_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("lottery_execution_intent_heads", "lottery_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("lottery_execution_intent_heads", "current_intent_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("lottery_execution_intent_heads", "generation"): (
        "bigint unsigned",
        "NO",
        None,
    ),
    ("lottery_execution_intent_heads", "created_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("lottery_execution_intent_heads", "updated_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("task_execution_intent_bindings", "task_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "intent_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "lottery_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "account_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "contract_version"): (
        "tinyint unsigned",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "binding_kind"): (
        "varchar(16)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "requested_actions"): (
        "json",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "requested_actions_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "bound_action_plan"): (
        "json",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "bound_action_plan_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "evidence_action_plan_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "rule_snapshot_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "rule_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "execution_evidence_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "execution_evidence_kind"): (
        "varchar(32)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "exact_execution_evidence_id"): (
        "char(36)",
        "YES",
        None,
    ),
    ("task_execution_intent_bindings", "oauth_calibration_id"): (
        "char(36)",
        "YES",
        None,
    ),
    ("task_execution_intent_bindings", "execution_path_id"): (
        "varchar(128)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "target_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "config_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "execution_revision"): (
        "bigint unsigned",
        "NO",
        None,
    ),
    (
        "task_execution_intent_bindings",
        "account_lease_generation",
    ): ("bigint unsigned", "NO", None),
    ("task_execution_intent_bindings", "account_lease_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "binding_hash"): (
        "char(64)",
        "NO",
        None,
    ),
    ("task_execution_intent_bindings", "created_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("outbox_events", "redis_delivery_epoch"): (
        "varchar(128)",
        "YES",
        None,
    ),
    ("schema_migrations", "checksum"): ("char(64)", "NO", None),
    ("account_active_risk_states", "account_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("account_active_risk_states", "risk_event_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("account_active_risk_states", "event_type"): (
        "varchar(64)",
        "NO",
        None,
    ),
    ("account_active_risk_states", "detail"): (
        "json",
        "YES",
        None,
    ),
    ("account_active_risk_states", "event_created_at"): (
        "timestamp",
        "NO",
        None,
    ),
    ("account_active_risk_states", "active_until"): (
        "timestamp",
        "NO",
        None,
    ),
    ("account_active_risk_states", "updated_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("account_profile_cleanup_intents", "id"): (
        "bigint",
        "NO",
        None,
    ),
    ("account_profile_cleanup_intents", "account_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("account_profile_cleanup_intents", "platform"): (
        "varchar(32)",
        "NO",
        None,
    ),
    ("account_profile_cleanup_intents", "status"): (
        "varchar(16)",
        "NO",
        "pending",
    ),
    ("account_profile_cleanup_intents", "attempts"): (
        "int unsigned",
        "NO",
        "0",
    ),
    ("account_profile_cleanup_intents", "claim_token"): (
        "char(36)",
        "YES",
        None,
    ),
    ("account_profile_cleanup_intents", "worker_id"): (
        "varchar(128)",
        "YES",
        None,
    ),
    ("account_profile_cleanup_intents", "claimed_at"): (
        "timestamp",
        "YES",
        None,
    ),
    ("account_profile_cleanup_intents", "next_attempt_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("account_profile_cleanup_intents", "completed_at"): (
        "timestamp",
        "YES",
        None,
    ),
    ("account_profile_cleanup_intents", "last_error_code"): (
        "varchar(128)",
        "YES",
        None,
    ),
    ("account_profile_cleanup_intents", "created_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("account_profile_cleanup_intents", "updated_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("login_profile_cleanup_intents", "id"): (
        "bigint",
        "NO",
        None,
    ),
    ("login_profile_cleanup_intents", "session_id"): (
        "char(36)",
        "NO",
        None,
    ),
    ("login_profile_cleanup_intents", "status"): (
        "varchar(16)",
        "NO",
        "pending",
    ),
    ("login_profile_cleanup_intents", "attempts"): (
        "int unsigned",
        "NO",
        "0",
    ),
    ("login_profile_cleanup_intents", "claim_token"): (
        "char(36)",
        "YES",
        None,
    ),
    ("login_profile_cleanup_intents", "worker_id"): (
        "varchar(128)",
        "YES",
        None,
    ),
    ("login_profile_cleanup_intents", "claimed_at"): (
        "timestamp",
        "YES",
        None,
    ),
    ("login_profile_cleanup_intents", "next_attempt_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("login_profile_cleanup_intents", "completed_at"): (
        "timestamp",
        "YES",
        None,
    ),
    ("login_profile_cleanup_intents", "last_error_code"): (
        "varchar(128)",
        "YES",
        None,
    ),
    ("login_profile_cleanup_intents", "created_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("login_profile_cleanup_intents", "updated_at"): (
        "timestamp",
        "NO",
        "CURRENT_TIMESTAMP",
    ),
    ("account_profile_context_leases", "account_id"): (
        "bigint",
        "NO",
        None,
    ),
    ("account_profile_context_leases", "platform"): (
        "varchar(32)",
        "NO",
        None,
    ),
    ("account_profile_context_leases", "lease_token"): (
        "char(36)",
        "NO",
        None,
    ),
    ("account_profile_context_leases", "owner_id"): (
        "varchar(128)",
        "NO",
        None,
    ),
    ("account_profile_context_leases", "acquired_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("account_profile_context_leases", "renewed_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("account_profile_context_leases", "lease_expires_at"): (
        "timestamp(6)",
        "NO",
        None,
    ),
    ("account_profile_context_leases", "created_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("account_profile_context_leases", "updated_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("xiaohongshu_target_sources", "source_type"): (
        "varchar(32)",
        "NO",
        None,
    ),
    ("xiaohongshu_target_sources", "source_value"): (
        "varchar(256)",
        "NO",
        None,
    ),
    ("xiaohongshu_target_sources", "active"): (
        "tinyint unsigned",
        "NO",
        "1",
    ),
    ("xiaohongshu_target_sources", "status"): (
        "varchar(16)",
        "NO",
        "idle",
    ),
    ("xiaohongshu_target_sources", "version"): (
        "bigint unsigned",
        "NO",
        "1",
    ),
    ("xiaohongshu_target_sources", "created_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("xiaohongshu_target_sources", "updated_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("xiaohongshu_target_candidates", "platform"): (
        "varchar(32)",
        "NO",
        "xiaohongshu",
    ),
    ("xiaohongshu_target_candidates", "raw_url"): (
        "varchar(512)",
        "NO",
        None,
    ),
    ("xiaohongshu_target_candidates", "canonical_url"): (
        "varchar(512)",
        "NO",
        None,
    ),
    ("xiaohongshu_target_candidates", "evidence"): (
        "json",
        "NO",
        None,
    ),
    ("xiaohongshu_target_candidates", "rule"): (
        "json",
        "NO",
        None,
    ),
    ("xiaohongshu_target_candidates", "classification"): (
        "json",
        "NO",
        None,
    ),
    ("xiaohongshu_target_candidates", "decision_status"): (
        "varchar(16)",
        "NO",
        "pending",
    ),
    ("xiaohongshu_target_candidates", "accepted_lottery_id"): (
        "bigint",
        "YES",
        None,
    ),
    ("xiaohongshu_target_candidates", "version"): (
        "bigint unsigned",
        "NO",
        "1",
    ),
    ("xiaohongshu_target_candidates", "first_seen_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    ("xiaohongshu_target_candidates", "last_seen_at"): (
        "timestamp(6)",
        "NO",
        "CURRENT_TIMESTAMP(6)",
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "candidate_id",
    ): ("bigint", "NO", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "source_id",
    ): ("bigint", "NO", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "tracked_source_id",
    ): ("bigint", "YES", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "source_type",
    ): ("varchar(32)", "NO", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "source_value",
    ): ("varchar(256)", "NO", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "evidence",
    ): ("json", "NO", None),
    (
        "xiaohongshu_target_candidate_source_hits",
        "hit_count",
    ): ("bigint unsigned", "NO", "1"),
    (
        "xiaohongshu_target_candidate_source_hits",
        "first_seen_at",
    ): ("timestamp(6)", "NO", "CURRENT_TIMESTAMP(6)"),
    (
        "xiaohongshu_target_candidate_source_hits",
        "last_seen_at",
    ): ("timestamp(6)", "NO", "CURRENT_TIMESTAMP(6)"),
}

# MySQL may add whitespace, backticks, a surrounding pair of parentheses and
# character-set introducers to CHECK_CLAUSE.  ``normalise_check_clause`` removes
# only those presentation differences; the boolean expression itself must
# remain byte-for-byte equivalent after normalisation.
PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES = {
    ("accounts", "chk_account_execution_revision"):
        "execution_revision > 0",
    (
        "xiaohongshu_target_sources",
        "chk_xhs_target_source_type",
    ): (
        "source_type IN "
        "('keyword', 'author_profile', 'offline_search_result')"
    ),
    (
        "xiaohongshu_target_sources",
        "chk_xhs_target_source_active",
    ): "active IN (0, 1)",
    (
        "xiaohongshu_target_sources",
        "chk_xhs_target_source_status",
    ): "status IN ('idle', 'scanning', 'succeeded', 'failed')",
    (
        "xiaohongshu_target_sources",
        "chk_xhs_target_source_version",
    ): "version > 0",
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_platform",
    ): "platform = 'xiaohongshu'",
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_decision",
    ): (
        "decision_status IN "
        "('pending', 'accepted', 'skipped', 'needs_review')"
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_accept_binding",
    ): (
        "((decision_status = 'accepted') "
        "AND (accepted_lottery_id IS NOT NULL)) "
        "OR ((decision_status <> 'accepted') "
        "AND (accepted_lottery_id IS NULL))"
    ),
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_version",
    ): "version > 0",
    (
        "xiaohongshu_target_candidates",
        "chk_xhs_target_candidate_seen_order",
    ): "last_seen_at >= first_seen_at",
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_source_type",
    ): (
        "source_type IN "
        "('keyword', 'author_profile', 'offline_search_result')"
    ),
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_count",
    ): "hit_count > 0",
    (
        "xiaohongshu_target_candidate_source_hits",
        "chk_xhs_target_hit_seen_order",
    ): "last_seen_at >= first_seen_at",
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_platform",
    ): "platform IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')",
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_status",
    ): "status IN ('pending', 'running', 'succeeded')",
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_attempts",
    ): "attempts <= 2147483647",
    (
        "account_profile_cleanup_intents",
        "chk_account_profile_cleanup_lifecycle",
    ): (
        "((status = 'pending') AND (claim_token IS NULL) "
        "AND (worker_id IS NULL) AND (claimed_at IS NULL) "
        "AND (completed_at IS NULL)) "
        "OR ((status = 'running') AND (attempts > 0) "
        "AND (claim_token IS NOT NULL) AND (worker_id IS NOT NULL) "
        "AND (claimed_at IS NOT NULL) AND (completed_at IS NULL)) "
        "OR ((status = 'succeeded') AND (attempts > 0) "
        "AND (claim_token IS NULL) AND (worker_id IS NULL) "
        "AND (claimed_at IS NULL) AND (completed_at IS NOT NULL))"
    ),
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_status",
    ): "status IN ('pending', 'running', 'succeeded')",
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_attempts",
    ): "attempts <= 2147483647",
    (
        "login_profile_cleanup_intents",
        "chk_login_profile_cleanup_lifecycle",
    ): (
        "((status = 'pending') AND (claim_token IS NULL) "
        "AND (worker_id IS NULL) AND (claimed_at IS NULL) "
        "AND (completed_at IS NULL)) "
        "OR ((status = 'running') AND (attempts > 0) "
        "AND (claim_token IS NOT NULL) AND (worker_id IS NOT NULL) "
        "AND (claimed_at IS NOT NULL) AND (completed_at IS NULL)) "
        "OR ((status = 'succeeded') AND (attempts > 0) "
        "AND (claim_token IS NULL) AND (worker_id IS NULL) "
        "AND (claimed_at IS NULL) AND (completed_at IS NOT NULL))"
    ),
    (
        "account_profile_context_leases",
        "chk_account_profile_context_lease_platform",
    ): "platform IN ('bilibili', 'douyin', 'weibo', 'xiaohongshu')",
    (
        "account_profile_context_leases",
        "chk_account_profile_context_lease_lifecycle",
    ): (
        "(renewed_at >= acquired_at) "
        "AND (lease_expires_at > renewed_at)"
    ),
    ("lottery_rule_snapshots", "chk_rule_snapshot_complete"): (
        "(is_complete IN (0, 1)) AND ((is_complete = 0) OR "
        "((attested_by IS NOT NULL) AND (attested_at IS NOT NULL)))"
    ),
    ("account_operation_leases", "chk_account_operation_lease_generation"):
        "generation > 0",
    ("task_runs", "chk_task_run_reconciliation_required"):
        "reconciliation_required IN (0, 1)",
    ("execution_evidence_bindings", "chk_execution_evidence_status"): (
        "status IN ('pending', 'verified', 'revoked', 'expired')"
    ),
    ("execution_evidence_bindings", "chk_execution_evidence_pair"): (
        "((probe_id IS NULL) AND (shadow_task_id IS NULL)) OR "
        "((probe_id IS NOT NULL) AND (shadow_task_id IS NOT NULL))"
    ),
    ("execution_evidence_bindings", "chk_execution_evidence_verified"): (
        "(status <> 'verified') OR ((verified_at IS NOT NULL) "
        "AND (probe_id IS NOT NULL) AND (shadow_task_id IS NOT NULL) "
        "AND (expires_at > verified_at))"
    ),
    ("execution_evidence_bindings", "chk_execution_evidence_expiry"):
        "expires_at > created_at",
    (
        "execution_evidence_bindings",
        "chk_execution_evidence_observation_hashes_v2",
    ): (
        "(status <> 'verified') OR ("
        "(probe_observation_hash IS NOT NULL) "
        "AND (shadow_observation_hash IS NOT NULL) "
        "AND (probe_observation_kind IS NOT NULL) "
        "AND (shadow_observation_kind IS NOT NULL) "
        "AND REGEXP_LIKE(probe_observation_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(shadow_observation_hash, '^[0-9a-f]{64}$', 'c') "
        "AND (CHAR_LENGTH(TRIM(probe_observation_kind)) > 0) "
        "AND (CHAR_LENGTH(TRIM(shadow_observation_kind)) > 0))"
    ),
    ("external_action_intents", "chk_external_action_status"): (
        "status IN ('pending', 'prepared', 'started', 'succeeded', "
        "'failed', 'unknown')"
    ),
    ("external_action_intents", "chk_external_action_effect_certainty_v2"): (
        "((status IN ('pending', 'prepared')) "
        "AND (effect_certainty = 'not_started')) "
        "OR ((status IN ('started', 'unknown')) "
        "AND (effect_certainty = 'unknown')) "
        "OR ((status = 'succeeded') "
        "AND (effect_certainty = 'confirmed_effect')) "
        "OR ((status = 'failed') "
        "AND (effect_certainty = 'confirmed_no_effect'))"
    ),
    ("external_action_intents", "chk_external_action_lifecycle_v2"): (
        "((status = 'pending') AND (attempt_no = 0) AND (started_at IS NULL) "
        "AND (completed_at IS NULL) AND (outcome IS NULL)) "
        "OR ((status = 'prepared') AND (attempt_no > 0) "
        "AND (started_at IS NULL) AND (completed_at IS NULL) "
        "AND (outcome IS NULL)) "
        "OR ((status = 'started') AND (attempt_no > 0) "
        "AND (started_at IS NOT NULL) AND (completed_at IS NULL) "
        "AND (outcome IS NULL)) "
        "OR ((status = 'succeeded') AND (attempt_no > 0) "
        "AND (started_at IS NOT NULL) AND (completed_at IS NOT NULL) "
        "AND (outcome IS NOT NULL) AND (outcome = 'ok')) "
        "OR ((status = 'failed') AND (attempt_no > 0) "
        "AND (started_at IS NOT NULL) AND (completed_at IS NOT NULL) "
        "AND (outcome IS NOT NULL) AND (outcome IN "
        "('retry', 'limit', 'skip', 'captcha', 'risk', 'auth', "
        "'rejected'))) "
        "OR ((status = 'unknown') AND (attempt_no > 0) "
        "AND (started_at IS NOT NULL) AND (completed_at IS NOT NULL) "
        "AND (outcome IS NOT NULL) AND (outcome = 'unknown') "
        "AND (reconciliation_note IS NOT NULL) "
        "AND (CHAR_LENGTH(TRIM(reconciliation_note)) > 0))"
    ),
    ("external_action_intents", "chk_external_action_lease_generation"):
        "lease_generation > 0",
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_contract",
    ): "contract_version = 1",
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_actions",
    ): (
        "JSON_TYPE(full_required_actions) = 'ARRAY' "
        "AND JSON_LENGTH(full_required_actions) > 0"
    ),
    (
        "lottery_execution_intents",
        "chk_lottery_execution_intent_hashes",
    ): (
        "REGEXP_LIKE(intent_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(full_action_plan_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(full_required_actions_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(rule_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(target_hash, '^[0-9a-f]{64}$', 'c')"
    ),
    (
        "lottery_execution_intent_heads",
        "chk_lottery_execution_intent_head_generation",
    ): "generation > 0",
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_contract",
    ): "contract_version = 1",
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_kind",
    ): "binding_kind IN ('full', 'repair')",
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_evidence_kind",
    ): (
        "((execution_evidence_kind = 'exact_execution_evidence') "
        "AND (exact_execution_evidence_id IS NOT NULL) "
        "AND (exact_execution_evidence_id = execution_evidence_id) "
        "AND (oauth_calibration_id IS NULL)) "
        "OR ((execution_evidence_kind = 'oauth_account_calibration') "
        "AND (oauth_calibration_id IS NOT NULL) "
        "AND (oauth_calibration_id = execution_evidence_id) "
        "AND (exact_execution_evidence_id IS NULL))"
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_actions",
    ): (
        "JSON_TYPE(requested_actions) = 'ARRAY' "
        "AND JSON_LENGTH(requested_actions) > 0"
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_hashes",
    ): (
        "REGEXP_LIKE(requested_actions_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(bound_action_plan_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(evidence_action_plan_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(rule_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(target_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(config_hash, '^[0-9a-f]{64}$', 'c') "
        "AND REGEXP_LIKE(binding_hash, '^[0-9a-f]{64}$', 'c')"
    ),
    (
        "task_execution_intent_bindings",
        "chk_task_execution_intent_revision",
    ): "execution_revision > 0 AND account_lease_generation > 0",
}

# Hash of the quote-aware, presentation-normalised ACTION_STATEMENT created by
# 0008_recreate_terminal_outbox_trigger_collation.sql.  Keeping the digest here
# prevents a modified migration file from redefining the production contract.
PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS = {
    "trg_task_runs_terminal_outbox": (
        "AFTER",
        "UPDATE",
        "task_runs",
        "9fef0142af8c58e00e18960dc4cbc7b2d047f1e25847b981f1ff231199177d21",
    ),
    "trg_risk_events_active_state": (
        "AFTER",
        "INSERT",
        "risk_events",
        "97c196e5431da56171fc5f227dc87ec745248f057861757da530e15033d38702",
    ),
}
PRODUCTION_REQUIRED_TRIGGERS = set(PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS)
TRIGGER_METADATA_READER = "dpms_required_trigger_metadata"
TRIGGER_METADATA_CONTRACT_VERSION = "dpms-trigger-metadata-v1"


class FatalMigrationError(BaseException):
    """Bypass broad ``except Exception`` startup handlers in production."""


def _normalise_sql_expression(value: object) -> str:
    """Normalise SQL presentation without changing quoted string values.

    MySQL may prefix literals returned from information_schema with a charset
    introducer (for example ``_utf8mb4'failed'``).  A regex that removes every
    ``_word`` before a quote also corrupts legitimate values such as
    ``'not_started'`` and can make distinct safety contracts compare equal.
    This scanner removes introducers only outside a quoted string, lower-cases
    only SQL syntax/identifiers, and preserves literal bytes exactly.
    """

    source = str(value or "").strip().rstrip(";").strip()
    # MySQL 8 returns CHECK_CLAUSE string delimiters escaped as ``\'`` via
    # information_schema.  The safety contracts contain no quote-bearing
    # literals, so restoring those delimiters before the quote-aware scan is
    # lossless for every expression this verifier accepts.
    source = source.replace("\\'", "'")
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(source):
        char = source[index]
        if in_string:
            result.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                result.append(source[index])
            elif char == "'":
                if index + 1 < len(source) and source[index + 1] == "'":
                    index += 1
                    result.append(source[index])
                else:
                    in_string = False
            index += 1
            continue

        if char == "'":
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "_" and (
            index == 0 or not (source[index - 1].isalnum() or source[index - 1] in "_$")
        ):
            end = index + 1
            while end < len(source) and source[end].isalnum():
                end += 1
            if end > index + 1 and end < len(source) and source[end] == "'":
                index = end
                continue
        if char == "`" or char.isspace():
            index += 1
            continue
        result.append(char.lower())
        index += 1
    return "".join(result)


def _tokenise_check_clause(value: object) -> list[str]:
    """Return quote-aware SQL tokens for the supported CHECK expression subset."""

    source = str(value or "").strip().rstrip(";").strip().replace("\\'", "'")
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace() or char == "`":
            index += 1
            continue
        if char == "_" and (
            index == 0
            or not (source[index - 1].isalnum() or source[index - 1] in "_$")
        ):
            end = index + 1
            while end < len(source) and source[end].isalnum():
                end += 1
            if end > index + 1 and end < len(source) and source[end] == "'":
                index = end
                continue
        if char == "'":
            literal = [char]
            index += 1
            while index < len(source):
                current = source[index]
                literal.append(current)
                if current == "'":
                    if index + 1 < len(source) and source[index + 1] == "'":
                        index += 1
                        literal.append(source[index])
                    else:
                        index += 1
                        break
                elif current == "\\" and index + 1 < len(source):
                    index += 1
                    literal.append(source[index])
                index += 1
            else:
                raise ValueError("Unterminated string in CHECK clause")
            tokens.append("".join(literal))
            continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < len(source) and (
                source[end].isalnum() or source[end] in "_$"
            ):
                end += 1
            tokens.append(source[index:end].lower())
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(source) and (
                source[end].isdigit() or source[end] == "."
            ):
                end += 1
            tokens.append(source[index:end])
            index = end
            continue
        operator = next(
            (
                candidate
                for candidate in ("<=>", "<>", "!=", ">=", "<=", "&&", "||")
                if source.startswith(candidate, index)
            ),
            None,
        )
        if operator is not None:
            tokens.append(operator)
            index += len(operator)
            continue
        tokens.append(char.lower())
        index += 1
    return tokens


class _CheckClauseParser:
    """Canonicalise AND/OR grouping while retaining predicate token bytes."""

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.index = 0

    def parse(self):
        if not self.tokens:
            raise ValueError("Empty CHECK clause")
        node = self._parse_or()
        if self.index != len(self.tokens):
            raise ValueError("Unsupported CHECK clause expression")
        return node

    def _peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def _parse_or(self):
        parts = [self._parse_and()]
        while self._peek() == "or":
            self.index += 1
            parts.append(self._parse_and())
        return self._combine("or", parts)

    def _parse_and(self):
        parts = [self._parse_primary()]
        while self._peek() == "and":
            self.index += 1
            parts.append(self._parse_primary())
        return self._combine("and", parts)

    def _parse_primary(self):
        if self._peek() == "(":
            self.index += 1
            node = self._parse_or()
            if self._peek() != ")":
                raise ValueError("Unbalanced parentheses in CHECK clause")
            self.index += 1
            return node

        atom: list[str] = []
        nested_depth = 0
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            if nested_depth == 0 and token in {"and", "or", ")"}:
                break
            atom.append(token)
            self.index += 1
            if token == "(":
                nested_depth += 1
            elif token == ")":
                if nested_depth <= 0:
                    raise ValueError("Unbalanced parentheses in CHECK predicate")
                nested_depth -= 1
        if not atom or nested_depth != 0:
            raise ValueError("Invalid CHECK predicate")
        return ("atom", tuple(atom))

    @staticmethod
    def _combine(kind: str, parts: list[tuple]):
        flattened: list[tuple] = []
        for part in parts:
            if part[0] == kind:
                flattened.extend(part[1])
            else:
                flattened.append(part)
        if len(flattened) == 1:
            return flattened[0]
        return (kind, tuple(flattened))


def _render_check_node(node: tuple) -> str:
    kind, value = node
    if kind == "atom":
        return "".join(value)
    return f"{kind}(" + ",".join(_render_check_node(item) for item in value) + ")"


def normalise_check_clause(value: object) -> str:
    """Canonicalise MySQL presentation while preserving boolean precedence."""

    return _render_check_node(_CheckClauseParser(_tokenise_check_clause(value)).parse())


def trigger_statement_checksum(value: object) -> str:
    """Digest a trigger body after quote-aware presentation normalisation."""

    normalised = _normalise_sql_expression(value)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def parse_version(filename: str) -> str | None:
    """Return the 4-digit version prefix of a migration filename, or None."""
    match = VERSION_RE.match(filename)
    return match.group(1) if match else None


def discover_migrations(dir_path: Path) -> list[tuple[str, Path]]:
    """All migration files under ``dir_path`` as (version, path), version-ordered."""
    items: list[tuple[str, Path]] = []
    if not Path(dir_path).is_dir():
        return items
    for path in Path(dir_path).glob("*.sql"):
        version = parse_version(path.name)
        if version is not None:
            items.append((version, path))
    items.sort(key=lambda item: item[0])
    duplicates = _duplicate_versions([v for v, _ in items])
    if duplicates:
        raise ValueError(f"Duplicate migration versions: {sorted(duplicates)}")
    return items


def _duplicate_versions(versions: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for version in versions:
        if version in seen:
            dupes.add(version)
        seen.add(version)
    return dupes


def pending_migrations(all_migrations: list[tuple[str, Path]], applied: set[str]) -> list[tuple[str, Path]]:
    """Migrations not yet recorded as applied, preserving version order."""
    return [(version, path) for version, path in all_migrations if version not in applied]


def migration_checksum(path: Path) -> str:
    """SHA-256 of the exact migration bytes recorded at first application."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    Drops ``--`` line comments and blank statements. Migration files are
    author-controlled, so a simple ``;`` split (no ``;`` inside string literals)
    is sufficient and keeps the runner dependency-free.
    """
    statements: list[str] = []
    uncommented = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    for chunk in uncommented.split(";"):
        cleaned = chunk.strip()
        if cleaned:
            statements.append(cleaned)
    return statements


def _production_mode() -> bool:
    return str(settings.deployment_mode or "").strip().lower() == "production"


def _handle_migration_error(exc: Exception) -> None:
    if _production_mode():
        raise FatalMigrationError(f"Refusing to start in production after migration failure: {exc}") from exc
    raise exc


@asynccontextmanager
async def schema_upgrade_process_lock():
    """Serialize the complete bootstrap + versioned migration phase.

    ``run_migrations`` has its own connection-scoped lock, but the historical
    idempotent baseline bootstrap runs before it. The dedicated migration
    command holds this outer lock so two operator invocations cannot race in
    that previously unlocked DDL window.
    """

    async with database.connection() as connection:
        lock_row = await connection.fetch_one(
            "SELECT GET_LOCK(:lock_name, :timeout) AS acquired",
            {
                "lock_name": MIGRATION_PROCESS_LOCK_NAME,
                "timeout": MIGRATION_LOCK_TIMEOUT_SECONDS,
            },
        )
        if lock_row is None or int(lock_row["acquired"] or 0) != 1:
            raise RuntimeError(
                "Timed out acquiring the schema upgrade process lock"
            )
        try:
            yield
        finally:
            try:
                release_row = await connection.fetch_one(
                    "SELECT RELEASE_LOCK(:lock_name) AS released",
                    {"lock_name": MIGRATION_PROCESS_LOCK_NAME},
                )
                if (
                    release_row is None
                    or int(release_row["released"] or 0) != 1
                ):
                    structured_log(
                        "warning",
                        "migration_process_lock_not_released",
                    )
            except Exception as lock_exc:
                structured_log(
                    "warning",
                    "migration_process_lock_release_failed",
                    exception=lock_exc,
                )


async def run_migrations(dir_path: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in order; return the versions applied this run.

    All statements run on one physical MySQL connection.  This is required by
    migrations that use connection-scoped ``@variables``/``PREPARE``, and lets
    the named advisory lock serialize competing application startups.  DDL is
    still not transactionally coupled to the version INSERT, so migrations
    containing DDL must make their statements retry-safe (0010/0011 do so with
    guarded, atomic MySQL 8 ALTER statements).
    """
    try:
        with allow_schema_writes():
            async with database.connection() as connection:
                lock_row = await connection.fetch_one(
                    "SELECT GET_LOCK(:lock_name, :timeout) AS acquired",
                    {
                        "lock_name": MIGRATION_LOCK_NAME,
                        "timeout": MIGRATION_LOCK_TIMEOUT_SECONDS,
                    },
                )
                if lock_row is None or int(lock_row["acquired"] or 0) != 1:
                    raise RuntimeError("Timed out acquiring the schema migration lock")
                try:
                    await connection.execute(
                        """CREATE TABLE IF NOT EXISTS schema_migrations (
                          version VARCHAR(16) PRIMARY KEY,
                          checksum CHAR(64) NOT NULL,
                          applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        ) ENGINE=InnoDB"""
                    )
                    checksum_column = await connection.fetch_one(
                        """SELECT COLUMN_TYPE, IS_NULLABLE
                           FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA = DATABASE()
                             AND TABLE_NAME = 'schema_migrations'
                             AND COLUMN_NAME = 'checksum'
                           LIMIT 1"""
                    )
                    checksum_definition_needs_repair = not checksum_column
                    if not checksum_column:
                        await connection.execute(
                            "ALTER TABLE schema_migrations ADD COLUMN checksum CHAR(64) NULL AFTER version"
                        )
                    else:
                        checksum_definition_needs_repair = (
                            str(checksum_column["COLUMN_TYPE"] or "").strip().lower()
                            != "char(64)"
                            or str(checksum_column["IS_NULLABLE"] or "").strip().upper()
                            != "NO"
                        )
                    all_migrations = discover_migrations(dir_path)
                    migration_by_version = {
                        version: path for version, path in all_migrations
                    }
                    rows = await connection.fetch_all(
                        "SELECT version, checksum FROM schema_migrations"
                    )
                    applied: set[str] = set()
                    for row in rows:
                        version = str(row["version"])
                        path = migration_by_version.get(version)
                        if path is None:
                            raise RuntimeError(
                                f"Applied migration file is missing: {version}"
                            )
                        expected_checksum = migration_checksum(path)
                        raw_checksum = row["checksum"]
                        if raw_checksum is None:
                            # Legacy rows predate checksums.  Backfill the
                            # current bytes once; 0011 repairs every known
                            # historical 0010 shape before schema verification.
                            await connection.execute(
                                """UPDATE schema_migrations SET checksum = :checksum
                                   WHERE version = :version AND checksum IS NULL""",
                                {
                                    "version": version,
                                    "checksum": expected_checksum,
                                },
                            )
                        else:
                            recorded_checksum = str(raw_checksum)
                            if not CHECKSUM_RE.fullmatch(recorded_checksum):
                                raise RuntimeError(
                                    "Applied migration checksum has invalid format: "
                                    f"{version}"
                                )
                            if recorded_checksum != expected_checksum:
                                raise RuntimeError(
                                    f"Applied migration checksum mismatch: {version}"
                                )
                        applied.add(version)
                    if checksum_definition_needs_repair:
                        # The only nullable values accepted above are legacy
                        # NULLs, and those have now been backfilled.  From this
                        # point onward an absent checksum is impossible at the
                        # storage boundary as well as in runner logic.
                        await connection.execute(
                            "ALTER TABLE schema_migrations "
                            "MODIFY COLUMN checksum CHAR(64) NOT NULL"
                        )
                    pending = pending_migrations(
                        all_migrations, applied
                    )

                    applied_now: list[str] = []
                    for version, path in pending:
                        sql = Path(path).read_text(encoding="utf-8")
                        for statement in split_statements(sql):
                            await connection.execute(statement)
                        await connection.execute(
                            """INSERT INTO schema_migrations (version, checksum)
                               VALUES (:version, :checksum)""",
                            {
                                "version": version,
                                "checksum": migration_checksum(path),
                            },
                        )
                        applied_now.append(version)
                        structured_log("info", "migration_applied", version=version)
                finally:
                    try:
                        release_row = await connection.fetch_one(
                            "SELECT RELEASE_LOCK(:lock_name) AS released",
                            {"lock_name": MIGRATION_LOCK_NAME},
                        )
                        if (
                            release_row is None
                            or int(release_row["released"] or 0) != 1
                        ):
                            structured_log(
                                "warning",
                                "migration_lock_not_released",
                            )
                    except Exception as lock_exc:
                        # A lost MySQL connection releases its named locks; do
                        # not hide the original migration error with a cleanup
                        # error from that already-lost connection.
                        structured_log(
                            "warning",
                            "migration_lock_release_failed",
                            exception=lock_exc,
                        )
        # Safety-critical schema is runtime behavior, not a production-only
        # nicety.  Verify it after every startup even when no migration was
        # pending, because a recorded version cannot prove the live schema has
        # not drifted or a non-transactional DDL run was not interrupted.
        await verify_production_schema()
        return applied_now
    except Exception as exc:
        structured_log("error", "migration_run_failed", mode=settings.deployment_mode, exception=exc)
        _handle_migration_error(exc)
        raise


async def verify_migrations_current(
    dir_path: Path = MIGRATIONS_DIR,
) -> None:
    """Verify migration history and live schema without mutating either.

    Production application processes use this read-only gate. Applying DDL
    from an API process makes it possible for a newly started Core to change
    the protocol while an old Core is still serving traffic. The dedicated
    migration command is the only production entry point that calls
    ``run_migrations``.
    """

    migrations = discover_migrations(dir_path)
    if not migrations:
        raise RuntimeError("No migration files are available")

    expected = {
        version: migration_checksum(path)
        for version, path in migrations
    }
    rows = await database.fetch_all(
        "SELECT version, checksum FROM schema_migrations"
    )
    recorded: dict[str, str] = {}
    problems: list[str] = []
    for row in rows:
        version = str(row["version"])
        raw_checksum = row["checksum"]
        if version in recorded:
            problems.append(f"duplicate:{version}")
            continue
        if version not in expected:
            problems.append(f"missing_file:{version}")
            continue
        checksum = "" if raw_checksum is None else str(raw_checksum)
        recorded[version] = checksum
        if not CHECKSUM_RE.fullmatch(checksum):
            problems.append(f"invalid_checksum:{version}")
        elif checksum != expected[version]:
            problems.append(f"checksum_mismatch:{version}")

    for version in expected:
        if version not in recorded:
            problems.append(f"pending:{version}")

    if problems:
        raise RuntimeError(
            "Migration history is not current: "
            + ",".join(sorted(problems))
        )
    await verify_production_schema()


async def verify_production_schema() -> None:
    """Fail production startup if the DB is missing schema covered by migrations."""
    missing: list[str] = []
    page_size_row = await database.fetch_one(
        "SELECT @@innodb_page_size AS innodb_page_size"
    )
    try:
        innodb_page_size = int(page_size_row["innodb_page_size"])
    except (KeyError, TypeError, ValueError):
        innodb_page_size = 0
    if innodb_page_size < MIN_INNODB_PAGE_SIZE:
        missing.append(
            f"server:innodb_page_size>={MIN_INNODB_PAGE_SIZE}"
        )

    table_rows = await database.fetch_all(
        """SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"""
    )
    existing_tables = {row["TABLE_NAME"] for row in table_rows}
    for table in sorted(PRODUCTION_REQUIRED_TABLES - existing_tables):
        missing.append(f"table:{table}")

    for table, columns in PRODUCTION_REQUIRED_COLUMNS.items():
        if table not in existing_tables:
            continue
        column_rows = await database.fetch_all(
            """SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
               FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table""",
            {"table": table},
        )
        existing_columns = {row["COLUMN_NAME"] for row in column_rows}
        for column in sorted(columns - existing_columns):
            missing.append(f"column:{table}.{column}")
        actual_definitions = {
            row["COLUMN_NAME"]: (
                str(row["COLUMN_TYPE"] or "").strip().lower(),
                str(row["IS_NULLABLE"] or "").strip().upper(),
                None
                if row["COLUMN_DEFAULT"] is None
                else str(row["COLUMN_DEFAULT"]).strip(),
            )
            for row in column_rows
        }
        for (required_table, column), expected in (
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS.items()
        ):
            if required_table != table or column not in existing_columns:
                continue
            if actual_definitions.get(column) != expected:
                missing.append(f"column_definition:{table}.{column}")

    index_rows = await database.fetch_all(
        """SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE,
                  SUB_PART, IS_VISIBLE
           FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE()
           ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"""
    )
    actual_indexes: dict[tuple[str, str], list[tuple[int, str]]] = {}
    index_non_unique: dict[tuple[str, str], set[int]] = {}
    index_prefix_lengths: dict[tuple[str, str], list[object]] = {}
    index_visibility: dict[tuple[str, str], set[str]] = {}
    for row in index_rows:
        key = (row["TABLE_NAME"], row["INDEX_NAME"])
        actual_indexes.setdefault(key, []).append(
            (int(row["SEQ_IN_INDEX"]), row["COLUMN_NAME"])
        )
        index_non_unique.setdefault(key, set()).add(int(row["NON_UNIQUE"]))
        index_prefix_lengths.setdefault(key, []).append(row["SUB_PART"])
        try:
            visibility = row["IS_VISIBLE"]
        except KeyError:
            # Unit-test and legacy record fixtures created before visibility
            # became part of the query represent ordinary visible indexes.
            visibility = "YES"
        index_visibility.setdefault(key, set()).add(
            str(visibility or "").strip().upper()
        )
    for key in sorted(PRODUCTION_FORBIDDEN_INDEXES):
        if key[0] in existing_tables and key in actual_indexes:
            missing.append(f"forbidden_index:{key[0]}.{key[1]}")
    for table, expected_columns in PRODUCTION_REQUIRED_PRIMARY_KEYS.items():
        if table not in existing_tables:
            continue
        key = (table, "PRIMARY")
        actual_columns = tuple(
            column for _, column in sorted(actual_indexes.get(key, []))
        )
        if (
            actual_columns != expected_columns
            or index_non_unique.get(key) != {0}
            or index_visibility.get(key) != {"YES"}
            or any(
                prefix_length is not None
                for prefix_length in index_prefix_lengths.get(key, [])
            )
        ):
            missing.append(f"primary_key:{table}")
    for key, expected_columns in PRODUCTION_REQUIRED_UNIQUE_INDEXES.items():
        if key[0] not in existing_tables:
            continue
        actual_columns = tuple(
            column for _, column in sorted(actual_indexes.get(key, []))
        )
        if (
            actual_columns != expected_columns
            or index_non_unique.get(key) != {0}
            or index_visibility.get(key) != {"YES"}
            or any(
                prefix_length is not None
                for prefix_length in index_prefix_lengths.get(key, [])
            )
        ):
            missing.append(f"unique_index:{key[0]}.{key[1]}")
    for key, expected_columns in PRODUCTION_REQUIRED_INDEXES.items():
        if key[0] not in existing_tables:
            continue
        actual_columns = tuple(
            column for _, column in sorted(actual_indexes.get(key, []))
        )
        if (
            actual_columns != expected_columns
            or index_non_unique.get(key) != {1}
            or index_visibility.get(key) != {"YES"}
            or any(
                prefix_length is not None
                for prefix_length in index_prefix_lengths.get(key, [])
            )
        ):
            missing.append(f"index:{key[0]}.{key[1]}")

    foreign_key_rows = await database.fetch_all(
        """SELECT kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.COLUMN_NAME,
                  kcu.ORDINAL_POSITION, kcu.REFERENCED_TABLE_NAME,
                  kcu.REFERENCED_COLUMN_NAME, rc.DELETE_RULE, rc.UPDATE_RULE
           FROM information_schema.KEY_COLUMN_USAGE kcu
           JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
             ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
            AND rc.TABLE_NAME = kcu.TABLE_NAME
            AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
           WHERE kcu.CONSTRAINT_SCHEMA = DATABASE()
             AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
           ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION"""
    )
    actual_foreign_keys: dict[
        tuple[str, str], list[tuple[int, str, str, str]]
    ] = {}
    foreign_key_delete_rules: dict[tuple[str, str], set[str]] = {}
    foreign_key_update_rules: dict[tuple[str, str], set[str]] = {}
    for row in foreign_key_rows:
        key = (row["TABLE_NAME"], row["CONSTRAINT_NAME"])
        actual_foreign_keys.setdefault(key, []).append(
            (
                int(row["ORDINAL_POSITION"]),
                row["COLUMN_NAME"],
                row["REFERENCED_TABLE_NAME"],
                row["REFERENCED_COLUMN_NAME"],
            )
        )
        foreign_key_delete_rules.setdefault(key, set()).add(
            str(row["DELETE_RULE"] or "").strip().upper()
        )
        foreign_key_update_rules.setdefault(key, set()).add(
            str(row["UPDATE_RULE"] or "").strip().upper()
        )
    for key, expected in PRODUCTION_REQUIRED_FOREIGN_KEYS.items():
        if key[0] not in existing_tables:
            continue
        ordered = sorted(actual_foreign_keys.get(key, []))
        actual_local = tuple(item[1] for item in ordered)
        actual_tables = {item[2] for item in ordered}
        actual_remote = tuple(item[3] for item in ordered)
        expected_local, expected_table, expected_remote = expected
        allowed_restrictive_rules = {"RESTRICT", "NO ACTION"}
        if (
            actual_local != expected_local
            or actual_tables != {expected_table}
            or actual_remote != expected_remote
            or not foreign_key_delete_rules.get(key)
            or not foreign_key_delete_rules[key] <= allowed_restrictive_rules
            or not foreign_key_update_rules.get(key)
            or not foreign_key_update_rules[key] <= allowed_restrictive_rules
        ):
            missing.append(f"foreign_key:{key[0]}.{key[1]}")

    constraint_rows = await database.fetch_all(
        """SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.ENFORCED,
                  cc.CHECK_CLAUSE
           FROM information_schema.TABLE_CONSTRAINTS tc
           JOIN information_schema.CHECK_CONSTRAINTS cc
             ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
            AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
           WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
             AND tc.CONSTRAINT_TYPE = 'CHECK'"""
    )
    actual_checks = {
        (row["TABLE_NAME"], row["CONSTRAINT_NAME"]) for row in constraint_rows
    }
    actual_check_clauses = {
        (row["TABLE_NAME"], row["CONSTRAINT_NAME"]): normalise_check_clause(
            row["CHECK_CLAUSE"]
        )
        for row in constraint_rows
    }
    actual_check_enforcement = {
        (row["TABLE_NAME"], row["CONSTRAINT_NAME"]):
            str(row["ENFORCED"] or "").strip().upper()
        for row in constraint_rows
    }
    for key in sorted(PRODUCTION_REQUIRED_CHECK_CONSTRAINTS):
        if key[0] not in existing_tables:
            continue
        if key not in actual_checks:
            missing.append(f"check:{key[0]}.{key[1]}")
            continue
        if actual_check_enforcement.get(key) != "YES":
            missing.append(f"check_enforced:{key[0]}.{key[1]}")
        expected_clause = PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES.get(key)
        if expected_clause is None:
            missing.append(f"check_clause_contract:{key[0]}.{key[1]}")
            continue
        if actual_check_clauses.get(key) != normalise_check_clause(expected_clause):
            missing.append(f"check_clause:{key[0]}.{key[1]}")

    trigger_rows = []
    if PRODUCTION_REQUIRED_TRIGGERS:
        try:
            trigger_rows = await database.fetch_all(
                f"CALL {TRIGGER_METADATA_READER}()"
            )
        except Exception as exc:
            raise RuntimeError(
                "Production schema drift detected: trigger_metadata_reader"
            ) from exc
        if any(
            str(row["CONTRACT_VERSION"] or "")
            != TRIGGER_METADATA_CONTRACT_VERSION
            for row in trigger_rows
        ):
            missing.append("trigger_metadata_reader_contract")
    existing_triggers = {row["TRIGGER_NAME"]: row for row in trigger_rows}
    for trigger in sorted(PRODUCTION_REQUIRED_TRIGGERS - set(existing_triggers)):
        missing.append(f"trigger:{trigger}")
    for trigger, expected in PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS.items():
        row = existing_triggers.get(trigger)
        if row is None:
            continue
        expected_timing, expected_event, expected_table, expected_statement_hash = expected
        actual_definition = (
            str(row["ACTION_TIMING"] or "").strip().upper(),
            str(row["EVENT_MANIPULATION"] or "").strip().upper(),
            str(row["EVENT_OBJECT_TABLE"] or "").strip().lower(),
            trigger_statement_checksum(row["ACTION_STATEMENT"]),
        )
        if actual_definition != (
            expected_timing,
            expected_event,
            expected_table,
            expected_statement_hash,
        ):
            missing.append(f"trigger_definition:{trigger}")

    if missing:
        raise RuntimeError("Production schema drift detected: " + ", ".join(missing))
    structured_log("info", "production_schema_verified", tables=len(PRODUCTION_REQUIRED_TABLES))
