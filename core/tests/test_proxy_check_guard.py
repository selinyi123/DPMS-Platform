import base64
import os
import unittest

# Provide a valid key/secret before importing the API module, so importing
# proxies.py (which pulls in app.config / app.security) never fails on env.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.proxies import (  # noqa: E402
    ALLOWED_CHECK_DOMAINS,
    _is_allowed_check_host,
    validate_check_url,
)


class AllowedCheckDomainsTests(unittest.TestCase):
    def test_platform_domains_are_present(self):
        self.assertEqual(
            ALLOWED_CHECK_DOMAINS,
            {"bilibili.com", "weibo.com", "douyin.com", "xiaohongshu.com"},
        )


class IsAllowedCheckHostTests(unittest.TestCase):
    def test_default_target_is_allowed(self):
        self.assertTrue(_is_allowed_check_host("www.bilibili.com"))

    def test_bare_platform_domain_is_allowed(self):
        self.assertTrue(_is_allowed_check_host("weibo.com"))

    def test_platform_subdomain_is_allowed(self):
        self.assertTrue(_is_allowed_check_host("space.bilibili.com"))

    def test_case_insensitive(self):
        self.assertTrue(_is_allowed_check_host("WWW.BILIBILI.COM"))

    def test_arbitrary_internal_host_is_rejected(self):
        self.assertFalse(_is_allowed_check_host("169.254.169.254"))
        self.assertFalse(_is_allowed_check_host("localhost"))
        self.assertFalse(_is_allowed_check_host("internal.svc.cluster.local"))

    def test_lookalike_domain_is_rejected(self):
        # "evil-bilibili.com" must not match "bilibili.com" via naive suffix
        # matching - it neither equals the allowed domain nor ends with
        # ".bilibili.com".
        self.assertFalse(_is_allowed_check_host("evil-bilibili.com"))
        self.assertFalse(_is_allowed_check_host("bilibili.com.evil.net"))

    def test_empty_host_is_rejected(self):
        self.assertFalse(_is_allowed_check_host(None))
        self.assertFalse(_is_allowed_check_host(""))


class ValidateCheckUrlTests(unittest.TestCase):
    def test_valid_url_is_returned(self):
        self.assertEqual(validate_check_url("https://www.bilibili.com"), "https://www.bilibili.com")

    def test_non_http_scheme_is_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            validate_check_url("file:///etc/passwd")

    def test_missing_host_is_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            validate_check_url("https://")


if __name__ == "__main__":
    unittest.main()
