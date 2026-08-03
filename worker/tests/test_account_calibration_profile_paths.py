import unittest
from pathlib import Path

from app.account_calibrator import calibration_screenshot_path


class AccountCalibrationProfilePathTests(unittest.TestCase):
    def test_path_is_platform_and_uuid_bound(self):
        calibration_id = "60dc9ca4-a25f-4ec0-89c1-29e3702a22a6"
        self.assertEqual(
            calibration_screenshot_path("weibo", calibration_id),
            Path(
                "/profiles/weibo/account-calibrations/"
                f"{calibration_id}.png"
            ),
        )
        for platform, value in (
            ("../weibo", calibration_id),
            ("weibo/account-calibrations", calibration_id),
            ("weibo", "../calibration"),
            ("weibo", calibration_id.upper()),
        ):
            with self.subTest(
                platform=platform,
                calibration_id=value,
            ), self.assertRaises(ValueError):
                calibration_screenshot_path(platform, value)


if __name__ == "__main__":
    unittest.main()
