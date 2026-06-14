import base64
import os
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api.accounts import DEFAULT_FINGERPRINT_POOL  # noqa: E402


class FingerprintPoolTests(unittest.TestCase):
    def test_pool_is_not_a_single_profile(self):
        # The whole point of P2-1: do not funnel every account onto one shared
        # fingerprint.
        self.assertGreaterEqual(len(DEFAULT_FINGERPRINT_POOL), 3)

    def test_user_agents_are_unique(self):
        # fingerprints is unique on (platform, user_agent); colliding UAs in the
        # pool would silently collapse profiles back toward one row.
        uas = [profile["ua"] for profile in DEFAULT_FINGERPRINT_POOL]
        self.assertEqual(len(uas), len(set(uas)))

    def test_profiles_have_realistic_shape(self):
        for profile in DEFAULT_FINGERPRINT_POOL:
            self.assertIn("ua", profile)
            self.assertTrue(profile["ua"].startswith("Mozilla/5.0"))
            self.assertGreater(profile["vw"], 0)
            self.assertGreater(profile["vh"], 0)
            # Ordinary, non-anomalous desktop dimensions.
            self.assertGreaterEqual(profile["vw"], 1000)
            self.assertGreaterEqual(profile["vh"], 600)


if __name__ == "__main__":
    unittest.main()
