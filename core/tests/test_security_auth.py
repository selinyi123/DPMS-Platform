import base64
import os
import types
import unittest

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.config import settings  # noqa: E402
from app.security import actor_from_request  # noqa: E402


def request(method="GET", headers=None, query=None):
    return types.SimpleNamespace(
        method=method,
        headers=headers or {},
        query_params=query or {},
    )


class ActorFromRequestTests(unittest.TestCase):
    def setUp(self):
        self._admin_token = settings.admin_token
        settings.admin_token = "test-admin-token"

    def tearDown(self):
        settings.admin_token = self._admin_token

    def test_header_token_authenticates(self):
        actor = actor_from_request(request(headers={"x-admin-token": "test-admin-token"}))
        self.assertEqual(actor["role"], "owner")
        self.assertEqual(actor["auth_type"], "x-admin-token")

    def test_query_token_authenticates_read_only_get(self):
        actor = actor_from_request(request(query={"admin_token": "test-admin-token"}))
        self.assertEqual(actor["role"], "owner")
        self.assertEqual(actor["auth_type"], "admin_token_query")

    def test_query_token_rejected_for_write_methods(self):
        actor = actor_from_request(request(method="POST", query={"admin_token": "test-admin-token"}))
        self.assertIsNone(actor)


if __name__ == "__main__":
    unittest.main()
