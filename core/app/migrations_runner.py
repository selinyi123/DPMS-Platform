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
from pathlib import Path

from app.config import settings
from app.db import allow_schema_writes, database
from app.utils.log import structured_log

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_.+\.sql$")
MIGRATION_LOCK_NAME = "dpms:schema_migrations"
MIGRATION_LOCK_TIMEOUT_SECONDS = 30
MIN_INNODB_PAGE_SIZE = 16_384
CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")

PRODUCTION_REQUIRED_TABLES = {
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
    "outbox_events",
    "task_outbox_events",
    "failed_task_messages",
    "worker_heartbeats",
    "bilibili_action_ledger",
}

PRODUCTION_REQUIRED_COLUMNS = {
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
    "schema_migrations": {"version", "checksum", "applied_at"},
    "accounts": {"id", "status", "execution_revision"},
    "task_outbox_events": {"event_kind", "status", "dedup_key", "payload"},
    "failed_task_messages": {"stream_key", "message_id", "reason", "payload"},
    "bilibili_action_ledger": {"task_id", "account_id", "lottery_id", "action", "outcome", "ok"},
}

# These ordered signatures are part of the safety contract, not merely
# performance indexes.  MySQL requires the referenced and referencing column
# order to agree; checking names alone would let a drifted schema start while
# silently weakening exact evidence/lease binding.
PRODUCTION_REQUIRED_UNIQUE_INDEXES = {
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
}

PRODUCTION_REQUIRED_FOREIGN_KEYS = {
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
    ("task_runs", "fk_task_run_execution_evidence"): (
        (
            "execution_evidence_id",
            "lottery_id",
            "account_id",
            "rule_snapshot_id",
            "execution_path_id",
            "target_hash",
            "rule_hash",
            "action_plan_hash",
            "config_hash",
        ),
        "execution_evidence_bindings",
        (
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
}

PRODUCTION_REQUIRED_CHECK_CONSTRAINTS = {
    ("accounts", "chk_account_execution_revision"),
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
    ("schema_migrations", "checksum"): ("char(64)", "NO", None),
}

# MySQL may add whitespace, backticks, a surrounding pair of parentheses and
# character-set introducers to CHECK_CLAUSE.  ``normalise_check_clause`` removes
# only those presentation differences; the boolean expression itself must
# remain byte-for-byte equivalent after normalisation.
PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES = {
    ("accounts", "chk_account_execution_revision"):
        "execution_revision > 0",
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
        "('retry', 'limit', 'skip', 'captcha', 'risk', 'auth'))) "
        "OR ((status = 'unknown') AND (attempt_no > 0) "
        "AND (started_at IS NOT NULL) AND (completed_at IS NOT NULL) "
        "AND (outcome IS NOT NULL) AND (outcome = 'unknown') "
        "AND (reconciliation_note IS NOT NULL) "
        "AND (CHAR_LENGTH(TRIM(reconciliation_note)) > 0))"
    ),
    ("external_action_intents", "chk_external_action_lease_generation"):
        "lease_generation > 0",
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
}
PRODUCTION_REQUIRED_TRIGGERS = set(PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS)


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
                  SUB_PART
           FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE()
           ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"""
    )
    actual_indexes: dict[tuple[str, str], list[tuple[int, str]]] = {}
    index_non_unique: dict[tuple[str, str], set[int]] = {}
    index_prefix_lengths: dict[tuple[str, str], list[object]] = {}
    for row in index_rows:
        key = (row["TABLE_NAME"], row["INDEX_NAME"])
        actual_indexes.setdefault(key, []).append(
            (int(row["SEQ_IN_INDEX"]), row["COLUMN_NAME"])
        )
        index_non_unique.setdefault(key, set()).add(int(row["NON_UNIQUE"]))
        index_prefix_lengths.setdefault(key, []).append(row["SUB_PART"])
    for key, expected_columns in PRODUCTION_REQUIRED_UNIQUE_INDEXES.items():
        if key[0] not in existing_tables:
            continue
        actual_columns = tuple(
            column for _, column in sorted(actual_indexes.get(key, []))
        )
        if (
            actual_columns != expected_columns
            or index_non_unique.get(key) != {0}
            or any(
                prefix_length is not None
                for prefix_length in index_prefix_lengths.get(key, [])
            )
        ):
            missing.append(f"unique_index:{key[0]}.{key[1]}")

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

    trigger_rows = await database.fetch_all(
        """SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE,
                  ACTION_TIMING, ACTION_STATEMENT
           FROM information_schema.TRIGGERS
           WHERE TRIGGER_SCHEMA = DATABASE()"""
    )
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
