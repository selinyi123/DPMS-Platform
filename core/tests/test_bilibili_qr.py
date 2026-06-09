import unittest

from app.services.bilibili_qr import cookies_from_login_url
from app.utils.cookies import parse_cookie_payload, validate_required_cookies


class BilibiliQrTests(unittest.TestCase):
    def test_extracts_required_cookies_from_confirmed_login_url(self):
        cookies = cookies_from_login_url(
            "https://www.bilibili.com/?"
            "DedeUserID=12345&"
            "DedeUserID__ckMd5=abcdef&"
            "SESSDATA=session%2Cvalue&"
            "bili_jct=csrf-token"
        )

        values = {cookie["name"]: cookie["value"] for cookie in cookies}
        self.assertEqual("12345", values["DedeUserID"])
        self.assertEqual("session,value", values["SESSDATA"])
        self.assertEqual("csrf-token", values["bili_jct"])

    def test_rejects_confirmed_url_without_required_cookies(self):
        with self.assertRaisesRegex(ValueError, "bilibili_qr_missing_cookies"):
            cookies_from_login_url("https://www.bilibili.com/?bili_jct=csrf-token")

    def test_cookie_import_requires_platform_login_cookies(self):
        cookies = parse_cookie_payload("bilibili", "bili_jct=csrf-token")
        with self.assertRaisesRegex(ValueError, "SESSDATA"):
            validate_required_cookies(cookies, ["SESSDATA", "DedeUserID"])

    def test_cookie_import_accepts_required_platform_cookies(self):
        cookies = parse_cookie_payload(
            "bilibili",
            "SESSDATA=session-value; DedeUserID=12345; bili_jct=csrf-token",
        )
        validate_required_cookies(cookies, ["SESSDATA", "DedeUserID"])


if __name__ == "__main__":
    unittest.main()
