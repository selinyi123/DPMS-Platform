import base64
import json
import os
import unittest
from unittest.mock import patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"0" * 32).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services import xiaohongshu_target_pursuit_requests as requests


class FakeRedis:
    def __init__(self, result=None):
        self.result = result
        self.added = []
        self.deleted = []

    async def xadd(self, stream, fields):
        self.added.append((stream, fields))
        if callable(self.result):
            self.result = self.result(fields)
        return "1-0"

    async def get(self, key):
        del key
        if self.result is None:
            return None
        value, self.result = self.result, None
        return json.dumps(value)

    async def delete(self, key):
        self.deleted.append(key)
        return 1


class XiaohongshuTargetPursuitRequestTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_dispatch_uses_bounded_secret_free_envelope(self):
        redis = FakeRedis(
            result=lambda fields: {
                "request_id": fields["request_id"],
                "status": "completed",
                "candidates": [
                    {
                        "raw_url": (
                            "https://www.xiaohongshu.com/explore/"
                            "64f1a2b3c4d5e6f7a8b9c0d1"
                        )
                    }
                ],
            }
        )
        with patch.object(requests, "redis", redis):
            result = (
                await requests.dispatch_xiaohongshu_target_pursuit_scan(
                    "keyword",
                    "抽奖",
                    max_candidates=3,
                )
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(1, len(redis.added))
        _stream, fields = redis.added[0]
        self.assertEqual("3", fields["max_candidates"])
        self.assertEqual(
            {
                "protocol_version",
                "request_id",
                "source_type",
                "source_value",
                "requested_at_ms",
                "max_candidates",
            },
            set(fields),
        )
        self.assertFalse(
            any(
                marker in json.dumps(fields).casefold()
                for marker in ("cookie", "token", "credential")
            )
        )

    async def test_offline_import_never_enters_browser_lane(self):
        redis = FakeRedis()
        with (
            patch.object(requests, "redis", redis),
            self.assertRaisesRegex(
                requests.XiaohongshuTargetPursuitDispatchError,
                "browser_source_unsupported",
            ),
        ):
            await requests.dispatch_xiaohongshu_target_pursuit_scan(
                "offline_search_result",
                "export.json",
            )
        self.assertEqual([], redis.added)

    async def test_worker_failure_code_is_propagated_without_detail(self):
        redis = FakeRedis(
            result=lambda fields: {
                "request_id": fields["request_id"],
                "status": "failed",
                "error_code": (
                    "xiaohongshu_target_pursuit_ready_account_required"
                ),
                "candidates": [],
                "detail": "must not be included in the exception",
            }
        )
        with (
            patch.object(requests, "redis", redis),
            self.assertRaisesRegex(
                requests.XiaohongshuTargetPursuitDispatchError,
                "ready_account_required",
            ) as caught,
        ):
            await requests.dispatch_xiaohongshu_target_pursuit_scan(
                "author_profile",
                (
                    "https://www.xiaohongshu.com/user/profile/"
                    "64f1a2b3c4d5e6f7a8b9c0d1"
                ),
            )
        self.assertNotIn("must not be included", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
