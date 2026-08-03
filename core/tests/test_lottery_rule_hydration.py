import base64
import os
import unittest
from unittest import mock

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("DATABASE_URL", "mysql+aiomysql://u:p@localhost:3306/lottery")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.services.lottery_rule_hydration import (  # noqa: E402
    combine_trusted_rule_text,
    hydrate_lottery_rule,
    target_identity_from_lottery,
)


DYNAMIC_ID = "1221467928554110976"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeResponse(self.payload)


class LotteryRuleHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_hydration_uses_canonical_dynamic_and_ready_cookie(self):
        payload = {
            "code": 0,
            "data": {
                "item": {
                    "id_str": DYNAMIC_ID,
                    "type": "DYNAMIC_TYPE_DRAW",
                    "modules": [
                        {
                            "module_type": "MODULE_TYPE_TITLE",
                            "module_title": {
                                "text": "七月的暑期抽奖给大家安排上！"
                            },
                        },
                        {
                            "module_type": "MODULE_TYPE_AUTHOR",
                            "module_author": {
                                "mid": 15664766,
                                "name": "旅客君LookUplus",
                                "jump_url": "//space.bilibili.com/15664766",
                            },
                        },
                        {
                            "module_type": "MODULE_TYPE_CONTENT",
                            "module_content": {
                                "paragraphs": [
                                    {
                                        "text": {
                                            "nodes": [
                                                {
                                                    "type": "TEXT_NODE_TYPE_WORD",
                                                    "word": {
                                                        "words": "抽奖要求：关注 + 评论 + 转发，并评论 "
                                                    },
                                                },
                                                {
                                                    "type": "TEXT_NODE_TYPE_RICH",
                                                    "rich": {
                                                        "text": "@旅客君LookUplus ",
                                                        "orig_text": "@旅客君LookUplus ",
                                                        "rid": "15664766",
                                                    },
                                                },
                                                {
                                                    "type": "TEXT_NODE_TYPE_WORD",
                                                    "word": {
                                                        "words": "\u200b，2026 年 8 月 4 日开奖"
                                                    },
                                                },
                                            ]
                                        }
                                    }
                                ]
                            },
                        },
                    ],
                }
            },
        }
        lottery = {
            "id": 3,
            "platform": "bilibili",
            "raw_url": "https://b23.tv/d92lTnG",
            "canonical_url": f"canonical://bilibili/dynamic/opus_{DYNAMIC_ID}",
        }
        fake_client = _FakeClient(payload)
        with (
            mock.patch(
                "app.services.discovery.load_bilibili_discovery_cookie_header",
                new=mock.AsyncMock(return_value="SESSDATA=opaque"),
            ) as cookie_loader,
            mock.patch("httpx.AsyncClient", return_value=fake_client),
        ):
            result = await hydrate_lottery_rule(lottery)

        cookie_loader.assert_awaited_once()
        self.assertIn("/opus/detail", fake_client.calls[0][0][0])
        self.assertEqual("dynamic", result["target_kind"])
        self.assertTrue(
            result["rule_text"].startswith("七月的暑期抽奖给大家安排上！")
        )
        self.assertIn("关注 + 评论 + 转发", result["rule_text"])
        self.assertIn("@旅客君LookUplus", result["rule_text"])
        self.assertIn("8 月 4 日开奖", result["rule_text"])
        self.assertNotIn("\u200b", result["rule_text"])
        self.assertEqual("15664766", result["target_identity"]["uid"])
        self.assertEqual(
            "旅客君LookUplus",
            result["target_identity"]["display_name"],
        )
        self.assertTrue(result["target_identity"]["verified"])
        self.assertFalse(result["rule_snapshot"]["expanded_body"]["present"])
        self.assertFalse(result["rule_snapshot"]["pinned_comment"]["trusted"])

    async def test_bilibili_hydration_does_not_make_anonymous_request(self):
        lottery = {
            "id": 3,
            "platform": "bilibili",
            "raw_url": "https://b23.tv/d92lTnG",
            "canonical_url": f"canonical://bilibili/dynamic/{DYNAMIC_ID}",
        }
        with (
            mock.patch(
                "app.services.discovery.load_bilibili_discovery_cookie_header",
                new=mock.AsyncMock(side_effect=RuntimeError("no ready account")),
            ),
            mock.patch("httpx.AsyncClient") as client,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "bilibili_rule_hydration_ready_account_required",
            ):
                await hydrate_lottery_rule(lottery)
        client.assert_not_called()

    async def test_xiaohongshu_manual_target_reuses_matching_candidate_evidence(self):
        canonical_url = (
            "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1"
        )
        candidate = {
            "classification": {
                "observed_at": "2026-08-01T08:00:00+00:00",
                "author": {
                    "stable_id": "5ff0e6410000000001008400",
                    "display_name": "作者昵称",
                    "profile_url": (
                        "https://www.xiaohongshu.com/user/profile/"
                        "5ff0e6410000000001008400"
                    ),
                    "verified": True,
                },
                "content_snapshots": {
                    "body": {
                        "text": "正文规则",
                        "trusted": True,
                        "observed_at": "2026-08-01T08:00:00+00:00",
                    },
                    "expanded_body": {
                        "text": "展开后的完整条件",
                        "trusted": True,
                        "observed_at": "2026-08-01T08:00:00+00:00",
                    },
                    "pinned_comment": {
                        "text": "作者置顶补充",
                        "trusted": True,
                        "observed_at": "2026-08-01T08:00:00+00:00",
                    },
                },
                "review_reason_codes": [],
            }
        }
        with mock.patch(
            "app.services.lottery_rule_hydration.database.fetch_one",
            new=mock.AsyncMock(return_value=candidate),
        ) as fetch_one:
            result = await hydrate_lottery_rule(
                {
                    "id": 9,
                    "platform": "xiaohongshu",
                    "raw_url": (
                        "https://www.xiaohongshu.com/explore/"
                        "64f1a2b3c4d5e6f7a8b9c0d1"
                    ),
                    "canonical_url": canonical_url,
                    "source_type": "manual_upload",
                }
            )

        self.assertEqual(
            "正文规则\n\n展开后的完整条件\n\n作者置顶补充",
            result["rule_text"],
        )
        self.assertTrue(result["target_identity"]["verified"])
        self.assertEqual(
            canonical_url,
            fetch_one.await_args.args[1]["canonical_url"],
        )

    def test_trusted_snapshots_are_combined_once_in_source_order(self):
        text = combine_trusted_rule_text(
            {
                "body": {"text": "正文", "trusted": True},
                "expanded_body": {"text": "正文", "trusted": True},
                "pinned_comment": {"text": "作者置顶补充", "trusted": True},
            }
        )
        self.assertEqual("正文\n\n作者置顶补充", text)

    def test_list_projection_reuses_xiaohongshu_candidate_identity(self):
        identity = target_identity_from_lottery(
            {
                "action_plan": {
                    "target_pursuit_review_snapshot": {
                        "author": {
                            "stable_id": "5ff0e6410000000001008400",
                            "display_name": "作者昵称",
                            "profile_url": (
                                "https://www.xiaohongshu.com/user/profile/"
                                "5ff0e6410000000001008400"
                            ),
                            "verified": True,
                        }
                    }
                }
            }
        )
        self.assertEqual("5ff0e6410000000001008400", identity["uid"])
        self.assertEqual("作者昵称", identity["display_name"])
        self.assertTrue(identity["verified"])


if __name__ == "__main__":
    unittest.main()
