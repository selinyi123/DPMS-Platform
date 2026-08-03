import base64
import os
import unittest

from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.lotteries import validated_probe_navigation_url  # noqa: E402


class AdapterProbeNavigationTargetTests(unittest.TestCase):
    def test_keeps_validated_raw_https_url_for_browser_navigation(self):
        raw_url = "https://www.bilibili.com/opus/1220306071196794898"
        self.assertEqual(validated_probe_navigation_url(raw_url), raw_url)

    def test_rejects_internal_canonical_identifier(self):
        with self.assertRaises(HTTPException) as context:
            validated_probe_navigation_url("canonical://bilibili/dynamic/opus_1220306071196794898")
        self.assertEqual(context.exception.status_code, 400)

    def test_rejects_insecure_http_target(self):
        with self.assertRaises(HTTPException) as context:
            validated_probe_navigation_url("http://www.bilibili.com/opus/1220306071196794898")
        self.assertEqual(context.exception.status_code, 400)

    def test_rejects_https_value_without_a_host(self):
        with self.assertRaises(HTTPException) as context:
            validated_probe_navigation_url("https:opus/1220306071196794898")
        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
