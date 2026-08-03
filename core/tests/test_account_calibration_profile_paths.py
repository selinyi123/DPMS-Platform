import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import accounts


class AccountCalibrationProfilePathTests(
    unittest.IsolatedAsyncioTestCase
):
    async def _serve(self, screenshot_path: str):
        calibration_id = str(uuid.uuid4())
        row = {
            "calibration_id": calibration_id,
            "platform": "weibo",
            "screenshot_path": screenshot_path.format(
                calibration_id=calibration_id
            ),
        }
        response = object()
        database = AsyncMock()
        database.fetch_one.return_value = row
        with (
            patch.object(accounts, "database", database),
            patch.object(
                accounts,
                "get_platform",
                return_value={"id": "weibo"},
            ),
            patch.object(
                accounts,
                "_profile_png_response",
                return_value=response,
            ) as serve,
        ):
            result = await (
                accounts.get_account_calibration_screenshot(
                    calibration_id
                )
            )
        return result, serve, calibration_id

    async def test_current_path_is_platform_scoped(self):
        result, serve, calibration_id = await self._serve(
            "/profiles/weibo/account-calibrations/"
            "{calibration_id}.png"
        )
        self.assertIsNotNone(result)
        serve.assert_called_once_with(
            Path(
                "/profiles/weibo/account-calibrations/"
                f"{calibration_id}.png"
            ),
            allowed_root=Path(
                "/profiles/weibo/account-calibrations"
            ),
        )

    async def test_exact_legacy_path_remains_readable(self):
        _result, serve, calibration_id = await self._serve(
            "/profiles/account-calibrations/"
            "{calibration_id}.png"
        )
        serve.assert_called_once_with(
            Path(
                "/profiles/account-calibrations/"
                f"{calibration_id}.png"
            ),
            allowed_root=Path(
                "/profiles/account-calibrations"
            ),
        )

    async def test_other_platform_path_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            await self._serve(
                "/profiles/douyin/account-calibrations/"
                "{calibration_id}.png"
            )
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
