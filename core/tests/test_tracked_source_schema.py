import unittest

from pydantic import ValidationError

from app.models.schemas import (
    LotteryCreate,
    LotteryTargetImport,
    TrackedSourceCreate,
)


class TrackedSourceSchemaTests(unittest.TestCase):
    def test_source_value_matches_mysql_storage_boundary(self):
        accepted = TrackedSourceCreate(source_value="x" * 256)
        self.assertEqual(len(accepted.source_value), 256)

        with self.assertRaises(ValidationError):
            TrackedSourceCreate(source_value="x" * 257)

    def test_lottery_ingress_matches_mysql_string_boundaries(self):
        LotteryCreate(
            source_type="x" * 32,
            source_id="x" * 64,
            raw_url="x" * 512,
        )
        LotteryTargetImport(
            source_type="x" * 32,
            source_id="x" * 64,
            content="https://example.test/target",
        )

        invalid_cases = (
            lambda: LotteryCreate(source_type="x" * 33, raw_url="x" * 8),
            lambda: LotteryCreate(source_id="x" * 65, raw_url="x" * 8),
            lambda: LotteryCreate(raw_url="x" * 513),
            lambda: LotteryTargetImport(source_type="x" * 33, content="x"),
            lambda: LotteryTargetImport(source_id="x" * 65, content="x"),
        )
        for build in invalid_cases:
            with self.subTest(build=build):
                with self.assertRaises(ValidationError):
                    build()


if __name__ == "__main__":
    unittest.main()
