import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from fastapi import HTTPException  # noqa: E402

from app.api.lotteries import (  # noqa: E402
    create_tracked_source,
    create_lottery,
    import_lottery_targets,
    list_tracked_sources,
    parse_target_lines,
)
from app.models.schemas import (  # noqa: E402
    LotteryCreate,
    LotteryTargetImport,
    TrackedSourceCreate,
)
from app.utils.canonicalizer import CanonicalizationError  # noqa: E402


class LotteryIngressBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_short_link_timeout_returns_structured_retryable_error(self):
        payload = LotteryCreate(
            platform="bilibili",
            raw_url="https://b23.tv/AbCdEf",
        )
        canonicalize = AsyncMock(
            side_effect=CanonicalizationError(
                "canonicalization_short_link_timeout",
                retryable=True,
            )
        )
        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch(
                "app.api.lotteries.validate_lottery_target",
                return_value=SimpleNamespace(
                    valid=True,
                    reason=None,
                    kind="short_link",
                ),
            ),
            patch("app.api.lotteries.canonicalize_lottery_url", canonicalize),
            patch("app.api.lotteries.structured_log"),
        ):
            with self.assertRaises(HTTPException) as context:
                await create_lottery(payload, object())

        self.assertEqual(503, context.exception.status_code)
        self.assertEqual(
            {
                "code": "lottery_target_canonicalization_failed",
                "reason_code": "canonicalization_short_link_timeout",
                "retryable": True,
            },
            context.exception.detail,
        )

    async def test_import_keeps_canonicalization_failure_row_local_and_safe(self):
        payload = LotteryTargetImport(
            platform="bilibili",
            content="https://www.bilibili.com/video/BV1xx411c7mD",
        )
        canonicalize = AsyncMock(
            side_effect=CanonicalizationError(
                "canonicalization_short_link_unresolved"
            )
        )
        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch("app.api.lotteries.canonicalize_lottery_url", canonicalize),
            patch("app.api.lotteries.database.execute", new=AsyncMock()) as execute,
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
        ):
            result = await import_lottery_targets(payload, object())

        self.assertEqual(0, result["created_count"])
        self.assertEqual(1, result["invalid_count"])
        self.assertEqual(
            "lottery_target_canonicalization_failed",
            result["invalid"][0]["error"],
        )
        self.assertEqual(
            "canonicalization_short_link_unresolved",
            result["invalid"][0]["reason_code"],
        )
        self.assertNotIn("Cannot canonicalize", result["invalid"][0]["error"])
        execute.assert_not_awaited()

    def test_import_line_rejects_url_larger_than_storage_column(self):
        prefix = "https://www.bilibili.com/video/BV1xx411c7mD?context="
        oversized_url = prefix + ("x" * (513 - len(prefix)))

        rows = parse_target_lines(oversized_url, "bilibili", 50)

        self.assertEqual(len(oversized_url), 513)
        self.assertEqual(1, len(rows))
        self.assertEqual("Target URL exceeds storage limit", rows[0]["error"])

    def test_import_line_rejects_score_outside_request_model_bounds(self):
        target = "https://www.bilibili.com/video/BV1xx411c7mD"

        for score in (-1, 101):
            with self.subTest(score=score):
                rows = parse_target_lines(f"{target},{score}", "bilibili", 50)
                self.assertEqual(1, len(rows))
                self.assertEqual(
                    "Value score must be between 0 and 100",
                    rows[0]["error"],
                )

        accepted = parse_target_lines(f"{target},100", "bilibili", 50)
        self.assertEqual(100, accepted[0]["value_score"])

    async def test_import_batch_marks_multiple_short_links_invalid_without_network(self):
        cases = (
            (
                "bilibili",
                "https://b23.tv/AbCdEf",
                "https://b23.tv/ZyXwVu",
                "xiaohongshu_import_short_link_batch_unsupported",
            ),
            (
                "weibo",
                "https://t.cn/A6abcdef",
                "https://t.cn/A6fedcba",
                "weibo_import_short_link_batch_unsupported",
            ),
            (
                "xiaohongshu",
                "https://xhslink.com/a/AbC123def",
                "https://xhslink.com/a/Def456ghi",
                "xiaohongshu_import_short_link_batch_unsupported",
            ),
            (
                "douyin",
                "https://v.douyin.com/abc123/",
                "https://v.douyin.com/def456/",
                "douyin_import_short_link_batch_unsupported",
            ),
        )
        with patch(
            "app.api.lotteries.require_min_role",
            return_value={"actor_id": "test-operator"},
        ):
            for (
                platform,
                first_short_url,
                second_short_url,
                expected_error,
            ) in cases:
                canonicalize = AsyncMock()
                payload = LotteryTargetImport(
                    platform=platform,
                    content=f"{first_short_url}\n{second_short_url}",
                )
                with self.subTest(platform=platform), patch(
                    "app.api.lotteries.canonicalize_lottery_url",
                    canonicalize,
                ), patch(
                    "app.api.lotteries.database.execute",
                    new=AsyncMock(),
                ) as execute, patch(
                    "app.api.lotteries._record_post_commit_event",
                    new=AsyncMock(),
                ):
                    result = await import_lottery_targets(payload, object())
                    self.assertEqual(0, result["created_count"])
                    self.assertEqual(2, result["invalid_count"])
                    self.assertEqual(
                        {
                            item["error"]
                            for item in result["invalid"]
                        },
                        {expected_error},
                    )
                    canonicalize.assert_not_awaited()
                    execute.assert_not_awaited()

    async def test_short_link_overflow_isolated_from_peer_direct_rows(self):
        payload = LotteryTargetImport(
            platform="bilibili",
            content=(
                "douyin,https://v.douyin.com/abc123/\n"
                "douyin,https://v.douyin.com/def456/\n"
                "bilibili,https://t.bilibili.com/123456789"
            ),
        )
        canonicalize = AsyncMock(
            return_value="canonical://bilibili/dynamic/123456789"
        )
        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch(
                "app.api.lotteries.canonicalize_lottery_url",
                canonicalize,
            ),
            patch(
                "app.api.lotteries.database.execute",
                new=AsyncMock(return_value=301),
            ) as execute,
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
        ):
            result = await import_lottery_targets(payload, object())

        self.assertEqual(1, result["created_count"])
        self.assertEqual(2, result["invalid_count"])
        self.assertEqual("bilibili", result["created"][0]["platform"])
        self.assertEqual(
            {"douyin"},
            {item["platform"] for item in result["invalid"]},
        )
        self.assertEqual(
            {"douyin_import_short_link_batch_unsupported"},
            {item["error"] for item in result["invalid"]},
        )
        canonicalize.assert_awaited_once_with(
            "bilibili",
            "https://t.bilibili.com/123456789",
        )
        execute.assert_awaited_once()

    async def test_one_short_link_can_share_batch_with_direct_targets(self):
        payload = LotteryTargetImport(
            platform="bilibili",
            content=(
                "https://b23.tv/AbCdEf\n"
                "https://t.bilibili.com/123456789"
            ),
        )
        canonicalize = AsyncMock(
            side_effect=(
                "canonical://bilibili/dynamic/987654321",
                "canonical://bilibili/dynamic/123456789",
            )
        )
        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch(
                "app.api.lotteries.canonicalize_lottery_url",
                canonicalize,
            ),
            patch(
                "app.api.lotteries.database.execute",
                new=AsyncMock(side_effect=(101, 102)),
            ),
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
        ):
            result = await import_lottery_targets(payload, object())

        self.assertEqual(2, result["created_count"])
        self.assertEqual(0, result["invalid_count"])
        self.assertEqual(2, canonicalize.await_count)

    async def test_mixed_import_applies_short_link_budget_per_platform(self):
        payload = LotteryTargetImport(
            platform="bilibili",
            content=(
                "bilibili,https://b23.tv/AbCdEf\n"
                "weibo,https://t.cn/A6abcdef"
            ),
        )
        canonicalize = AsyncMock(
            side_effect=(
                "canonical://bilibili/dynamic/987654321",
                "canonical://weibo/status/123456789",
            )
        )
        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch(
                "app.api.lotteries.canonicalize_lottery_url",
                canonicalize,
            ),
            patch(
                "app.api.lotteries.database.execute",
                new=AsyncMock(side_effect=(201, 202)),
            ),
            patch(
                "app.api.lotteries._record_post_commit_event",
                new=AsyncMock(),
            ),
        ):
            result = await import_lottery_targets(payload, object())

        self.assertEqual(2, result["created_count"])
        self.assertEqual(0, result["invalid_count"])
        self.assertEqual(2, canonicalize.await_count)

    async def test_discovery_source_errors_keep_string_detail_contract(self):
        cases = (
            (
                TrackedSourceCreate(
                    platform="bilibili",
                    source_type="unsupported",
                    source_value="value",
                ),
                "source_type must be url_list, keyword, or up",
            ),
            (
                TrackedSourceCreate(
                    platform="weibo",
                    source_type="keyword",
                    source_value="giveaway",
                ),
                "source_type must be url_list",
            ),
            (
                TrackedSourceCreate(
                    platform="weibo",
                    source_type="url_list",
                    source_value="   ",
                ),
                "source_value is required",
            ),
            (
                TrackedSourceCreate(
                    platform="bilibili",
                    source_type="up",
                    source_value="not-a-number",
                ),
                "bilibili_discovery_up_uid_invalid",
            ),
            (
                TrackedSourceCreate(
                    platform="bilibili",
                    source_type="keyword",
                    source_value="长" * 65,
                ),
                "bilibili_discovery_keyword_invalid",
            ),
            (
                TrackedSourceCreate(
                    platform="douyin",
                    source_type="url_list",
                    source_value="https://example.test/not-a-douyin-target",
                ),
                "platform_discovery_url_list_target_required",
            ),
        )
        with patch(
            "app.api.lotteries.require_min_role",
            return_value={"actor_id": "test-operator"},
        ):
            for payload, expected_detail in cases:
                with self.subTest(expected_detail=expected_detail):
                    with self.assertRaises(HTTPException) as caught:
                        await create_tracked_source(payload, object())
                    self.assertEqual(400, caught.exception.status_code)
                    self.assertIsInstance(caught.exception.detail, str)
                    self.assertEqual(expected_detail, caught.exception.detail)

    async def test_source_list_marks_invalid_legacy_rows_effectively_inactive(self):
        rows = [
            {
                "id": 1,
                "platform": "bilibili",
                "source_type": "up",
                "source_value": "123456",
                "active": 1,
            },
            {
                "id": 2,
                "platform": "bilibili",
                "source_type": "up",
                "source_value": "not-a-number",
                "active": 1,
            },
            {
                "id": 3,
                "platform": "weibo",
                "source_type": "keyword",
                "source_value": "giveaway",
                "active": 1,
            },
        ]
        with patch(
            "app.api.lotteries.database.fetch_all",
            AsyncMock(return_value=rows),
        ):
            result = await list_tracked_sources()

        self.assertTrue(result[0]["effective_active"])
        self.assertIsNone(result[0]["validation_error"])
        self.assertFalse(result[1]["effective_active"])
        self.assertEqual(
            "bilibili_discovery_up_uid_invalid",
            result[1]["validation_error"],
        )
        self.assertFalse(result[2]["effective_active"])
        self.assertEqual(
            "platform_discovery_source_type_not_supported",
            result[2]["validation_error"],
        )

    async def test_source_upsert_uses_bounded_non_sensitive_event_identity(self):
        prefix = "https://t.bilibili.com/123456789?context="
        source_value = prefix + ("x" * (256 - len(prefix)))
        payload = TrackedSourceCreate(
            platform="bilibili",
            source_type="url_list",
            source_value=source_value,
        )
        record_event = AsyncMock(return_value="event-1")

        with (
            patch(
                "app.api.lotteries.require_min_role",
                return_value={"actor_id": "test-operator"},
            ),
            patch(
                "app.api.lotteries.database.execute",
                AsyncMock(return_value=None),
            ) as execute,
            patch(
                "app.api.lotteries._record_post_commit_event",
                record_event,
            ),
        ):
            await create_tracked_source(payload, object())

        self.assertIn("LAST_INSERT_ID(id)", execute.await_args.args[0])
        aggregate_id = str(record_event.await_args.kwargs["aggregate_id"])
        self.assertLessEqual(len(aggregate_id), 128)
        self.assertNotIn(source_value, aggregate_id)


if __name__ == "__main__":
    unittest.main()
