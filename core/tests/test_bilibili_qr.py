import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.bilibili_qr import (
    cookies_from_login_url,
    cookies_from_poll_response,
    poll_bilibili_qr,
    provider_qr_controls_expiry,
)
from app.utils.cookies import (
    parse_cookie_payload,
    validate_api_cookie_name_uniqueness,
    validate_required_cookies,
)


class BilibiliQrTests(unittest.TestCase):
    def test_official_provider_qr_controls_expiry(self):
        self.assertTrue(
            provider_qr_controls_expiry("bilibili", "provider-key")
        )
        self.assertFalse(provider_qr_controls_expiry("bilibili", None))
        self.assertFalse(
            provider_qr_controls_expiry("xiaohongshu", "provider-key")
        )

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

    def test_success_response_accepts_set_cookie_credentials(self):
        cookies = cookies_from_poll_response(
            "https://www.bilibili.com/?bili_jct=csrf-token",
            [
                SimpleNamespace(
                    name="SESSDATA",
                    value="session%2Cvalue",
                    domain=".bilibili.com",
                    path="/",
                    secure=True,
                    expires=None,
                    _rest={"HttpOnly": None},
                ),
                SimpleNamespace(
                    name="DedeUserID",
                    value="12345",
                    domain=".bilibili.com",
                    path="/",
                    secure=True,
                    expires=None,
                    _rest={},
                ),
            ],
        )

        values = {cookie["name"]: cookie["value"] for cookie in cookies}
        self.assertEqual("session%2Cvalue", values["SESSDATA"])
        self.assertEqual("12345", values["DedeUserID"])
        self.assertEqual("csrf-token", values["bili_jct"])

    def test_set_cookie_value_overrides_decoded_url_without_duplicates(self):
        cookies = cookies_from_poll_response(
            "https://www.bilibili.com/?SESSDATA=session%252Cvalue&DedeUserID=12345",
            [
                SimpleNamespace(
                    name="SESSDATA",
                    value="session%2Cvalue",
                    domain=".bilibili.com",
                    path="/",
                    secure=True,
                    expires=None,
                    _rest={},
                )
            ],
        )

        self.assertEqual(
            1,
            sum(cookie["name"] == "SESSDATA" for cookie in cookies),
        )
        self.assertEqual(
            "session%2Cvalue",
            next(
                cookie["value"]
                for cookie in cookies
                if cookie["name"] == "SESSDATA"
            ),
        )

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

    def test_cookie_import_accepts_direct_json_map(self):
        cookies = parse_cookie_payload(
            "bilibili",
            '{"SESSDATA":"session-value","DedeUserID":"12345"}',
        )
        validate_required_cookies(cookies, ["SESSDATA", "DedeUserID"])

    def test_cookie_import_accepts_netscape_export(self):
        cookies = parse_cookie_payload(
            "bilibili",
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1790000000\t"
            "SESSDATA\tsession-value\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t1790000000\t"
            "DedeUserID\t12345",
        )
        validate_required_cookies(cookies, ["SESSDATA", "DedeUserID"])

    def test_cookie_import_accepts_browser_table_rows(self):
        cookies = parse_cookie_payload(
            "bilibili",
            "SESSDATA\tsession-value\t.bilibili.com\t/\tSession\t"
            "HttpOnly\tSecure\n"
            "DedeUserID\t12345\t.bilibili.com\t/\tSession\tSecure",
        )
        validate_required_cookies(cookies, ["SESSDATA", "DedeUserID"])

    def test_cookie_import_rejects_duplicate_required_cookie_names(self):
        cookies = parse_cookie_payload(
            "bilibili",
            """[
              {"name":"SESSDATA","value":"session-value"},
              {"name":"DedeUserID","value":"12345"},
              {"name":"DedeUserID","value":"67890"}
            ]""",
        )
        with self.assertRaisesRegex(
            ValueError,
            "Duplicate required Cookie names.*DedeUserID",
        ):
            validate_required_cookies(
                cookies,
                ["SESSDATA", "DedeUserID"],
            )

    def test_api_cookie_contract_rejects_duplicate_csrf_but_allows_optional(
        self,
    ):
        critical = parse_cookie_payload(
            "bilibili",
            """[
              {"name":"bili_jct","value":"csrf-a"},
              {"name":"bili_jct","value":"csrf-b"}
            ]""",
        )
        with self.assertRaisesRegex(
            ValueError,
            "Duplicate Bilibili API Cookie names.*bili_jct",
        ):
            validate_api_cookie_name_uniqueness(
                "bilibili",
                critical,
            )

        optional = parse_cookie_payload(
            "bilibili",
            """[
              {"name":"buvid3","value":"host-a"},
              {"name":"buvid3","value":"host-b"}
            ]""",
        )
        validate_api_cookie_name_uniqueness("bilibili", optional)


class BilibiliQrPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_poll_uses_response_set_cookie_credentials(self):
        response = SimpleNamespace(
            cookies=SimpleNamespace(
                jar=[
                    SimpleNamespace(
                        name="SESSDATA",
                        value="session%2Cvalue",
                        domain=".bilibili.com",
                        path="/",
                        secure=True,
                        expires=None,
                        _rest={"HttpOnly": None},
                    ),
                    SimpleNamespace(
                        name="DedeUserID",
                        value="12345",
                        domain=".bilibili.com",
                        path="/",
                        secure=True,
                        expires=None,
                        _rest={},
                    ),
                ]
            ),
            raise_for_status=lambda: None,
            json=lambda: {
                "code": 0,
                "message": "0",
                "data": {
                    "code": 0,
                    "message": "",
                    "refresh_token": "not-persisted-by-this-contract",
                    "timestamp": 1,
                    "url": "https://www.bilibili.com/?bili_jct=csrf-token",
                },
            },
        )

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def get(self, *_args, **_kwargs):
                return response

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await poll_bilibili_qr("provider-key-not-logged")

        self.assertEqual("confirmed", result.status)
        values = {cookie["name"]: cookie["value"] for cookie in result.cookies}
        self.assertEqual("session%2Cvalue", values["SESSDATA"])
        self.assertEqual("12345", values["DedeUserID"])


if __name__ == "__main__":
    unittest.main()
