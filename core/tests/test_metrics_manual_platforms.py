import unittest

from app.api.metrics import build_production_checks


class ManualPlatformReadinessTests(unittest.TestCase):
    def test_manual_shadow_platforms_are_not_counted_as_missing_dry_run(self):
        summary = {
            "platforms_total": 4,
            "dry_run_supported": 2,
            "dry_run_ready": 2,
            "real_run_ready": 0,
        }
        checks = {
            item["code"]: item
            for item in build_production_checks([], summary)
        }
        self.assertTrue(checks["all_platforms_dry_ready"]["passed"])
        self.assertIn("2/2", checks["all_platforms_dry_ready"]["detail"])

    def test_supported_dry_run_platform_still_fails_when_not_ready(self):
        summary = {
            "platforms_total": 4,
            "dry_run_supported": 2,
            "dry_run_ready": 1,
            "real_run_ready": 0,
        }
        checks = {
            item["code"]: item
            for item in build_production_checks([], summary)
        }
        self.assertFalse(checks["all_platforms_dry_ready"]["passed"])


if __name__ == "__main__":
    unittest.main()
