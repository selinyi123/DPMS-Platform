import base64
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.migrations_runner import (  # noqa: E402
    MIGRATIONS_DIR,
    MIGRATION_LOCK_NAME,
    MIGRATION_LOCK_TIMEOUT_SECONDS,
    MIN_INNODB_PAGE_SIZE,
    PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
    PRODUCTION_REQUIRED_COLUMN_DEFINITIONS,
    PRODUCTION_REQUIRED_COLUMNS,
    PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES,
    PRODUCTION_REQUIRED_FOREIGN_KEYS,
    PRODUCTION_REQUIRED_TABLES,
    PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS,
    PRODUCTION_REQUIRED_UNIQUE_INDEXES,
    discover_migrations,
    parse_version,
    pending_migrations,
    migration_checksum,
    normalise_check_clause,
    run_migrations,
    split_statements,
    trigger_statement_checksum,
    verify_production_schema,
)


class ParseVersionTests(unittest.TestCase):
    def test_valid_names(self):
        self.assertEqual(parse_version("0001_baseline.sql"), "0001")
        self.assertEqual(parse_version("0042_add_index.sql"), "0042")

    def test_invalid_names(self):
        self.assertIsNone(parse_version("baseline.sql"))
        self.assertIsNone(parse_version("001_too_short.sql"))
        self.assertIsNone(parse_version("0001_baseline.txt"))
        self.assertIsNone(parse_version("README.md"))


class DiscoverAndPendingTests(unittest.TestCase):
    def test_discovers_in_version_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "0002_second.sql").write_text("SELECT 1;")
            (tmp / "0001_first.sql").write_text("SELECT 1;")
            (tmp / "notes.txt").write_text("ignore me")
            found = discover_migrations(tmp)
            self.assertEqual([v for v, _ in found], ["0001", "0002"])

    def test_duplicate_versions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "0001_a.sql").write_text("SELECT 1;")
            (tmp / "0001_b.sql").write_text("SELECT 1;")
            with self.assertRaises(ValueError):
                discover_migrations(tmp)

    def test_missing_dir_is_empty(self):
        self.assertEqual(discover_migrations(Path("/no/such/dir")), [])

    def test_pending_excludes_applied(self):
        migrations = [("0001", Path("a")), ("0002", Path("b")), ("0003", Path("c"))]
        pending = pending_migrations(migrations, applied={"0001", "0002"})
        self.assertEqual([v for v, _ in pending], ["0003"])

    def test_pending_empty_when_all_applied(self):
        migrations = [("0001", Path("a"))]
        self.assertEqual(pending_migrations(migrations, applied={"0001"}), [])

    def test_checksum_binds_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_bytes(b"SELECT 1;\n")
            first = migration_checksum(path)
            path.write_bytes(b"SELECT 2;\n")
            second = migration_checksum(path)

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)


class SplitStatementsTests(unittest.TestCase):
    def test_splits_and_strips_comments(self):
        sql = """
        -- a comment
        CREATE TABLE x (id INT);
        ALTER TABLE x ADD COLUMN y INT;
        """
        statements = split_statements(sql)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("CREATE TABLE x"))
        self.assertTrue(statements[1].startswith("ALTER TABLE x"))

    def test_blank_and_comment_only_yield_nothing(self):
        self.assertEqual(split_statements("-- just a comment\n\n   "), [])


class _FakeMigrationConnection:
    def __init__(self, applied_rows=None, checksum_column=None):
        self.calls = []
        self.applied_rows = list(applied_rows or [])
        self.checksum_column = checksum_column or {
            "COLUMN_TYPE": "char(64)",
            "IS_NULLABLE": "NO",
        }

    async def fetch_one(self, query, values=None):
        self.calls.append(("fetch_one", query, values))
        if "GET_LOCK" in query:
            return {"acquired": 1}
        if "RELEASE_LOCK" in query:
            return {"released": 1}
        if "information_schema.COLUMNS" in query:
            return self.checksum_column
        raise AssertionError(f"unexpected fetch_one: {query}")

    async def fetch_all(self, query, values=None):
        self.calls.append(("fetch_all", query, values))
        if "SELECT version, checksum FROM schema_migrations" in query:
            return list(self.applied_rows)
        raise AssertionError(f"unexpected fetch_all: {query}")

    async def execute(self, query, values=None):
        self.calls.append(("execute", query, values))


class _FakeConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeMigrationDatabase:
    def __init__(self, applied_rows=None, checksum_column=None):
        self.bound_connection = _FakeMigrationConnection(
            applied_rows,
            checksum_column,
        )

    def connection(self):
        return _FakeConnectionContext(self.bound_connection)


class MigrationConnectionContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_uses_one_connection_and_releases_named_lock(self):
        fake_database = _FakeMigrationDatabase()

        with patch("app.migrations_runner.database", fake_database), patch(
            "app.migrations_runner.verify_production_schema", new=AsyncMock()
        ):
            applied = await run_migrations(Path("/no/such/migrations"))

        self.assertEqual(applied, [])
        calls = fake_database.bound_connection.calls
        self.assertIn("GET_LOCK", calls[0][1])
        self.assertTrue(
            any(
                "CREATE TABLE IF NOT EXISTS schema_migrations" in call[1]
                for call in calls
            )
        )
        self.assertIn("RELEASE_LOCK", calls[-1][1])

    async def test_recorded_checksum_mismatch_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            fake_database = _FakeMigrationDatabase(
                [{"version": "0001", "checksum": "0" * 64}]
            )
            with patch("app.migrations_runner.database", fake_database), patch(
                "app.migrations_runner.verify_production_schema", new=AsyncMock()
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    await run_migrations(Path(tmp))

        calls = fake_database.bound_connection.calls
        self.assertFalse(
            any(call[0] == "execute" and str(call[1]).strip() == "SELECT 1" for call in calls)
        )
        self.assertIn("RELEASE_LOCK", calls[-1][1])

    async def test_legacy_null_checksum_is_backfilled_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            expected_checksum = migration_checksum(path)
            fake_database = _FakeMigrationDatabase(
                [{"version": "0001", "checksum": None}],
                {"COLUMN_TYPE": "char(64)", "IS_NULLABLE": "YES"},
            )
            with patch("app.migrations_runner.database", fake_database), patch(
                "app.migrations_runner.verify_production_schema", new=AsyncMock()
            ):
                applied = await run_migrations(Path(tmp))

        self.assertEqual(applied, [])
        backfills = [
            call for call in fake_database.bound_connection.calls
            if call[0] == "execute" and "SET checksum = :checksum" in call[1]
        ]
        self.assertEqual(len(backfills), 1)
        self.assertEqual(backfills[0][2]["checksum"], expected_checksum)
        self.assertTrue(any(
            call[0] == "execute"
            and "MODIFY COLUMN checksum CHAR(64) NOT NULL" in call[1]
            for call in fake_database.bound_connection.calls
        ))

    async def test_non_lowercase_or_non_hex_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            for invalid_checksum in {"A" * 64, "g" * 64, "", "0" * 63}:
                with self.subTest(checksum=repr(invalid_checksum)):
                    fake_database = _FakeMigrationDatabase([
                        {"version": "0001", "checksum": invalid_checksum}
                    ])
                    with patch(
                        "app.migrations_runner.database", fake_database
                    ), patch(
                        "app.migrations_runner.verify_production_schema",
                        new=AsyncMock(),
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "checksum has invalid format",
                        ):
                            await run_migrations(Path(tmp))


class ProductionSchemaSemanticContractTests(unittest.TestCase):
    def test_security_critical_columns_include_exact_semantics(self):
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[
                ("accounts", "execution_revision")
            ],
            ("bigint unsigned", "NO", "1"),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[
                ("external_action_intents", "effect_certainty")
            ],
            ("varchar(32)", "NO", "not_started"),
        )

    def test_check_normalisation_preserves_logic_but_ignores_mysql_formatting(self):
        actual = "((`status` = _utf8mb4'failed') AND (`effect_certainty` = _utf8mb4'confirmed_no_effect'))"
        expected = "(status = 'failed') AND (effect_certainty = 'confirmed_no_effect')"
        self.assertEqual(normalise_check_clause(actual), normalise_check_clause(expected))
        self.assertNotEqual(
            normalise_check_clause(actual),
            normalise_check_clause("1"),
        )

    def test_check_normalisation_does_not_erase_literal_suffixes(self):
        self.assertIn(
            "'not_started'",
            normalise_check_clause("effect_certainty = 'not_started'"),
        )
        self.assertNotEqual(
            normalise_check_clause("effect_certainty = 'not_started'"),
            normalise_check_clause("effect_certainty = 'not'"),
        )
        self.assertEqual(
            normalise_check_clause("status = _utf8mb4'failed'"),
            normalise_check_clause("status = 'failed'"),
        )

    def test_check_normalisation_handles_mysql_escaped_delimiters(self):
        self.assertEqual(
            normalise_check_clause(r"status IN (_utf8mb4\'pending\')"),
            normalise_check_clause("status IN ('pending')"),
        )

    def test_check_normalisation_preserves_boolean_precedence(self):
        self.assertEqual(
            normalise_check_clause("a = 1 AND ((b = 2) AND (c = 3))"),
            normalise_check_clause("(a = 1 AND b = 2) AND c = 3"),
        )
        self.assertNotEqual(
            normalise_check_clause("a = 1 AND (b = 2 OR c = 3)"),
            normalise_check_clause("(a = 1 AND b = 2) OR c = 3"),
        )

    def test_effect_certainty_clause_is_verified_semantically(self):
        clause = PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES[
            ("external_action_intents", "chk_external_action_effect_certainty_v2")
        ]
        self.assertIn("confirmed_no_effect", clause)
        self.assertIn(
            ("external_action_intents", "chk_external_action_lifecycle_v2"),
            PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES,
        )

    def test_every_required_check_has_one_exact_clause(self):
        self.assertEqual(
            set(PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES),
            PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
        )

    def test_trigger_contract_hash_matches_migration_0008(self):
        trigger_sql = (MIGRATIONS_DIR / "0008_recreate_terminal_outbox_trigger_collation.sql").read_text(
            encoding="utf-8"
        )
        action_statement = trigger_sql.split(" FOR EACH ROW ", 1)[1].rstrip(";\n")
        expected = PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS[
            "trg_task_runs_terminal_outbox"
        ]
        self.assertEqual(expected[:3], ("AFTER", "UPDATE", "task_runs"))
        self.assertEqual(
            trigger_statement_checksum(action_statement),
            expected[3],
        )
        self.assertNotEqual(
            trigger_statement_checksum("SELECT 'TaskFinished'"),
            trigger_statement_checksum("SELECT 'taskfinished'"),
        )


class ProductionSchemaVerifierTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _database(fetch_all_results, page_size=MIN_INNODB_PAGE_SIZE):
        return SimpleNamespace(
            fetch_one=AsyncMock(
                return_value={"innodb_page_size": page_size}
            ),
            fetch_all=AsyncMock(side_effect=fetch_all_results),
        )

    @staticmethod
    def _contract_patches(**overrides):
        values = {
            "PRODUCTION_REQUIRED_TABLES": {"t"},
            "PRODUCTION_REQUIRED_COLUMNS": {},
            "PRODUCTION_REQUIRED_COLUMN_DEFINITIONS": {},
            "PRODUCTION_REQUIRED_UNIQUE_INDEXES": {},
            "PRODUCTION_REQUIRED_FOREIGN_KEYS": {},
            "PRODUCTION_REQUIRED_CHECK_CONSTRAINTS": set(),
            "PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES": {},
            "PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS": {},
            "PRODUCTION_REQUIRED_TRIGGERS": set(),
        }
        values.update(overrides)
        return [patch(f"app.migrations_runner.{name}", value) for name, value in values.items()]

    async def _verify(self, database, **contract):
        patches = self._contract_patches(**contract)
        with patch("app.migrations_runner.database", database):
            for contract_patch in patches:
                contract_patch.start()
            try:
                await verify_production_schema()
            finally:
                for contract_patch in reversed(patches):
                    contract_patch.stop()

    async def test_rejects_small_innodb_page_size(self):
        database = self._database(
            [[{"TABLE_NAME": "t"}], [], [], [], []],
            page_size=8192,
        )
        with self.assertRaisesRegex(RuntimeError, "innodb_page_size"):
            await self._verify(database)

    async def test_rejects_unique_index_column_prefix(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [{
                "TABLE_NAME": "t",
                "INDEX_NAME": "uk_t_c",
                "COLUMN_NAME": "c",
                "SEQ_IN_INDEX": 1,
                "NON_UNIQUE": 0,
                "SUB_PART": 8,
            }],
            [],
            [],
            [],
        ])
        with self.assertRaisesRegex(RuntimeError, "unique_index:t.uk_t_c"):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_UNIQUE_INDEXES={
                    ("t", "uk_t_c"): ("c",)
                },
            )

    async def test_rejects_cascading_foreign_key_rule(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [],
            [{
                "TABLE_NAME": "t",
                "CONSTRAINT_NAME": "fk_t_parent",
                "COLUMN_NAME": "parent_id",
                "ORDINAL_POSITION": 1,
                "REFERENCED_TABLE_NAME": "parent",
                "REFERENCED_COLUMN_NAME": "id",
                "DELETE_RULE": "CASCADE",
                "UPDATE_RULE": "RESTRICT",
            }],
            [],
            [],
        ])
        with self.assertRaisesRegex(RuntimeError, "foreign_key:t.fk_t_parent"):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_FOREIGN_KEYS={
                    ("t", "fk_t_parent"): (
                        ("parent_id",),
                        "parent",
                        ("id",),
                    )
                },
            )

    async def test_rejects_unenforced_or_drifted_check_clause(self):
        key = ("t", "chk_t")
        for enforced, clause, error in {
            ("NO", "c > 0", "check_enforced:t.chk_t"),
            ("YES", "c >= 0", "check_clause:t.chk_t"),
        }:
            with self.subTest(enforced=enforced, clause=clause):
                database = self._database([
                    [{"TABLE_NAME": "t"}],
                    [],
                    [],
                    [{
                        "TABLE_NAME": "t",
                        "CONSTRAINT_NAME": "chk_t",
                        "ENFORCED": enforced,
                        "CHECK_CLAUSE": clause,
                    }],
                    [],
                ])
                with self.assertRaisesRegex(RuntimeError, error):
                    await self._verify(
                        database,
                        PRODUCTION_REQUIRED_CHECK_CONSTRAINTS={key},
                        PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES={key: "c > 0"},
                    )

    async def test_rejects_trigger_metadata_or_body_drift(self):
        statement = "INSERT INTO audit_log(id) VALUES (1)"
        definition = (
            "AFTER",
            "UPDATE",
            "t",
            trigger_statement_checksum(statement),
        )
        valid_row = {
                "TRIGGER_NAME": "trg_t",
                "EVENT_MANIPULATION": "UPDATE",
                "EVENT_OBJECT_TABLE": "t",
                "ACTION_TIMING": "AFTER",
                "ACTION_STATEMENT": statement,
        }
        drifts = {
            "timing": {"ACTION_TIMING": "BEFORE"},
            "event": {"EVENT_MANIPULATION": "DELETE"},
            "table": {"EVENT_OBJECT_TABLE": "other_table"},
            "body": {"ACTION_STATEMENT": statement + ", (2)"},
        }
        for drift_kind, changes in drifts.items():
            with self.subTest(drift=drift_kind):
                row = {**valid_row, **changes}
                database = self._database([
                    [{"TABLE_NAME": "t"}],
                    [],
                    [],
                    [],
                    [row],
                ])
                with self.assertRaisesRegex(
                    RuntimeError,
                    "trigger_definition:trg_t",
                ):
                    await self._verify(
                        database,
                        PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS={
                            "trg_t": definition
                        },
                        PRODUCTION_REQUIRED_TRIGGERS={"trg_t"},
                    )


class RealBaselineTests(unittest.TestCase):
    def test_baseline_migration_present_and_named(self):
        found = discover_migrations(MIGRATIONS_DIR)
        versions = [v for v, _ in found]
        self.assertIn("0001", versions)

    def test_baseline_statements_parse(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        statements = split_statements(Path(found["0001"]).read_text(encoding="utf-8"))
        self.assertTrue(statements)
        self.assertTrue(all(stmt.strip() for stmt in statements))

    def test_bilibili_action_ledger_migration_present(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0009", found)
        sql = Path(found["0009"]).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS bilibili_action_ledger", sql)
        self.assertIn("uk_bilibili_action_task_action", sql)

    def test_real_run_v2_contract_migration_present_and_parseable(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0010", found)
        sql = Path(found["0010"]).read_text(encoding="utf-8")
        statements = split_statements(sql)

        self.assertGreaterEqual(len(statements), 8)
        self.assertTrue(all(statement.strip() for statement in statements))

    def test_real_run_v2_upgrade_migration_present_and_parseable(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0011", found)
        sql = Path(found["0011"]).read_text(encoding="utf-8")
        statements = split_statements(sql)

        self.assertGreaterEqual(len(statements), 80)
        self.assertTrue(all(statement.strip() for statement in statements))


class RealRunV2ContractTests(unittest.TestCase):
    def setUp(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        self.sql = Path(found["0010"]).read_text(encoding="utf-8").lower()
        self.upgrade_sql = Path(found["0011"]).read_text(encoding="utf-8").lower()
        self.compact_sql = " ".join(self.sql.split())
        self.token_sql = "".join(self.sql.split())
        self.all_token_sql = "".join((self.sql + self.upgrade_sql).split())

    def test_creates_v2_contract_tables(self):
        for table in {
            "lottery_rule_snapshots",
            "execution_evidence_bindings",
            "account_operation_leases",
            "external_action_intents",
        }:
            with self.subTest(table=table):
                self.assertIn(f"create table if not exists {table}", self.sql)

    def test_alters_existing_v2_contract_tables(self):
        for table in {"lotteries", "task_runs", "adapter_calibrations"}:
            with self.subTest(table=table):
                self.assertIn(f"alter table {table}", self.sql)

    def test_contract_has_exact_bindings_and_idempotency_keys(self):
        for token in {
            "idx_rule_snapshot_lottery_hash",
            "idx_execution_evidence_exact_binding",
            "uk_adapter_probe_exact_binding",
            "uk_external_action_task_action",
            "fk_lottery_authoritative_rule_snapshot",
            "fk_task_run_execution_evidence",
            "fk_execution_evidence_probe",
            "fk_execution_evidence_shadow",
            "fk_external_action_lease_binding",
        }:
            with self.subTest(token=token):
                self.assertIn(token, self.sql)

    def test_production_schema_contract_requires_v2_tables_and_columns(self):
        required = {
            "lottery_rule_snapshots": {
                "lottery_id",
                "rule_text",
                "rule_hash",
                "is_complete",
                "attested_by",
                "attested_at",
            },
            "execution_evidence_bindings": {
                "lottery_id",
                "account_id",
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
                "expires_at",
            },
            "account_operation_leases": {
                "account_id",
                "lease_id",
                "generation",
                "task_id",
                "expires_at",
                "released_at",
            },
            "external_action_intents": {
                "intent_id",
                "task_id",
                "lease_id",
                "lease_generation",
                "action",
                "payload_hash",
                "status",
                "effect_certainty",
                "attempt_no",
                "outcome",
                "reconciliation_note",
            },
            "lotteries": {
                "authoritative_rule_snapshot_id",
                "rule_hash",
                "action_plan_hash",
            },
            "task_runs": {
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
            "adapter_calibrations": {
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
            "accounts": {"execution_revision"},
        }

        self.assertTrue(required.keys() <= PRODUCTION_REQUIRED_TABLES)
        for table, columns in required.items():
            with self.subTest(table=table):
                self.assertTrue(columns <= PRODUCTION_REQUIRED_COLUMNS[table])

    def test_incomplete_snapshot_does_not_block_independent_attestation(self):
        self.assertIn("index idx_rule_snapshot_lottery_hash", self.sql)
        self.assertNotIn("unique key uk_rule_snapshot_lottery_hash", self.sql)

    def test_evidence_binds_snapshot_and_requires_probe_shadow_pair(self):
        self.assertIn("rule_snapshot_id bigint not null", self.sql)
        self.assertIn("chk_execution_evidence_pair", self.sql)
        self.assertIn("chk_execution_evidence_verified", self.sql)
        self.assertIn("probe_id is not null", self.sql)
        self.assertIn("shadow_task_id is not null", self.sql)
        self.assertIn("expires_at > verified_at", self.sql)
        self.assertIn("fk_execution_evidence_rule_snapshot", self.sql)
        for column in (
            "probe_observation_hash",
            "shadow_observation_hash",
            "probe_observation_kind",
            "shadow_observation_kind",
        ):
            self.assertIn(f"{column} is not null", self.sql)

    def test_probe_and_shadow_bind_the_same_full_contract(self):
        expected_probe = (
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
        )
        expected_probe_source = expected_probe[:-2] + (
            "observation_kind",
            "observation_hash",
        )
        expected_shadow = (
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
        )
        expected_shadow_source = ("task_id",) + expected_shadow[1:-2] + (
            "preflight_observation_kind",
            "preflight_observation_hash",
        )
        probe_contract = PRODUCTION_REQUIRED_FOREIGN_KEYS[
            ("execution_evidence_bindings", "fk_execution_evidence_probe_v2")
        ]
        shadow_contract = PRODUCTION_REQUIRED_FOREIGN_KEYS[
            ("execution_evidence_bindings", "fk_execution_evidence_shadow_v2")
        ]
        self.assertEqual(probe_contract[0], expected_probe)
        self.assertEqual(probe_contract[2], expected_probe_source)
        self.assertEqual(shadow_contract[0], expected_shadow)
        self.assertEqual(shadow_contract[2], expected_shadow_source)

    def test_declared_key_order_matches_migration_sql(self):
        for (_, index_name), columns in PRODUCTION_REQUIRED_UNIQUE_INDEXES.items():
            with self.subTest(index=index_name):
                signature = (
                    f"unique key {index_name} ({', '.join(columns)})"
                )
                self.assertIn("".join(signature.split()), self.all_token_sql)
        for (_, constraint_name), contract in PRODUCTION_REQUIRED_FOREIGN_KEYS.items():
            local_columns, referenced_table, referenced_columns = contract
            with self.subTest(foreign_key=constraint_name):
                signature = (
                    f"constraint {constraint_name} "
                    f"foreign key ({', '.join(local_columns)}) "
                    f"references {referenced_table}"
                    f"({', '.join(referenced_columns)})"
                )
                self.assertIn("".join(signature.split()), self.all_token_sql)

    def test_evidence_pair_is_idempotent_without_freezing_expired_contracts(self):
        self.assertIn(
            "unique key uk_execution_evidence_probe_shadow (probe_id, shadow_task_id)",
            self.compact_sql,
        )
        self.assertIn("index idx_execution_evidence_exact_binding", self.sql)
        self.assertNotIn("unique key uk_execution_evidence_exact_binding", self.sql)

    def test_leases_are_append_only_and_generation_fenced(self):
        self.assertIn(
            "create table if not exists account_operation_leases "
            "( lease_id char(36) primary key, account_id bigint not null",
            self.compact_sql,
        )
        self.assertNotIn("account_id bigint primary key", self.compact_sql)
        self.assertIn("uk_account_operation_generation", self.sql)
        self.assertEqual(
            PRODUCTION_REQUIRED_FOREIGN_KEYS[
                ("external_action_intents", "fk_external_action_lease_binding")
            ],
            (
                ("lease_id", "account_id", "lease_generation"),
                "account_operation_leases",
                ("lease_id", "account_id", "generation"),
            ),
        )

    def test_circular_references_have_an_insertable_first_leg(self):
        # lottery -> snapshot: insert lottery with NULL authoritative pointer,
        # insert snapshot, then update lottery. lease -> task: insert lease with
        # NULL task_id, insert task with lease binding, then backfill task_id.
        self.assertIn(
            "add column authoritative_rule_snapshot_id bigint null",
            self.sql,
        )
        self.assertIn("task_id char(36) null", self.sql)
        self.assertIn("add column account_lease_id char(36) null", self.sql)

    def test_existing_rows_remain_compatible_with_v2_columns(self):
        for token in {
            "add column authoritative_rule_snapshot_id bigint null",
            "add column rule_snapshot_id bigint null",
            "add column execution_evidence_id char(36) null",
            "add column account_lease_id char(36) null",
            "add column account_lease_generation bigint unsigned null",
            "add column reconciliation_required tinyint unsigned not null default 0",
        }:
            with self.subTest(token=token):
                self.assertIn(token, self.sql)

    def test_retry_guards_every_existing_table_alter(self):
        # New tables use atomic CREATE TABLE IF NOT EXISTS. Every ALTER of an
        # existing table is one atomic MySQL 8 statement behind a metadata
        # guard, so a crash before version recording can be retried.
        self.assertEqual(self.sql.count("prepare dpms_stmt from @dpms_sql"), 5)
        self.assertEqual(self.sql.count("execute dpms_stmt"), 5)
        self.assertEqual(self.sql.count("deallocate prepare dpms_stmt"), 5)
        self.assertGreaterEqual(self.sql.count("information_schema."), 5)

    def test_production_drift_contract_includes_keys_and_checks(self):
        self.assertIn(
            ("execution_evidence_bindings", "uk_execution_evidence_probe_shadow"),
            PRODUCTION_REQUIRED_UNIQUE_INDEXES,
        )
        for constraint in {
            ("execution_evidence_bindings", "chk_execution_evidence_status"),
            ("execution_evidence_bindings", "chk_execution_evidence_pair"),
            ("execution_evidence_bindings", "chk_execution_evidence_verified"),
            ("execution_evidence_bindings", "chk_execution_evidence_observation_hashes_v2"),
            ("external_action_intents", "chk_external_action_effect_certainty_v2"),
            ("external_action_intents", "chk_external_action_lifecycle_v2"),
        }:
            self.assertIn(constraint, PRODUCTION_REQUIRED_CHECK_CONSTRAINTS)

    def test_runner_serializes_migrations_on_one_named_lock(self):
        self.assertEqual(MIGRATION_LOCK_NAME, "dpms:schema_migrations")
        self.assertGreater(MIGRATION_LOCK_TIMEOUT_SECONDS, 0)


class RealRunV2UpgradeTests(unittest.TestCase):
    def setUp(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        self.sql = Path(found["0011"]).read_text(encoding="utf-8").lower()
        self.compact_sql = " ".join(self.sql.split())
        self.token_sql = "".join(self.sql.split())

    def test_adds_legacy_missing_columns_with_column_level_guards(self):
        columns = {
            ("accounts", "execution_revision"),
            ("adapter_calibrations", "observation_kind"),
            ("adapter_calibrations", "observation_hash"),
            ("task_runs", "preflight_observation"),
            ("task_runs", "preflight_observation_kind"),
            ("task_runs", "preflight_observation_hash"),
            ("execution_evidence_bindings", "probe_observation_kind"),
            ("execution_evidence_bindings", "probe_observation_hash"),
            ("execution_evidence_bindings", "shadow_observation_kind"),
            ("execution_evidence_bindings", "shadow_observation_hash"),
            ("external_action_intents", "effect_certainty"),
        }
        for table, column in columns:
            with self.subTest(table=table, column=column):
                metadata_guard = (
                    f"table_name = '{table}' and column_name = '{column}'"
                )
                self.assertIn(metadata_guard, self.compact_sql)
                self.assertIn(f"add column {column} ", self.compact_sql)

    def test_rebuilds_observation_keys_after_dropping_evidence_fks(self):
        objects = {
            "fk_execution_evidence_probe",
            "fk_execution_evidence_shadow",
            "uk_adapter_probe_exact_binding",
            "idx_adapter_probe_binding_lookup",
            "uk_task_run_shadow_binding",
            "idx_task_run_shadow_evidence_lookup",
            "idx_execution_evidence_exact_binding",
        }
        for name in objects:
            with self.subTest(name=name):
                self.assertGreaterEqual(self.sql.count(name), 3)

        first_source_index_drop = self.sql.index(
            "drop index uk_adapter_probe_exact_binding"
        )
        self.assertLess(
            self.sql.index("drop foreign key fk_execution_evidence_probe"),
            first_source_index_drop,
        )
        self.assertLess(
            self.sql.index("drop foreign key fk_execution_evidence_shadow"),
            first_source_index_drop,
        )

    def test_exact_keys_include_independent_observation_identity(self):
        for signature in {
            "observation_kind, observation_hash )",
            "preflight_observation_kind, preflight_observation_hash )",
            (
                "probe_observation_kind, probe_observation_hash, "
                "shadow_observation_kind, shadow_observation_hash, "
                "status, expires_at, verified_at"
            ),
        }:
            with self.subTest(signature=signature):
                self.assertIn("".join(signature.split()), self.token_sql)

    def test_revokes_unverifiable_legacy_evidence_before_strict_contract(self):
        revoke = self.sql.index("update execution_evidence_bindings")
        add_check = self.sql.index(
            "add constraint chk_execution_evidence_observation_hashes"
        )
        add_probe_fk = self.sql.index(
            "add constraint fk_execution_evidence_probe"
        )
        add_shadow_fk = self.sql.index(
            "add constraint fk_execution_evidence_shadow"
        )

        self.assertIn("set eeb.status = 'revoked'", self.sql[revoke:add_check])
        self.assertIn("where eeb.status = 'verified'", self.sql[revoke:add_check])
        self.assertLess(revoke, add_check)
        self.assertLess(revoke, add_probe_fk)
        self.assertLess(revoke, add_shadow_fk)
        self.assertNotIn("set probe_observation_hash =", self.sql)
        self.assertNotIn("set shadow_observation_hash =", self.sql)

    def test_observation_check_only_authorizes_verified_evidence(self):
        check_start = self.sql.index(
            "add constraint chk_execution_evidence_observation_hashes"
        )
        check_end = self.sql.index(")'", check_start)
        check_sql = self.sql[check_start:check_end]

        self.assertIn("status <> ''verified''", check_sql)
        self.assertIn("probe_observation_hash is not null", check_sql)
        self.assertIn("shadow_observation_hash is not null", check_sql)
        self.assertIn("probe_observation_kind is not null", check_sql)
        self.assertIn("shadow_observation_kind is not null", check_sql)
        self.assertIn("regexp_like(probe_observation_hash", check_sql)
        self.assertIn("regexp_like(shadow_observation_hash", check_sql)
        self.assertIn("char_length(trim(probe_observation_kind)) > 0", check_sql)
        self.assertIn("char_length(trim(shadow_observation_kind)) > 0", check_sql)

    def test_legacy_failed_intents_are_quarantined_not_declared_no_effect(self):
        task_quarantine = self.sql.index("update task_runs tr")
        failed_migration = self.sql.index(
            "update external_action_intents", task_quarantine
        )
        add_check = self.sql.index(
            "add constraint chk_external_action_effect_certainty"
        )

        self.assertLess(task_quarantine, failed_migration)
        self.assertLess(failed_migration, add_check)
        self.assertIn(
            "set tr.reconciliation_required = 1 where eai.status in ('failed', 'started', 'unknown')",
            self.compact_sql,
        )
        self.assertIn(
            "eai.status = 'succeeded' and tr.status <> 'succeeded'",
            self.compact_sql,
        )
        migrated = self.sql[failed_migration:add_check]
        self.assertIn("set status = 'unknown'", migrated)
        self.assertIn("outcome = 'unknown'", migrated)
        self.assertIn("effect_certainty = 'unknown'", migrated)
        self.assertNotIn(
            "where status = 'failed';\n\nupdate external_action_intents\n"
            "set effect_certainty = 'confirmed_no_effect'",
            migrated,
        )

    def test_effect_certainty_check_encodes_the_full_state_machine(self):
        for token in {
            "status in (''pending'', ''prepared'') and effect_certainty = ''not_started''",
            "status in (''started'', ''unknown'') and effect_certainty = ''unknown''",
            "status = ''succeeded'' and effect_certainty = ''confirmed_effect''",
            "status = ''failed'' and effect_certainty = ''confirmed_no_effect''",
        }:
            with self.subTest(token=token):
                self.assertIn(token, self.compact_sql)

    def test_v2_check_clauses_match_verifier_contract(self):
        for table, constraint in {
            (
                "execution_evidence_bindings",
                "chk_execution_evidence_observation_hashes_v2",
            ),
            (
                "external_action_intents",
                "chk_external_action_effect_certainty_v2",
            ),
            (
                "external_action_intents",
                "chk_external_action_lifecycle_v2",
            ),
        }:
            with self.subTest(constraint=constraint):
                match = re.search(
                    rf"add constraint {re.escape(constraint)} check "
                    rf"\((.*?)\n\s*\) enforced'",
                    self.sql,
                    re.DOTALL,
                )
                self.assertIsNotNone(match)
                migration_clause = match.group(1).replace("''", "'")
                self.assertEqual(
                    normalise_check_clause(migration_clause),
                    normalise_check_clause(
                        PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES[
                            (table, constraint)
                        ]
                    ),
                )

    def test_intent_lifecycle_requires_timestamps_and_known_outcomes(self):
        for token in {
            "status = ''succeeded'' and attempt_no > 0",
            "outcome = ''ok''",
            "outcome in (''retry'', ''limit'', ''skip'', ''captcha'', ''risk'', ''auth'')",
            "status = ''unknown'' and attempt_no > 0",
            "char_length(trim(reconciliation_note)) > 0",
        }:
            with self.subTest(token=token):
                self.assertIn(token, self.compact_sql)

    def test_repairs_security_critical_column_semantics(self):
        self.assertIn(
            "modify column execution_revision bigint unsigned not null default 1",
            self.compact_sql,
        )
        self.assertIn(
            "modify column effect_certainty varchar(32) not null default ''not_started''",
            self.compact_sql,
        )

    def test_every_dynamic_ddl_is_balanced_and_metadata_guarded(self):
        prepared = self.sql.count("prepare dpms_stmt from @dpms_sql")
        self.assertGreaterEqual(prepared, 25)
        self.assertEqual(prepared, self.sql.count("execute dpms_stmt"))
        self.assertEqual(prepared, self.sql.count("deallocate prepare dpms_stmt"))
        self.assertEqual(prepared, self.sql.count("set @dpms_sql = if("))
        self.assertGreaterEqual(self.sql.count("information_schema."), prepared)


if __name__ == "__main__":
    unittest.main()
