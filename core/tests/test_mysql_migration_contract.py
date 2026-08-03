"""Opt-in MySQL 8 integration checks for the real-run safety schema.

The default unit suite skips this module's database work.  CI or a local
container can enable it with ``DPMS_MYSQL_INTEGRATION=1`` and a disposable
``DATABASE_URL``.  The tests never call an external platform.
"""

import asyncio
import os
import unittest
import uuid


MYSQL_INTEGRATION = os.getenv("DPMS_MYSQL_INTEGRATION") == "1"

if MYSQL_INTEGRATION:
    from app.db import database
    from app.migrations_runner import (
        MIGRATIONS_DIR,
        split_statements,
        verify_production_schema,
    )


@unittest.skipUnless(MYSQL_INTEGRATION, "requires a disposable MySQL 8 database")
class MySQLMigrationContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not database.is_connected:
            await database.connect()

    async def asyncTearDown(self):
        if database.is_connected:
            await database.disconnect()

    async def _check_clause(self, connection, table: str, constraint: str) -> str:
        row = await connection.fetch_one(
            """SELECT cc.CHECK_CLAUSE, tc.ENFORCED
               FROM information_schema.TABLE_CONSTRAINTS tc
               JOIN information_schema.CHECK_CONSTRAINTS cc
                 ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
               WHERE tc.CONSTRAINT_SCHEMA = DATABASE()
                 AND tc.TABLE_NAME = :table
                 AND tc.CONSTRAINT_NAME = :constraint
                 AND tc.CONSTRAINT_TYPE = 'CHECK'""",
            {"table": table, "constraint": constraint},
        )
        self.assertIsNotNone(row, f"missing {table}.{constraint}")
        self.assertEqual(str(row["ENFORCED"]).upper(), "YES")
        # information_schema escapes SQL literal delimiters as ``\'``.  They
        # must be restored before embedding the server-owned clause in the
        # disposable temporary table below.
        return str(row["CHECK_CLAUSE"]).replace("\\'", "'")

    async def test_live_schema_verifier_accepts_exact_contract(self):
        await verify_production_schema()

    async def test_execution_intent_current_head_contract_is_complete(self):
        async with database.connection() as connection:
            obsolete_index = await connection.fetch_one(
                """SELECT COUNT(*) AS count
                   FROM information_schema.STATISTICS
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME = 'lottery_execution_intents'
                     AND INDEX_NAME =
                         'uk_lottery_execution_intent_lottery'"""
            )
            self.assertEqual(int(obsolete_index["count"]), 0)

            missing_heads = await connection.fetch_one(
                """SELECT COUNT(*) AS count
                   FROM (
                     SELECT roots.lottery_id
                     FROM lottery_execution_intents AS roots
                     LEFT JOIN lottery_execution_intent_heads AS heads
                       ON heads.lottery_id = roots.lottery_id
                     WHERE heads.lottery_id IS NULL
                     GROUP BY roots.lottery_id
                   ) AS missing"""
            )
            self.assertEqual(int(missing_heads["count"]), 0)

            invalid_heads = await connection.fetch_one(
                """SELECT COUNT(*) AS count
                   FROM lottery_execution_intent_heads AS heads
                   LEFT JOIN lottery_execution_intents AS roots
                     ON roots.intent_id = heads.current_intent_id
                    AND roots.lottery_id = heads.lottery_id
                   WHERE roots.intent_id IS NULL
                      OR heads.generation < 1"""
            )
            self.assertEqual(int(invalid_heads["count"]), 0)

    async def test_execution_intent_current_head_migration_is_idempotent(self):
        migration_sql = (
            MIGRATIONS_DIR / "0019_execution_intent_current_head.sql"
        ).read_text(encoding="utf-8")

        async with database.connection() as connection:
            before = await connection.fetch_all(
                """SELECT lottery_id, current_intent_id, generation,
                          created_at, updated_at
                   FROM lottery_execution_intent_heads
                   ORDER BY lottery_id"""
            )
            columns = (
                "lottery_id",
                "current_intent_id",
                "generation",
                "created_at",
                "updated_at",
            )
            before_rows = [
                tuple(row[column] for column in columns)
                for row in before
            ]

            for statement in split_statements(migration_sql):
                await connection.execute(statement)

            after = await connection.fetch_all(
                """SELECT lottery_id, current_intent_id, generation,
                          created_at, updated_at
                   FROM lottery_execution_intent_heads
                   ORDER BY lottery_id"""
            )
            after_rows = [
                tuple(row[column] for column in columns)
                for row in after
            ]

        self.assertEqual(after_rows, before_rows)

    async def test_account_calibration_recovery_indexes_are_exact_and_idempotent(
        self,
    ):
        migration_sql = (
            MIGRATIONS_DIR
            / "0020_account_calibration_platform_recovery_indexes.sql"
        ).read_text(encoding="utf-8")
        expected = {
            (
                "adapter_calibrations",
                "idx_adapter_probe_status",
            ): ("status", "created_at"),
            (
                "account_calibrations",
                "idx_account_calibration_status",
            ): ("status", "created_at"),
            (
                "account_calibrations",
                "idx_account_calibration_platform_queued",
            ): ("platform", "status", "created_at", "id"),
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
        }

        async with database.connection() as connection:
            for _ in range(2):
                for statement in split_statements(migration_sql):
                    await connection.execute(statement)

            rows = await connection.fetch_all(
                """SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX,
                          NON_UNIQUE, SUB_PART, IS_VISIBLE
                     FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND (
                        (TABLE_NAME = 'adapter_calibrations'
                         AND INDEX_NAME = 'idx_adapter_probe_status')
                        OR
                        (TABLE_NAME = 'account_calibrations'
                         AND INDEX_NAME IN (
                           'idx_account_calibration_status',
                           'idx_account_calibration_platform_queued',
                           'idx_account_calibration_platform_running'
                         ))
                      )
                    ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"""
            )

        actual: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            key = (str(row["TABLE_NAME"]), str(row["INDEX_NAME"]))
            actual.setdefault(key, []).append(str(row["COLUMN_NAME"]))
            self.assertEqual(int(row["NON_UNIQUE"]), 1)
            self.assertIsNone(row["SUB_PART"])
            self.assertEqual(str(row["IS_VISIBLE"]).upper(), "YES")

        self.assertEqual(
            {key: tuple(columns) for key, columns in actual.items()},
            expected,
        )

    async def test_risk_account_time_index_is_exact_and_idempotent(self):
        migration_sql = (
            MIGRATIONS_DIR
            / "0021_risk_events_account_created_index.sql"
        ).read_text(encoding="utf-8")

        async with database.connection() as connection:
            for _ in range(2):
                for statement in split_statements(migration_sql):
                    await connection.execute(statement)

            rows = await connection.fetch_all(
                """SELECT COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE,
                          SUB_PART, IS_VISIBLE
                     FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'risk_events'
                      AND INDEX_NAME = 'idx_risk_account_created_id'
                    ORDER BY SEQ_IN_INDEX"""
            )
            rollback_fallback = await connection.fetch_one(
                """SELECT COUNT(*) AS count
                     FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'risk_events'
                      AND INDEX_NAME =
                          'idx_risk_events_account_fk_rollback'"""
            )

        self.assertEqual(
            tuple(str(row["COLUMN_NAME"]) for row in rows),
            ("account_id", "created_at", "id"),
        )
        for row in rows:
            self.assertEqual(int(row["NON_UNIQUE"]), 1)
            self.assertIsNone(row["SUB_PART"])
            self.assertEqual(str(row["IS_VISIBLE"]).upper(), "YES")
        self.assertEqual(int(rollback_fallback["count"]), 0)

    async def test_strategy_query_budget_indexes_are_exact_and_idempotent(
        self,
    ):
        migration_sql = (
            MIGRATIONS_DIR / "0022_strategy_query_budget_indexes.sql"
        ).read_text(encoding="utf-8")
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

        async with database.connection() as connection:
            for _ in range(2):
                for statement in split_statements(migration_sql):
                    await connection.execute(statement)
            rows = await connection.fetch_all(
                """SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX,
                          NON_UNIQUE, SUB_PART, IS_VISIBLE
                     FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND (
                        (TABLE_NAME = 'accounts'
                         AND INDEX_NAME = 'idx_account_strategy_candidate')
                        OR
                        (TABLE_NAME = 'account_calibrations'
                         AND INDEX_NAME =
                           'idx_account_calibration_account_platform_id')
                        OR
                        (TABLE_NAME = 'task_runs'
                         AND INDEX_NAME IN (
                           'idx_task_run_account_created_id',
                           'idx_task_run_created_lottery_id'
                         ))
                        OR
                        (TABLE_NAME = 'risk_events'
                         AND INDEX_NAME = 'idx_risk_created_account_id')
                        OR
                        (TABLE_NAME = 'lotteries'
                         AND INDEX_NAME =
                           'idx_lottery_extracted_platform_id')
                      )
                    ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"""
            )
            rollback_fallbacks = await connection.fetch_one(
                """SELECT COUNT(*) AS count
                     FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND (
                        (TABLE_NAME = 'account_calibrations'
                         AND INDEX_NAME =
                           'idx_account_calibrations_account_fk_rollback')
                        OR
                        (TABLE_NAME = 'task_runs'
                         AND INDEX_NAME =
                           'idx_task_runs_account_fk_rollback')
                      )"""
            )

        actual: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            key = (str(row["TABLE_NAME"]), str(row["INDEX_NAME"]))
            actual.setdefault(key, []).append(str(row["COLUMN_NAME"]))
            self.assertEqual(int(row["NON_UNIQUE"]), 1)
            self.assertIsNone(row["SUB_PART"])
            self.assertEqual(str(row["IS_VISIBLE"]).upper(), "YES")
        self.assertEqual(
            {key: tuple(columns) for key, columns in actual.items()},
            expected,
        )
        self.assertEqual(int(rollback_fallbacks["count"]), 0)

    async def test_strategy_query_budget_migration_refuses_index_drift(
        self,
    ):
        migration_sql = (
            MIGRATIONS_DIR / "0022_strategy_query_budget_indexes.sql"
        ).read_text(encoding="utf-8")
        async with database.connection() as connection:
            await connection.execute(
                """ALTER TABLE accounts
                   DROP INDEX idx_account_strategy_candidate,
                   ADD INDEX idx_account_strategy_candidate (platform, id)"""
            )
            try:
                with self.assertRaises(Exception):
                    for statement in split_statements(migration_sql):
                        await connection.execute(statement)
                rows = await connection.fetch_all(
                    """SELECT COLUMN_NAME
                         FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = 'accounts'
                          AND INDEX_NAME =
                              'idx_account_strategy_candidate'
                        ORDER BY SEQ_IN_INDEX"""
                )
                self.assertEqual(
                    tuple(str(row["COLUMN_NAME"]) for row in rows),
                    ("platform", "id"),
                )
            finally:
                await connection.execute(
                    """ALTER TABLE accounts
                       DROP INDEX idx_account_strategy_candidate,
                       ADD INDEX idx_account_strategy_candidate
                         (platform, status, deleted_at,
                          daily_task_count, id) VISIBLE"""
                )

    async def test_active_risk_state_trigger_selects_longest_cooldown(
        self,
    ):
        from app.services.real_run_readiness import recent_account_risk

        unique_suffix = uuid.uuid4().hex
        async with database.transaction(force_rollback=True):
            fingerprint_id = await database.execute(
                """INSERT INTO fingerprints (user_agent, platform)
                   VALUES (:user_agent, 'bilibili')""",
                {
                    "user_agent": (
                        f"dpms-active-risk-contract-{unique_suffix}"
                    )
                },
            )
            account_id = await database.execute(
                """INSERT INTO accounts (
                       platform, fingerprint_id, encrypted_credential, status
                     ) VALUES (
                       'bilibili', :fingerprint_id, :credential, 'ready'
                     )""",
                {
                    "fingerprint_id": fingerprint_id,
                    "credential": b"integration-test-only",
                },
            )
            expired_short_id = await database.execute(
                """INSERT INTO risk_events (
                       account_id, event_type, detail, created_at
                     ) VALUES (
                       :account_id, 'cooling',
                       JSON_OBJECT('reason', 'action_window'),
                       DATE_SUB(NOW(), INTERVAL 5 HOUR)
                     )""",
                {"account_id": account_id},
            )
            active_hard_id = await database.execute(
                """INSERT INTO risk_events (
                       account_id, event_type, detail, created_at
                     ) VALUES (
                       :account_id, 'login_required',
                       JSON_OBJECT('reason', 'redirected_to_login'),
                       DATE_SUB(NOW(), INTERVAL 20 HOUR)
                     )""",
                {"account_id": account_id},
            )
            state = await database.fetch_one(
                """SELECT risk_event_id, active_until > NOW() AS active
                     FROM account_active_risk_states
                    WHERE account_id = :account_id""",
                {"account_id": account_id},
            )
            risk = await recent_account_risk(account_id)

        self.assertNotEqual(expired_short_id, active_hard_id)
        self.assertEqual(int(state["risk_event_id"]), active_hard_id)
        self.assertEqual(int(state["active"]), 1)
        self.assertTrue(risk["has_recent_risk"])
        self.assertEqual(
            int(risk["latest_event"]["id"]),
            active_hard_id,
        )

    async def test_active_risk_deadline_is_timezone_invariant(self):
        unique_suffix = uuid.uuid4().hex
        async with database.connection() as connection:
            session = await connection.fetch_one(
                "SELECT @@session.time_zone AS time_zone"
            )
            self.assertEqual(str(session["time_zone"]), "+00:00")
            column = await connection.fetch_one(
                """SELECT DATA_TYPE
                     FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'account_active_risk_states'
                      AND COLUMN_NAME = 'active_until'"""
            )
            self.assertEqual(str(column["DATA_TYPE"]).lower(), "timestamp")

            async with connection.transaction(force_rollback=True):
                fingerprint_id = await connection.execute(
                    """INSERT INTO fingerprints (user_agent, platform)
                       VALUES (:user_agent, 'bilibili')""",
                    {
                        "user_agent": (
                            f"dpms-risk-timezone-contract-{unique_suffix}"
                        )
                    },
                )
                account_id = await connection.execute(
                    """INSERT INTO accounts (
                           platform, fingerprint_id,
                           encrypted_credential, status
                         ) VALUES (
                           'bilibili', :fingerprint_id,
                           :credential, 'ready'
                         )""",
                    {
                        "fingerprint_id": fingerprint_id,
                        "credential": b"integration-test-only",
                    },
                )
                await connection.execute(
                    """INSERT INTO risk_events (
                           account_id, event_type, detail, created_at
                         ) VALUES (
                           :account_id, 'login_required',
                           JSON_OBJECT(
                             'reason', 'redirected_to_login'
                           ),
                           DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)
                         )""",
                    {"account_id": account_id},
                )
                observations = []
                try:
                    for time_zone in ("+08:00", "-05:00"):
                        await connection.execute(
                            f"SET time_zone = '{time_zone}'"
                        )
                        row = await connection.fetch_one(
                            """SELECT active_until > NOW() AS active
                                 FROM account_active_risk_states
                                WHERE account_id = :account_id""",
                            {"account_id": account_id},
                        )
                        observations.append(int(row["active"]))
                finally:
                    await connection.execute(
                        "SET time_zone = '+00:00'"
                    )

        self.assertEqual(observations, [1, 1])

    async def test_final_readiness_snapshot_uses_one_pool_connection(self):
        import databases

        from app.services import real_run_readiness

        unique_suffix = uuid.uuid4().hex
        fingerprint_id = await database.execute(
            """INSERT INTO fingerprints (user_agent, platform)
               VALUES (:user_agent, 'bilibili')""",
            {
                "user_agent": (
                    f"dpms-final-snapshot-contract-{unique_suffix}"
                )
            },
        )
        account_id = None
        limited_database = None
        original_database = real_run_readiness.database
        try:
            account_id = await database.execute(
                """INSERT INTO accounts (
                       platform, fingerprint_id,
                       encrypted_credential, status
                     ) VALUES (
                       'bilibili', :fingerprint_id,
                       :credential, 'ready'
                     )""",
                {
                    "fingerprint_id": fingerprint_id,
                    "credential": b"integration-test-only",
                },
            )
            await database.execute(
                """INSERT INTO risk_events (
                       account_id, event_type, detail, created_at
                     ) VALUES (
                       :account_id, 'login_required',
                       JSON_OBJECT('reason', 'redirected_to_login'),
                       UTC_TIMESTAMP()
                     )""",
                {"account_id": account_id},
            )

            limited_database = databases.Database(
                os.environ["DATABASE_URL"],
                min_size=1,
                max_size=1,
                init_command="SET time_zone = '+00:00'",
            )
            await limited_database.connect()
            real_run_readiness.database = limited_database
            db_now, accounts, risks = await asyncio.wait_for(
                real_run_readiness
                ._load_final_account_mutable_state_snapshot({account_id}),
                timeout=2.0,
            )

            self.assertIsNotNone(db_now)
            self.assertEqual(set(accounts), {account_id})
            self.assertTrue(risks[account_id]["has_recent_risk"])
        finally:
            real_run_readiness.database = original_database
            if limited_database is not None and limited_database.is_connected:
                await limited_database.disconnect()
            if account_id is not None:
                await database.execute(
                    """DELETE FROM account_active_risk_states
                       WHERE account_id = :account_id""",
                    {"account_id": account_id},
                )
                await database.execute(
                    "DELETE FROM risk_events WHERE account_id = :account_id",
                    {"account_id": account_id},
                )
                await database.execute(
                    "DELETE FROM accounts WHERE id = :account_id",
                    {"account_id": account_id},
                )
            await database.execute(
                "DELETE FROM fingerprints WHERE id = :fingerprint_id",
                {"fingerprint_id": fingerprint_id},
            )

    async def test_cancelled_mysql_query_releases_pool_capacity(self):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                database.fetch_one("SELECT SLEEP(0.2) AS slept"),
                timeout=0.01,
            )

        row = await asyncio.wait_for(
            database.fetch_one("SELECT 1 AS healthy"),
            timeout=1.0,
        )
        self.assertEqual(int(row["healthy"]), 1)

    async def test_readiness_prefilter_per_lottery_union_is_valid_mysql(self):
        from app.services.real_run_readiness import (
            load_account_scoped_readiness_candidate_prefilter,
        )

        result = (
            await load_account_scoped_readiness_candidate_prefilter(
                [
                    {"id": 9_000_000_001, "platform": "bilibili"},
                    {"id": 9_000_000_002, "platform": "bilibili"},
                ]
            )
        )

        self.assertEqual(
            result.account_ids_for(9_000_000_001),
            frozenset(),
        )
        self.assertEqual(
            result.account_ids_for(9_000_000_002),
            frozenset(),
        )
        self.assertEqual(result.failed_platforms, frozenset())

    async def test_account_scoped_risk_query_preserves_older_active_signal(self):
        from app.services.real_run_readiness import (
            _fetch_account_scoped_active_risks,
            _sql_in_values,
            current_db_time,
        )

        unique_suffix = uuid.uuid4().hex
        async with database.transaction(force_rollback=True):
            fingerprint_id = await database.execute(
                """INSERT INTO fingerprints (user_agent, platform)
                   VALUES (:user_agent, 'bilibili')""",
                {"user_agent": f"dpms-mysql-risk-contract-{unique_suffix}"},
            )
            account_id = await database.execute(
                """INSERT INTO accounts (
                       platform, fingerprint_id, encrypted_credential, status
                     ) VALUES (
                       'bilibili', :fingerprint_id, :credential, 'ready'
                     )""",
                {
                    "fingerprint_id": fingerprint_id,
                    "credential": b"integration-test-only",
                },
            )
            await database.execute(
                """INSERT INTO risk_events (
                       account_id, event_type, detail, created_at
                     ) VALUES (
                       :account_id, 'hard_signal',
                       JSON_OBJECT('reason', 'page_risk_signal'),
                       DATE_SUB(NOW(), INTERVAL 12 HOUR)
                     )""",
                {"account_id": account_id},
            )
            await database.execute(
                """INSERT INTO risk_events (
                       account_id, event_type, detail, created_at
                     ) VALUES (
                       :account_id, 'short_signal',
                       JSON_OBJECT('reason', 'action_window'),
                       DATE_SUB(NOW(), INTERVAL 5 HOUR)
                     )""",
                {"account_id": account_id},
            )

            account_clause, account_values = _sql_in_values(
                "mysql_risk_account",
                [int(account_id)],
            )
            rows = await _fetch_account_scoped_active_risks(
                account_clause=account_clause,
                account_values=account_values,
                now=await current_db_time(required=True),
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(int(rows[0]["account_id"]), int(account_id))
            self.assertEqual(str(rows[0]["event_type"]), "hard_signal")

    async def test_terminal_intent_with_null_outcome_is_rejected(self):
        async with database.connection() as connection:
            clause = await self._check_clause(
                connection,
                "external_action_intents",
                "chk_external_action_lifecycle_v2",
            )
            await connection.execute("DROP TEMPORARY TABLE IF EXISTS dpms_lifecycle_check")
            await connection.execute(
                f"""CREATE TEMPORARY TABLE dpms_lifecycle_check (
                       status VARCHAR(32) NOT NULL,
                       attempt_no INT NOT NULL,
                       started_at DATETIME NULL,
                       completed_at DATETIME NULL,
                       outcome VARCHAR(32) NULL,
                       reconciliation_note TEXT NULL,
                       CONSTRAINT chk_lifecycle_copy CHECK ({clause}) ENFORCED
                     )"""
            )

            for status in ("succeeded", "failed", "unknown"):
                with self.subTest(status=status):
                    with self.assertRaises(Exception):
                        await connection.execute(
                            """INSERT INTO dpms_lifecycle_check (
                                   status, attempt_no, started_at, completed_at,
                                   outcome, reconciliation_note
                                 ) VALUES (
                                   :status, 1, NOW(), NOW(), NULL, 'manual review'
                                 )""",
                            {"status": status},
                        )

            for status, outcome, note in (
                ("succeeded", "ok", None),
                ("failed", "retry", None),
                ("failed", "rejected", None),
                ("unknown", "unknown", "manual review"),
            ):
                await connection.execute(
                    """INSERT INTO dpms_lifecycle_check (
                           status, attempt_no, started_at, completed_at,
                           outcome, reconciliation_note
                         ) VALUES (
                           :status, 1, NOW(), NOW(), :outcome, :note
                    )""",
                    {"status": status, "outcome": outcome, "note": note},
                )

            with self.assertRaises(Exception):
                await connection.execute(
                    """INSERT INTO dpms_lifecycle_check (
                           status, attempt_no, started_at, completed_at,
                           outcome, reconciliation_note
                         ) VALUES (
                           'succeeded', 1, NOW(), NOW(), 'rejected', NULL
                         )"""
                )

    async def test_verified_observation_requires_lower_hex_and_nonblank_kind(self):
        async with database.connection() as connection:
            clause = await self._check_clause(
                connection,
                "execution_evidence_bindings",
                "chk_execution_evidence_observation_hashes_v2",
            )
            await connection.execute("DROP TEMPORARY TABLE IF EXISTS dpms_observation_check")
            await connection.execute(
                f"""CREATE TEMPORARY TABLE dpms_observation_check (
                       status VARCHAR(32) NOT NULL,
                       probe_observation_kind VARCHAR(64) NULL,
                       probe_observation_hash CHAR(64) NULL,
                       shadow_observation_kind VARCHAR(64) NULL,
                       shadow_observation_hash CHAR(64) NULL,
                       CONSTRAINT chk_observation_copy CHECK ({clause}) ENFORCED
                     )"""
            )
            valid_hash = "a" * 64

            with self.assertRaises(Exception):
                await connection.execute(
                    """INSERT INTO dpms_observation_check VALUES
                         ('verified', 'probe', :uppercase_hash, 'shadow', :valid_hash)""",
                    {"uppercase_hash": "A" * 64, "valid_hash": valid_hash},
                )

            with self.assertRaises(Exception):
                await connection.execute(
                    """INSERT INTO dpms_observation_check VALUES
                         ('verified', '   ', :valid_hash, 'shadow', :valid_hash)""",
                    {"valid_hash": valid_hash},
                )

            await connection.execute(
                """INSERT INTO dpms_observation_check VALUES
                     ('verified', 'probe', :valid_hash, 'shadow', :valid_hash)""",
                {"valid_hash": valid_hash},
            )

    async def test_zz_recovery_index_rollbacks_reapply_cleanly(self):
        rollback_names = (
            "0023_account_active_risk_state.down.sql",
            "0022_strategy_query_budget_indexes.down.sql",
            "0021_risk_events_account_created_index.down.sql",
            "0020_account_calibration_platform_recovery_indexes.down.sql",
        )
        async with database.connection() as connection:
            for rollback_name in rollback_names:
                rollback_sql = (
                    MIGRATIONS_DIR / "rollback" / rollback_name
                ).read_text(encoding="utf-8")
                for statement in split_statements(rollback_sql):
                    await connection.execute(statement)

        from app.migrations_runner import run_migrations

        self.assertEqual(
            await run_migrations(),
            ["0020", "0021", "0022", "0023"],
        )
        await verify_production_schema()
        rollback_fallback = await database.fetch_one(
            """SELECT COUNT(*) AS count
                 FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND (
                    (TABLE_NAME = 'risk_events'
                     AND INDEX_NAME =
                       'idx_risk_events_account_fk_rollback')
                    OR
                    (TABLE_NAME = 'account_calibrations'
                     AND INDEX_NAME =
                       'idx_account_calibrations_account_fk_rollback')
                    OR
                    (TABLE_NAME = 'task_runs'
                     AND INDEX_NAME =
                       'idx_task_runs_account_fk_rollback')
                  )"""
        )
        self.assertEqual(int(rollback_fallback["count"]), 0)


if __name__ == "__main__":
    unittest.main()
