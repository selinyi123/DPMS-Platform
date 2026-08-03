import unittest
from unittest.mock import AsyncMock, patch

from app.db import GuardedDatabase, _is_schema_write, execute_affected_rows


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _AffectedRowsDatabase:
    def __init__(self, affected):
        self.affected = affected
        self.queries = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, values=None):
        self.queries.append((str(query), values))
        # Mirror databases' MySQL UPDATE behaviour: this is lastrowid, not
        # affected rows, and is commonly zero even when the UPDATE matched.
        return 0

    async def fetch_one(self, query, values=None):
        self.queries.append((str(query), values))
        return {"affected": self.affected}


class DatabaseWriteGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_only_ddl_is_classified_as_schema_write(self):
        self.assertTrue(_is_schema_write("CREATE TABLE example (id INT)"))
        self.assertTrue(_is_schema_write(" ALTER TABLE example ADD value INT"))
        self.assertTrue(_is_schema_write("/* maintenance */\nDROP TABLE example"))
        self.assertTrue(_is_schema_write("-- maintenance\nTRUNCATE TABLE example"))
        self.assertTrue(_is_schema_write("/*!50000 RENAME TABLE old TO new */"))
        self.assertTrue(_is_schema_write("/*!50000 CREATE*/ TABLE example (id INT)"))
        for statement in (
            "INSERT INTO runtime_settings (setting_key, setting_value) VALUES ('x', 'y')",
            "INSERT INTO circuit_breakers (scope, status) VALUES ('global', 'open')",
            "INSERT INTO policy_versions (policy_key, version) VALUES ('p', 1)",
        ):
            with self.subTest(statement=statement):
                self.assertFalse(_is_schema_write(statement))

    async def test_production_allows_runtime_safety_mutations_but_skips_ddl(self):
        guarded = GuardedDatabase.__new__(GuardedDatabase)
        guarded._inner = AsyncMock()
        guarded._inner.execute.return_value = 1

        with patch("app.db.production_mode", return_value=True):
            result = await guarded.execute(
                "INSERT INTO circuit_breakers (scope, status) VALUES ('global', 'open')"
            )
            skipped = await guarded.execute("CREATE TABLE forbidden (id INT)")

        self.assertEqual(result, 1)
        self.assertIsNone(skipped)
        guarded._inner.execute.assert_awaited_once()

    async def test_conditional_update_uses_mysql_row_count_not_execute_return(self):
        fake = _AffectedRowsDatabase(1)

        affected = await execute_affected_rows(
            "UPDATE accounts SET status = 'warming' WHERE id = :id",
            {"id": 7},
            db=fake,
        )

        self.assertEqual(affected, 1)
        self.assertIn("ROW_COUNT()", fake.queries[-1][0])


if __name__ == "__main__":
    unittest.main()
