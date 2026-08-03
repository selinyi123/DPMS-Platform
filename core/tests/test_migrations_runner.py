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
    MIGRATION_PROCESS_LOCK_NAME,
    MIGRATION_LOCK_TIMEOUT_SECONDS,
    MIN_INNODB_PAGE_SIZE,
    PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
    PRODUCTION_REQUIRED_COLUMN_DEFINITIONS,
    PRODUCTION_REQUIRED_COLUMNS,
    PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES,
    PRODUCTION_FORBIDDEN_INDEXES,
    PRODUCTION_REQUIRED_FOREIGN_KEYS,
    PRODUCTION_REQUIRED_INDEXES,
    PRODUCTION_REQUIRED_PRIMARY_KEYS,
    PRODUCTION_REQUIRED_TABLES,
    PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS,
    PRODUCTION_REQUIRED_UNIQUE_INDEXES,
    discover_migrations,
    parse_version,
    pending_migrations,
    migration_checksum,
    normalise_check_clause,
    run_migrations,
    schema_upgrade_process_lock,
    split_statements,
    trigger_statement_checksum,
    verify_migrations_current,
    verify_production_schema,
)
from app.services.real_run_readiness import (  # noqa: E402
    ACCOUNT_RISK_COOLDOWN_BY_REASON,
    ACCOUNT_RISK_COOLDOWN_HOURS,
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
    async def test_process_lock_covers_the_callers_complete_upgrade_phase(self):
        fake_database = _FakeMigrationDatabase()

        with patch("app.migrations_runner.database", fake_database):
            async with schema_upgrade_process_lock():
                calls_inside = list(
                    fake_database.bound_connection.calls
                )
                self.assertEqual(len(calls_inside), 1)
                self.assertIn("GET_LOCK", calls_inside[0][1])
                self.assertEqual(
                    calls_inside[0][2]["lock_name"],
                    MIGRATION_PROCESS_LOCK_NAME,
                )

        calls = fake_database.bound_connection.calls
        self.assertIn("RELEASE_LOCK", calls[-1][1])
        self.assertEqual(
            calls[-1][2]["lock_name"],
            MIGRATION_PROCESS_LOCK_NAME,
        )

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


class ReadOnlyMigrationStartupGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_history_is_verified_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            fake_database = SimpleNamespace(
                fetch_all=AsyncMock(
                    return_value=[
                        {
                            "version": "0001",
                            "checksum": migration_checksum(path),
                        }
                    ]
                )
            )
            with patch(
                "app.migrations_runner.database",
                fake_database,
            ), patch(
                "app.migrations_runner.verify_production_schema",
                new=AsyncMock(),
            ) as verify_schema:
                await verify_migrations_current(Path(tmp))

        fake_database.fetch_all.assert_awaited_once()
        self.assertFalse(hasattr(fake_database, "execute"))
        verify_schema.assert_awaited_once()

    async def test_pending_migration_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            fake_database = SimpleNamespace(
                fetch_all=AsyncMock(return_value=[])
            )
            with patch(
                "app.migrations_runner.database",
                fake_database,
            ), patch(
                "app.migrations_runner.verify_production_schema",
                new=AsyncMock(),
            ) as verify_schema:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "pending:0001",
                ):
                    await verify_migrations_current(Path(tmp))

        verify_schema.assert_not_awaited()

    async def test_unknown_or_modified_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_example.sql"
            path.write_text("SELECT 1;", encoding="utf-8")
            fake_database = SimpleNamespace(
                fetch_all=AsyncMock(
                    return_value=[
                        {"version": "0001", "checksum": "0" * 64},
                        {"version": "9999", "checksum": "0" * 64},
                    ]
                )
            )
            with patch(
                "app.migrations_runner.database",
                fake_database,
            ), patch(
                "app.migrations_runner.verify_production_schema",
                new=AsyncMock(),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "checksum_mismatch:0001",
                ) as raised:
                    await verify_migrations_current(Path(tmp))

        self.assertIn("missing_file:9999", str(raised.exception))


class ProductionSchemaSemanticContractTests(unittest.TestCase):
    def test_security_critical_columns_include_exact_semantics(self):
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[("task_phases", "phase")],
            (
                "enum('init','followed','liked','commented','favorited','reposted','completed')",
                "YES",
                "init",
            ),
        )
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
            "PRODUCTION_FORBIDDEN_INDEXES": set(),
            "PRODUCTION_REQUIRED_UNIQUE_INDEXES": {},
            "PRODUCTION_REQUIRED_PRIMARY_KEYS": {},
            "PRODUCTION_REQUIRED_INDEXES": {},
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

    async def test_rejects_forbidden_obsolete_index(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [{
                "TABLE_NAME": "t",
                "INDEX_NAME": "uk_t_obsolete",
                "COLUMN_NAME": "c",
                "SEQ_IN_INDEX": 1,
                "NON_UNIQUE": 0,
                "SUB_PART": None,
            }],
            [],
            [],
            [],
        ])
        with self.assertRaisesRegex(
            RuntimeError,
            "forbidden_index:t.uk_t_obsolete",
        ):
            await self._verify(
                database,
                PRODUCTION_FORBIDDEN_INDEXES={("t", "uk_t_obsolete")},
            )

    async def test_rejects_missing_or_drifted_primary_key(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [{
                "TABLE_NAME": "t",
                "INDEX_NAME": "PRIMARY",
                "COLUMN_NAME": "other_id",
                "SEQ_IN_INDEX": 1,
                "NON_UNIQUE": 0,
                "SUB_PART": None,
            }],
            [],
            [],
            [],
        ])
        with self.assertRaisesRegex(RuntimeError, "primary_key:t"):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_PRIMARY_KEYS={"t": ("id",)},
            )

    async def test_rejects_drifted_required_non_unique_index(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [
                {
                    "TABLE_NAME": "t",
                    "INDEX_NAME": "idx_t_lane",
                    "COLUMN_NAME": "status",
                    "SEQ_IN_INDEX": 1,
                    "NON_UNIQUE": 1,
                    "SUB_PART": None,
                },
                {
                    "TABLE_NAME": "t",
                    "INDEX_NAME": "idx_t_lane",
                    "COLUMN_NAME": "stream_key",
                    "SEQ_IN_INDEX": 2,
                    "NON_UNIQUE": 1,
                    "SUB_PART": None,
                },
            ],
            [],
            [],
            [],
        ])
        with self.assertRaisesRegex(RuntimeError, "index:t.idx_t_lane"):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_INDEXES={
                    ("t", "idx_t_lane"): (
                        "stream_key",
                        "status",
                        "id",
                    )
                },
            )

    async def test_rejects_invisible_required_index(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [{
                "TABLE_NAME": "t",
                "INDEX_NAME": "idx_t_lane",
                "COLUMN_NAME": "id",
                "SEQ_IN_INDEX": 1,
                "NON_UNIQUE": 1,
                "SUB_PART": None,
                "IS_VISIBLE": "NO",
            }],
            [],
            [],
            [],
        ])
        with self.assertRaisesRegex(RuntimeError, "index:t.idx_t_lane"):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_INDEXES={
                    ("t", "idx_t_lane"): ("id",)
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
                "CONTRACT_VERSION": "dpms-trigger-metadata-v1",
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

    async def test_trigger_metadata_reader_fails_closed(self):
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [],
            [],
            [],
            RuntimeError("routine unavailable"),
        ])
        with self.assertRaisesRegex(
            RuntimeError,
            "trigger_metadata_reader",
        ):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS={
                    "trg_t": (
                        "AFTER",
                        "UPDATE",
                        "t",
                        "0" * 64,
                    )
                },
                PRODUCTION_REQUIRED_TRIGGERS={"trg_t"},
            )

    async def test_rejects_trigger_metadata_reader_contract_drift(self):
        statement = "INSERT INTO audit_log(id) VALUES (1)"
        database = self._database([
            [{"TABLE_NAME": "t"}],
            [],
            [],
            [],
            [{
                "CONTRACT_VERSION": "unexpected",
                "TRIGGER_NAME": "trg_t",
                "EVENT_MANIPULATION": "UPDATE",
                "EVENT_OBJECT_TABLE": "t",
                "ACTION_TIMING": "AFTER",
                "ACTION_STATEMENT": statement,
            }],
        ])
        with self.assertRaisesRegex(
            RuntimeError,
            "trigger_metadata_reader_contract",
        ):
            await self._verify(
                database,
                PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS={
                    "trg_t": (
                        "AFTER",
                        "UPDATE",
                        "t",
                        trigger_statement_checksum(statement),
                    )
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

    def test_runtime_trigger_metadata_reader_migration_is_read_only(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        migration = Path(found["0028"])
        sql = migration.read_text(encoding="utf-8")
        compact = " ".join(sql.split())
        self.assertIn(
            "CREATE PROCEDURE dpms_required_trigger_metadata()",
            compact,
        )
        self.assertIn("SQL SECURITY DEFINER", compact)
        self.assertIn("READS SQL DATA", compact)
        self.assertIn("FROM information_schema.TRIGGERS", compact)
        routine = compact.split(
            "CREATE PROCEDURE dpms_required_trigger_metadata()",
            maxsplit=1,
        )[1]
        for mutating_keyword in (" INSERT ", " UPDATE ", " DELETE "):
            self.assertNotIn(mutating_keyword, routine)

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

    def test_published_real_run_migration_bytes_remain_frozen(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertEqual(
            migration_checksum(found["0010"]),
            "9dc1545aef147e5ac67473c194864d306fa153f8532fb10b81a9e49e6aec2db2",
        )
        self.assertEqual(
            migration_checksum(found["0011"]),
            "46d1667d0d64711943f4a96305250a3264075b16fa8623bfdec42f6a61079a54",
        )

    def test_execution_intent_migration_and_manual_rollback_are_present(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0014", found)
        sql = Path(found["0014"]).read_text(encoding="utf-8")
        statements = split_statements(sql)
        self.assertGreaterEqual(len(statements), 19)
        self.assertIn(
            "DROP FOREIGN KEY fk_task_run_execution_evidence",
            sql,
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS lottery_execution_intents",
            sql,
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS task_execution_intent_bindings",
            sql,
        )
        old_fk_drop = sql.index(
            "DROP FOREIGN KEY fk_task_run_execution_evidence"
        )
        self.assertGreater(
            old_fk_drop,
            sql.index("CREATE TABLE IF NOT EXISTS lottery_execution_intents"),
        )
        self.assertGreater(
            old_fk_drop,
            sql.index(
                "CREATE TABLE IF NOT EXISTS task_execution_intent_bindings"
            ),
        )
        replacement_guard = sql.index(
            "CREATE TEMPORARY TABLE "
            "dpms_0014_replacement_contract_guard"
        )
        self.assertGreater(old_fk_drop, replacement_guard)
        for token in (
            "@dpms_0014_ready_tables = 2",
            "@dpms_0014_ready_columns = 43",
            "@dpms_0014_ready_unique_indexes = 7",
            "@dpms_0014_ready_foreign_keys = 8",
            "@dpms_0014_ready_checks = 9",
            "chk_0014_replacement_tables_ready",
            "chk_0014_replacement_columns_ready",
            "chk_0014_replacement_unique_indexes_ready",
            "chk_0014_replacement_foreign_keys_ready",
            "chk_0014_replacement_checks_ready",
        ):
            with self.subTest(replacement_guard=token):
                self.assertIn(token, sql)
        for token in (
            "CREATE TEMPORARY TABLE dpms_0014_expected_columns",
            "LOWER(actual.COLUMN_TYPE) = expected.column_type",
            "actual.IS_NULLABLE = expected.is_nullable",
            "actual.COLUMN_DEFAULT <=> expected.column_default",
            "UPPER(actual.EXTRA) = expected.extra",
            "'raw_url',\n    'varchar(512)',",
            "'oauth_calibration_id',\n    'char(36)',\n    'YES',",
            "'created_at',\n    'timestamp',\n    'NO',\n"
            "    'CURRENT_TIMESTAMP',\n    'DEFAULT_GENERATED'",
            "@dpms_account_calibration_id_index_signature",
            "'1:calibration_id:0:FULL'",
            "candidate_index.INDEX_NAME = 'PRIMARY'",
        ):
            with self.subTest(column_signature=token):
                self.assertIn(token, sql)
        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0014_execution_intent_repair_binding.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_statements = split_statements(rollback_sql)
        self.assertGreaterEqual(len(rollback_statements), 30)
        self.assertTrue(all(statement.strip() for statement in rollback_statements))
        self.assertIn(
            "SIGNAL SQLSTATE '45000'",
            rollback_sql,
        )
        self.assertIn(
            "execution intent tables are not empty",
            rollback_sql,
        )
        self.assertIn(
            "LOCK TABLES",
            rollback_sql,
        )
        self.assertIn(
            "DROP TABLE task_execution_intent_bindings, "
            "lottery_execution_intents",
            rollback_sql,
        )
        self.assertIn(
            "DELETE FROM schema_migrations WHERE version = '0014'",
            rollback_sql,
        )
        self.assertGreater(
            rollback_sql.index(
                "DELETE FROM schema_migrations WHERE version = '0014'"
            ),
            rollback_sql.index("DROP PROCEDURE dpms_refuse_0014_rollback"),
        )
        self.assertLess(
            rollback_sql.index("execution intent tables are not empty"),
            rollback_sql.index(
                "ADD CONSTRAINT fk_task_run_execution_evidence"
            ),
        )
        self.assertLess(
            rollback_sql.index("ADD CONSTRAINT fk_task_run_execution_evidence"),
            rollback_sql.index(
                "DROP TABLE task_execution_intent_bindings, "
                "lottery_execution_intents"
            ),
        )

    def test_execution_intent_rollback_restores_only_the_full_legacy_binding(
        self,
    ):
        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0014_execution_intent_repair_binding.down.sql"
        ).read_text(encoding="utf-8")
        compact = " ".join(rollback.split())

        pairs = (
            ("id", "execution_evidence_id"),
            ("lottery_id", "lottery_id"),
            ("account_id", "account_id"),
            ("rule_snapshot_id", "rule_snapshot_id"),
            ("execution_path_id", "execution_path_id"),
            ("target_hash", "target_hash"),
            ("rule_hash", "rule_hash"),
            ("action_plan_hash", "action_plan_hash"),
            ("config_hash", "config_hash"),
        )
        for evidence_column, task_column in pairs:
            with self.subTest(column=evidence_column):
                self.assertIn(
                    f"evidence.{evidence_column} = task.{task_column}",
                    compact,
                )
        self.assertIn(
            "WHERE task.execution_evidence_id IS NOT NULL "
            "AND evidence.id IS NULL",
            compact,
        )
        self.assertIn(
            "FOREIGN KEY ( execution_evidence_id, lottery_id, account_id, "
            "rule_snapshot_id, execution_path_id, target_hash, rule_hash, "
            "action_plan_hash, config_hash ) "
            "REFERENCES execution_evidence_bindings ( id, lottery_id, "
            "account_id, rule_snapshot_id, execution_path_id, target_hash, "
            "rule_hash, action_plan_hash, config_hash )",
            compact,
        )

    def test_task_outbox_lane_index_migration_is_retry_safe(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0015", found)
        sql = Path(found["0015"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        self.assertEqual(len(split_statements(sql)), 6)
        self.assertIn(
            "idx_outbox_stream_status_id (stream_key, status, id)",
            compact,
        )
        self.assertIn("information_schema.statistics", compact)
        self.assertIn("is_visible", compact)
        self.assertIn("drop index idx_outbox_stream_status_id", compact)
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("outbox_events", "idx_outbox_stream_status_id")
            ],
            ("stream_key", "status", "id"),
        )
        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0015_task_outbox_stream_lane_index.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_statements = split_statements(
            rollback.read_text(encoding="utf-8")
        )
        self.assertEqual(len(rollback_statements), 5)
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0015'",
        )
        self.assertIn(
            "DELETE FROM schema_migrations WHERE version = '0015'",
            rollback.read_text(encoding="utf-8"),
        )

    def test_runtime_schema_declares_and_repairs_outbox_lane_index(self):
        runtime_schema_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "runtime_schema.py"
        ).read_text(encoding="utf-8").lower()
        compact = " ".join(runtime_schema_source.split())

        self.assertGreaterEqual(
            compact.count(
                "idx_outbox_stream_status_id (stream_key, status, id)"
            ),
            2,
        )
        self.assertIn(
            'index_exists( "outbox_events", '
            '"idx_outbox_stream_status_id", )',
            compact,
        )

    def test_account_calibration_recovery_indexes_are_retry_safe(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0020", found)
        sql = Path(found["0020"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        self.assertEqual(len(split_statements(sql)), 16)
        self.assertEqual(
            compact.count("alter table account_calibrations"),
            1,
        )
        for index_name, columns in (
            (
                "idx_adapter_probe_status",
                ("status", "created_at"),
            ),
            (
                "idx_account_calibration_status",
                ("status", "created_at"),
            ),
            (
                "idx_account_calibration_platform_queued",
                ("platform", "status", "created_at", "id"),
            ),
            (
                "idx_account_calibration_platform_running",
                (
                    "platform",
                    "status",
                    "started_at",
                    "created_at",
                    "id",
                ),
            ),
        ):
            with self.subTest(index=index_name):
                signature = ", ".join(columns)
                self.assertIn(
                    f"add index {index_name} ({signature}) visible",
                    compact,
                )
                self.assertIn(f"drop index {index_name}", compact)
                self.assertIn(
                    f"alter index {index_name} visible",
                    compact,
                )
                self.assertEqual(
                    PRODUCTION_REQUIRED_INDEXES[
                        (
                            "adapter_calibrations"
                            if index_name == "idx_adapter_probe_status"
                            else "account_calibrations",
                            index_name,
                        )
                    ],
                    columns,
                )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / (
                "0020_account_calibration_platform_recovery_indexes"
                ".down.sql"
            )
        )
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_statements = split_statements(rollback_sql)
        self.assertEqual(len(rollback_statements), 8)
        self.assertEqual(
            " ".join(rollback_sql.lower().split()).count(
                "alter table account_calibrations"
            ),
            1,
        )
        self.assertIn(
            "DROP INDEX idx_account_calibration_platform_queued",
            rollback_sql,
        )
        self.assertIn(
            "DROP INDEX idx_account_calibration_platform_running",
            rollback_sql,
        )
        self.assertNotIn(
            "DROP INDEX idx_account_calibration_status",
            rollback_sql,
        )
        self.assertNotIn(
            "DROP INDEX idx_adapter_probe_status",
            rollback_sql,
        )
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0020'",
        )

    def test_runtime_schema_declares_account_calibration_recovery_indexes(self):
        runtime_schema_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "runtime_schema.py"
        ).read_text(encoding="utf-8")
        compact = " ".join(
            runtime_schema_source.lower().replace("`", "").split()
        )
        self.assertIn(
            "idx_account_calibration_platform_queued "
            "(platform, status, created_at, id)",
            compact,
        )
        self.assertIn(
            "idx_account_calibration_platform_running "
            "(platform, status, started_at, created_at, id)",
            compact,
        )

    def test_risk_account_time_index_migration_is_retry_safe(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0021", found)
        sql = Path(found["0021"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        self.assertEqual(len(split_statements(sql)), 10)
        self.assertIn(
            "add index idx_risk_account_created_id "
            "(account_id, created_at, id) visible",
            compact,
        )
        self.assertIn(
            "drop index idx_risk_account_created_id",
            compact,
        )
        self.assertIn(
            "alter index idx_risk_account_created_id visible",
            compact,
        )
        self.assertIn(
            "drop index idx_risk_events_account_fk_rollback",
            compact,
        )
        self.assertIn("information_schema.statistics", compact)
        self.assertIn("is_visible", compact)
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("risk_events", "idx_risk_account_created_id")
            ],
            ("account_id", "created_at", "id"),
        )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0021_risk_events_account_created_index.down.sql"
        )
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_statements = split_statements(rollback_sql)
        self.assertEqual(len(rollback_statements), 7)
        self.assertIn(
            "DROP INDEX idx_risk_account_created_id",
            rollback_sql,
        )
        self.assertIn(
            "ADD INDEX idx_risk_events_account_fk_rollback (account_id)",
            rollback_sql,
        )
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0021'",
        )

    def test_bootstrap_declares_risk_index_and_runtime_does_not(
        self,
    ):
        test_path = Path(__file__).resolve()
        init_path = next(
            candidate
            for candidate in (
                test_path.parents[1] / "init.sql",
                test_path.parents[2] / "init.sql",
            )
            if candidate.is_file()
        )
        init_source = init_path.read_text(encoding="utf-8")
        runtime_schema_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "runtime_schema.py"
        ).read_text(encoding="utf-8")
        init_compact = " ".join(
            init_source.lower().replace("`", "").split()
        )
        runtime_schema_compact = " ".join(
            runtime_schema_source.lower().replace("`", "").split()
        )

        signature = (
            "idx_risk_account_created_id "
            "(account_id, created_at, id)"
        )
        self.assertIn(signature, init_compact)
        self.assertNotIn(
            "idx_risk_account_created_id",
            runtime_schema_compact,
        )

    def test_strategy_query_budget_indexes_are_retry_safe_and_verified(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0022", found)
        sql = Path(found["0022"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        expected = {
            ("accounts", "idx_account_strategy_candidate"): (
                "platform",
                "status",
                "deleted_at",
                "daily_task_count",
                "id",
            ),
            (
                "account_calibrations",
                "idx_account_calibration_account_platform_id",
            ): ("account_id", "platform", "id"),
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
        }
        for (table_name, index_name), columns in expected.items():
            with self.subTest(index=index_name):
                signature = ", ".join(columns)
                self.assertIn(
                    f"add index {index_name} ({signature}) visible",
                    compact,
                )
                self.assertNotIn(f"drop index {index_name}", compact)
                self.assertNotIn(f"alter index {index_name} visible", compact)
                self.assertEqual(
                    PRODUCTION_REQUIRED_INDEXES[(table_name, index_name)],
                    columns,
                )
        self.assertEqual(
            compact.count("drift_requires_manual_resolution"),
            len(expected),
        )
        self.assertIn(
            "drop index idx_account_calibrations_account_fk_rollback",
            compact,
        )
        self.assertIn(
            "drop index idx_task_runs_account_fk_rollback",
            compact,
        )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0022_strategy_query_budget_indexes.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_compact = " ".join(rollback_sql.lower().split())
        for _table_name, index_name in expected:
            self.assertIn(f"drop index {index_name}", rollback_compact)
        self.assertIn(
            "add index idx_account_calibrations_account_fk_rollback "
            "(account_id)",
            rollback_compact,
        )
        self.assertIn(
            "add index idx_task_runs_account_fk_rollback (account_id)",
            rollback_compact,
        )
        self.assertEqual(
            split_statements(rollback_sql)[-1],
            "DELETE FROM schema_migrations WHERE version = '0022'",
        )

    def test_active_risk_state_migration_is_versioned_and_verified(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0023", found)
        sql = Path(found["0023"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        statements = split_statements(sql)
        self.assertIn(
            "create table if not exists account_active_risk_states",
            compact,
        )
        self.assertIn(
            "row_number() over ( partition by account_id "
            "order by active_until desc, created_at desc, id desc )",
            compact,
        )
        self.assertIn("when 'action_window' then 4", compact)
        self.assertIn(
            "when 'sliding_window_exceeded' then 4",
            compact,
        )
        self.assertIn("else 24", compact)
        non_default_cooldowns = sorted(
            (reason, str(hours))
            for reason, hours in ACCOUNT_RISK_COOLDOWN_BY_REASON.items()
            if hours != ACCOUNT_RISK_COOLDOWN_HOURS
        )
        sql_non_default_cooldowns = sorted(
            re.findall(r"when '([^']+)' then (\d+)", compact)
        )
        self.assertEqual(
            sql_non_default_cooldowns,
            sorted(non_default_cooldowns * 2),
        )
        self.assertEqual(
            compact.count(f"else {ACCOUNT_RISK_COOLDOWN_HOURS}"),
            2,
        )
        self.assertIn(
            "create trigger trg_risk_events_active_state "
            "after insert on risk_events",
            compact,
        )
        trigger_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith(
                "CREATE TRIGGER trg_risk_events_active_state"
            )
        )
        backfill_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith(
                "INSERT INTO account_active_risk_states"
            )
        )
        self.assertLess(trigger_index, backfill_index)
        self.assertIn("on duplicate key update", compact)
        self.assertNotIn("values(", compact)
        self.assertIn(
            "from (select new.account_id as account_id",
            compact,
        )
        self.assertIn("as incoming on duplicate key update", compact)
        self.assertEqual(
            PRODUCTION_REQUIRED_PRIMARY_KEYS[
                "account_active_risk_states"
            ],
            ("account_id",),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                (
                    "account_active_risk_states",
                    "uk_account_active_risk_event",
                )
            ],
            ("risk_event_id",),
        )
        self.assertIn(
            "trg_risk_events_active_state",
            PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS,
        )

        trigger_statement = next(
            statement.split(" FOR EACH ROW ", 1)[1]
            for statement in statements
            if statement.startswith(
                "CREATE TRIGGER trg_risk_events_active_state"
            )
        )
        self.assertEqual(
            trigger_statement_checksum(trigger_statement),
            PRODUCTION_REQUIRED_TRIGGER_DEFINITIONS[
                "trg_risk_events_active_state"
            ][3],
        )
        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0023_account_active_risk_state.down.sql"
        )
        rollback_compact = " ".join(
            rollback.read_text(encoding="utf-8").lower().split()
        )
        self.assertIn(
            "drop trigger if exists trg_risk_events_active_state",
            rollback_compact,
        )
        self.assertIn(
            "drop table if exists account_active_risk_states",
            rollback_compact,
        )

    def test_bootstrap_declares_strategy_indexes_and_runtime_does_not(
        self,
    ):
        test_path = Path(__file__).resolve()
        init_path = next(
            candidate
            for candidate in (
                test_path.parents[1] / "init.sql",
                test_path.parents[2] / "init.sql",
            )
            if candidate.is_file()
        )
        init_compact = " ".join(
            init_path.read_text(encoding="utf-8")
            .lower()
            .replace("`", "")
            .split()
        )
        runtime_schema_compact = " ".join(
            (
                Path(__file__).resolve().parents[1]
                / "app"
                / "runtime_schema.py"
            )
            .read_text(encoding="utf-8")
            .lower()
            .replace("`", "")
            .split()
        )
        for index_name, columns in (
            (
                "idx_account_strategy_candidate",
                "platform, status, deleted_at, daily_task_count, id",
            ),
            (
                "idx_account_calibration_account_platform_id",
                "account_id, platform, id",
            ),
            (
                "idx_task_run_account_created_id",
                "account_id, created_at, id",
            ),
            (
                "idx_task_run_created_lottery_id",
                "created_at, lottery_id, id",
            ),
            (
                "idx_risk_created_account_id",
                "created_at, account_id, id",
            ),
            (
                "idx_lottery_extracted_platform_id",
                "extracted_at, platform, id",
            ),
        ):
            with self.subTest(index=index_name):
                signature = f"{index_name} ({columns})"
                self.assertIn(signature, init_compact)
                self.assertNotIn(index_name, runtime_schema_compact)
        self.assertIn(
            "idx_risk_account_created_id "
            "(account_id, created_at, id)",
            init_compact,
        )
        self.assertNotIn(
            "idx_risk_account_created_id",
            runtime_schema_compact,
        )

    def test_outbox_redis_delivery_epoch_migration_is_retry_safe(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0016", found)
        sql = Path(found["0016"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        self.assertEqual(len(split_statements(sql)), 17)
        self.assertIn(
            "redis_delivery_epoch varchar(128) null",
            compact,
        )
        self.assertIn("information_schema.columns", compact)
        self.assertIn("is_visible", compact)
        self.assertIn(
            "modify column redis_delivery_epoch varchar(128) null",
            compact,
        )
        self.assertIn(
            "add unique key uk_outbox_dedup (dedup_key)",
            compact,
        )
        self.assertIn(
            "add index idx_task_run_status (status, created_at)",
            compact,
        )
        self.assertIn(
            "redis_delivery_epoch",
            PRODUCTION_REQUIRED_COLUMNS["outbox_events"],
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMNS["runtime_settings"],
            {"setting_key", "setting_value", "updated_at"},
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_INDEXES[
                ("task_runs", "idx_task_run_status")
            ],
            ("status", "created_at"),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                ("outbox_events", "uk_outbox_dedup")
            ],
            ("dedup_key",),
        )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0016_outbox_redis_delivery_epoch.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_statements = split_statements(
            rollback.read_text(encoding="utf-8")
        )
        self.assertEqual(len(rollback_statements), 5)
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0016'",
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[
                ("outbox_events", "redis_delivery_epoch")
            ],
            ("varchar(128)", "YES", None),
        )
        self.assertIn(
            "DELETE FROM schema_migrations WHERE version = '0016'",
            rollback.read_text(encoding="utf-8"),
        )

    def test_runtime_schema_declares_and_repairs_outbox_delivery_epoch(self):
        runtime_schema_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "runtime_schema.py"
        ).read_text(encoding="utf-8").lower()
        compact = " ".join(runtime_schema_source.split())

        self.assertIn(
            "redis_delivery_epoch varchar(128) null",
            compact,
        )
        self.assertIn(
            'ensure_column( "outbox_events", '
            '"redis_delivery_epoch", "varchar(128) null", )',
            compact,
        )

    def test_rejected_outcome_migration_and_fail_closed_rollback(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0018", found)
        sql = Path(found["0018"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        statements = split_statements(sql)

        self.assertEqual(len(statements), 6)
        self.assertTrue(all(statement.strip() for statement in statements))
        self.assertIn(
            "'drop check chk_external_action_lifecycle_v2, '",
            compact,
        )
        self.assertEqual(
            compact.count(
                "add constraint chk_external_action_lifecycle_v2 check"
            ),
            1,
        )
        self.assertIn(
            "''retry'', ''limit'', ''skip'', ''captcha'', ''risk'', "
            "''auth'', ''rejected''",
            compact,
        )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0018_external_action_rejected_outcome.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_statements = split_statements(rollback_sql)

        self.assertEqual(len(rollback_statements), 11)
        self.assertIn(
            "CREATE TEMPORARY TABLE dpms_0018_rollback_guard",
            rollback_sql,
        )
        self.assertIn("chk_0018_no_rejected_outcomes", rollback_sql)
        self.assertIn("CHECK (has_rejected = 0) ENFORCED", rollback_sql)
        self.assertIn("SELECT EXISTS (", rollback_sql)
        self.assertIn("LIMIT 1", rollback_sql)
        self.assertIn("WHERE outcome = 'rejected'", rollback_sql)
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0018'",
        )
        self.assertGreater(
            rollback_sql.index(
                "DELETE FROM schema_migrations WHERE version = '0018'"
            ),
            rollback_sql.index(
                "DROP TEMPORARY TABLE dpms_0018_rollback_guard"
            ),
        )

    def test_execution_intent_current_head_migration_contract(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))

        self.assertIn("0019", found)
        sql = Path(found["0019"]).read_text(encoding="utf-8")
        compact = " ".join(sql.lower().split())
        statements = split_statements(sql)

        self.assertGreaterEqual(len(statements), 30)
        self.assertTrue(all(statement.strip() for statement in statements))
        self.assertIn(
            "create table if not exists lottery_execution_intent_heads",
            compact,
        )
        self.assertIn(
            "primary key (lottery_id)",
            compact,
        )
        self.assertIn(
            "unique key uk_lottery_execution_intent_head_identity "
            "( current_intent_id, lottery_id )",
            compact,
        )
        self.assertIn(
            "foreign key (current_intent_id, lottery_id) references "
            "lottery_execution_intents(intent_id, lottery_id)",
            compact,
        )
        self.assertIn(
            "check (generation > 0) enforced",
            compact,
        )

        contract_guard = compact.index(
            "create temporary table dpms_0019_head_contract_guard"
        )
        backfill = compact.index(
            "insert into lottery_execution_intent_heads "
            "( lottery_id, current_intent_id, generation )"
        )
        drop_legacy_unique = compact.index(
            "drop index uk_lottery_execution_intent_lottery"
        )
        self.assertLess(contract_guard, backfill)
        self.assertLess(backfill, drop_legacy_unique)
        self.assertIn(
            "left join lottery_execution_intent_heads as heads",
            compact,
        )
        self.assertIn(
            "having count(*) <> 1",
            compact,
        )
        self.assertIn(
            "chk_0019_all_heads_reference_root",
            compact,
        )
        self.assertIn(
            "roots.intent_id is null or heads.generation < 1",
            compact,
        )
        self.assertIn(
            "@dpms_0019_legacy_lottery_index_signature is null",
            compact,
        )
        self.assertNotIn(
            "delete from lottery_execution_intents",
            compact,
        )
        self.assertNotIn(
            "update lottery_execution_intents",
            compact,
        )

        rollback = (
            MIGRATIONS_DIR
            / "rollback"
            / "0019_execution_intent_current_head.down.sql"
        )
        self.assertTrue(rollback.is_file())
        rollback_sql = rollback.read_text(encoding="utf-8")
        rollback_compact = " ".join(rollback_sql.lower().split())
        rollback_statements = split_statements(rollback_sql)

        self.assertTrue(all(statement.strip() for statement in rollback_statements))
        self.assertIn(
            "create temporary table dpms_0019_rollback_root_guard",
            rollback_compact,
        )
        self.assertIn(
            "having count(*) > 1",
            rollback_compact,
        )
        self.assertIn(
            "check (has_multiple_roots = 0) enforced",
            rollback_compact,
        )
        self.assertIn(
            "add unique key uk_lottery_execution_intent_lottery "
            "(lottery_id)",
            rollback_compact,
        )
        self.assertNotIn(
            "delete from lottery_execution_intents",
            rollback_compact,
        )
        self.assertEqual(
            rollback_statements[-1],
            "DELETE FROM schema_migrations WHERE version = '0019'",
        )
        self.assertLess(
            rollback_compact.index(
                "add unique key uk_lottery_execution_intent_lottery"
            ),
            rollback_compact.index(
                "drop table if exists lottery_execution_intent_heads"
            ),
        )

    def test_execution_intent_current_head_verifier_contract(self):
        table = "lottery_execution_intent_heads"

        self.assertIn(table, PRODUCTION_REQUIRED_TABLES)
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMNS[table],
            {
                "lottery_id",
                "current_intent_id",
                "generation",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_PRIMARY_KEYS[table],
            ("lottery_id",),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_UNIQUE_INDEXES[
                (table, "uk_lottery_execution_intent_head_identity")
            ],
            ("current_intent_id", "lottery_id"),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_FOREIGN_KEYS[
                (table, "fk_lottery_execution_intent_head_root")
            ],
            (
                ("current_intent_id", "lottery_id"),
                "lottery_execution_intents",
                ("intent_id", "lottery_id"),
            ),
        )
        self.assertIn(
            (
                table,
                "chk_lottery_execution_intent_head_generation",
            ),
            PRODUCTION_REQUIRED_CHECK_CONSTRAINTS,
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[
                (table, "generation")
            ],
            ("bigint unsigned", "NO", None),
        )
        self.assertIn(
            (
                "lottery_execution_intents",
                "uk_lottery_execution_intent_lottery",
            ),
            PRODUCTION_FORBIDDEN_INDEXES,
        )
        self.assertNotIn(
            (
                "lottery_execution_intents",
                "uk_lottery_execution_intent_lottery",
            ),
            PRODUCTION_REQUIRED_UNIQUE_INDEXES,
        )


class RealRunV2ContractTests(unittest.TestCase):
    def setUp(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        self.sql = Path(found["0010"]).read_text(encoding="utf-8").lower()
        self.upgrade_sql = Path(found["0011"]).read_text(encoding="utf-8").lower()
        self.intent_sql = Path(found["0014"]).read_text(
            encoding="utf-8"
        ).lower()
        self.epoch_sql = Path(found["0016"]).read_text(
            encoding="utf-8"
        ).lower()
        self.intent_head_sql = Path(found["0019"]).read_text(
            encoding="utf-8"
        ).lower()
        self.risk_state_sql = Path(found["0023"]).read_text(
            encoding="utf-8"
        ).lower()
        self.profile_cleanup_sql = Path(found["0024"]).read_text(
            encoding="utf-8"
        ).lower()
        self.profile_lease_sql = Path(found["0025"]).read_text(
            encoding="utf-8"
        ).lower()
        self.target_pursuit_sql = Path(found["0026"]).read_text(
            encoding="utf-8"
        ).lower()
        self.compact_sql = " ".join(self.sql.split())
        self.token_sql = "".join(self.sql.split())
        self.all_token_sql = "".join(
            (
                self.sql
                + self.upgrade_sql
                + self.intent_sql
                + self.epoch_sql
                + self.intent_head_sql
                + self.risk_state_sql
                + self.profile_cleanup_sql
                + self.profile_lease_sql
                + self.target_pursuit_sql
            ).split()
        )

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
            "account_calibrations": {"calibration_id"},
            "lottery_execution_intents": {
                "contract_version",
                "intent_id",
                "intent_hash",
                "lottery_id",
                "source_task_id",
                "source_account_id",
                "full_action_plan",
                "full_action_plan_hash",
                "full_required_actions",
                "full_required_actions_hash",
                "rule_snapshot_id",
                "rule_hash",
                "execution_path_id",
                "target_hash",
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
                "execution_evidence_id",
                "execution_evidence_kind",
                "exact_execution_evidence_id",
                "oauth_calibration_id",
                "config_hash",
                "execution_revision",
                "account_lease_id",
                "account_lease_generation",
                "binding_hash",
            },
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
            (
                "lottery_execution_intents",
                "chk_lottery_execution_intent_contract",
            ),
            (
                "lottery_execution_intent_heads",
                "chk_lottery_execution_intent_head_generation",
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
                "chk_task_execution_intent_revision",
            ),
        }:
            self.assertIn(constraint, PRODUCTION_REQUIRED_CHECK_CONSTRAINTS)

    def test_repair_binding_separates_full_evidence_and_subset_hashes(self):
        compact = " ".join(self.intent_sql.split())

        self.assertIn(
            "bound_action_plan_hash char(64) not null",
            compact,
        )
        self.assertIn(
            "evidence_action_plan_hash char(64) not null",
            compact,
        )
        exact_evidence_fk = PRODUCTION_REQUIRED_FOREIGN_KEYS[
            (
                "task_execution_intent_bindings",
                "fk_task_execution_intent_exact_evidence",
            )
        ]
        oauth_evidence_fk = PRODUCTION_REQUIRED_FOREIGN_KEYS[
            (
                "task_execution_intent_bindings",
                "fk_task_execution_intent_oauth_calibration",
            )
        ]
        self.assertEqual(
            exact_evidence_fk,
            (
                ("exact_execution_evidence_id",),
                "execution_evidence_bindings",
                ("id",),
            ),
        )
        self.assertEqual(
            oauth_evidence_fk,
            (
                ("oauth_calibration_id",),
                "account_calibrations",
                ("calibration_id",),
            ),
        )
        self.assertNotIn(
            ("task_runs", "fk_task_run_execution_evidence"),
            PRODUCTION_REQUIRED_FOREIGN_KEYS,
        )
        self.assertIn(
            "exact_execution_evidence_id = execution_evidence_id",
            compact,
        )
        self.assertIn(
            "oauth_calibration_id = execution_evidence_id",
            compact,
        )

    def test_execution_intent_migration_intentionally_does_not_backfill(self):
        self.assertNotIn(
            "insert into lottery_execution_intents select",
            self.intent_sql,
        )
        self.assertIn("no legacy backfill", self.intent_sql)

    def test_execution_intent_primary_keys_and_exact_columns_are_verified(self):
        self.assertEqual(
            PRODUCTION_REQUIRED_PRIMARY_KEYS["lottery_execution_intents"],
            ("intent_id",),
        )
        self.assertEqual(
            PRODUCTION_REQUIRED_PRIMARY_KEYS[
                "task_execution_intent_bindings"
            ],
            ("task_id",),
        )
        for key, expected in {
            ("lottery_execution_intents", "intent_hash"):
                ("char(64)", "NO", None),
            ("lottery_execution_intents", "created_at"):
                ("timestamp", "NO", "CURRENT_TIMESTAMP"),
            ("task_execution_intent_bindings", "binding_hash"):
                ("char(64)", "NO", None),
            ("task_execution_intent_bindings", "requested_actions"):
                ("json", "NO", None),
        }.items():
            with self.subTest(column=key):
                self.assertEqual(
                    PRODUCTION_REQUIRED_COLUMN_DEFINITIONS[key],
                    expected,
                )

    def test_runner_serializes_migrations_on_one_named_lock(self):
        self.assertEqual(MIGRATION_LOCK_NAME, "dpms:schema_migrations")
        self.assertGreater(MIGRATION_LOCK_TIMEOUT_SECONDS, 0)


class RealRunV2UpgradeTests(unittest.TestCase):
    def setUp(self):
        found = dict(discover_migrations(MIGRATIONS_DIR))
        self.sql = Path(found["0011"]).read_text(encoding="utf-8").lower()
        self.rejected_outcome_sql = Path(found["0018"]).read_text(
            encoding="utf-8"
        ).lower()
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

    def test_0018_lifecycle_clause_matches_verifier_contract(self):
        constraint = "chk_external_action_lifecycle_v2"
        match = re.search(
            rf"add constraint {re.escape(constraint)} check "
            rf"\((.*?)\n\s*\) enforced'",
            self.rejected_outcome_sql,
            re.DOTALL,
        )

        self.assertIsNotNone(match)
        migration_clause = match.group(1).replace("''", "'")
        self.assertEqual(
            normalise_check_clause(migration_clause),
            normalise_check_clause(
                PRODUCTION_REQUIRED_EXACT_CHECK_CLAUSES[
                    ("external_action_intents", constraint)
                ]
            ),
        )
        self.assertIn("'rejected'", migration_clause)

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
