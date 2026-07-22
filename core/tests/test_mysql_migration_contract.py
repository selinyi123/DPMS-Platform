"""Opt-in MySQL 8 integration checks for the real-run safety schema.

The default unit suite skips this module's database work.  CI or a local
container can enable it with ``DPMS_MYSQL_INTEGRATION=1`` and a disposable
``DATABASE_URL``.  The tests never call an external platform.
"""

import os
import unittest


MYSQL_INTEGRATION = os.getenv("DPMS_MYSQL_INTEGRATION") == "1"

if MYSQL_INTEGRATION:
    from app.db import database
    from app.migrations_runner import verify_production_schema


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


if __name__ == "__main__":
    unittest.main()
