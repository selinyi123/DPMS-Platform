import asyncio
import base64
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(b"0" * 32).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.api import xiaohongshu_targets as api  # noqa: E402
from app.services import xiaohongshu_target_candidates as service  # noqa: E402


class FakeDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield


class XiaohongshuTargetSourceRepositoryTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_list_sources_keeps_pagination_out_of_count_query(self):
        db = type("ListSourcesDatabase", (), {})()
        db.fetch_one = AsyncMock(return_value={"total": 1})
        db.fetch_all = AsyncMock(return_value=[])

        result = await service.repository.list_sources(
            source_type="keyword",
            active=True,
            limit=20,
            offset=40,
            db=db,
        )

        self.assertEqual(1, result["total"])
        self.assertEqual(
            {"source_type": "keyword", "active": 1},
            db.fetch_one.await_args.args[1],
        )
        self.assertEqual(
            {
                "source_type": "keyword",
                "active": 1,
                "limit": 20,
                "offset": 40,
            },
            db.fetch_all.await_args.args[1],
        )


def source_row(*, status="idle", version=4):
    return {
        "id": 7,
        "source_type": "keyword",
        "source_value": "抽奖",
        "active": 1,
        "status": status,
        "last_error_code": None,
        "version": version,
    }


def candidate_row(*, status="pending", version=3, lottery_id=None):
    return {
        "id": 11,
        "platform": "xiaohongshu",
        "raw_url": (
            "https://www.xiaohongshu.com/explore/"
            "64f1a2b3c4d5e6f7a8b9c0d1"
        ),
        "canonical_url": (
            "canonical://xiaohongshu/note/64f1a2b3c4d5e6f7a8b9c0d1"
        ),
        "title": "抽奖",
        "evidence": {"candidate": {"url": "https://example.invalid"}},
        "rule": {"required_actions": ["liked"]},
        "classification": {"initial_decision": "pending"},
        "published_at": None,
        "value_score": 10,
        "expires_at": None,
        "decision_status": status,
        "decision_reason": None,
        "accepted_lottery_id": lottery_id,
        "version": version,
    }


class XiaohongshuTargetSourceScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_and_finish_use_version_cas(self):
        db = FakeDatabase()
        initial = source_row()
        started = source_row(status="scanning", version=5)
        finished = source_row(status="succeeded", version=6)
        with (
            patch.object(
                service,
                "resolve_source",
                AsyncMock(return_value=(initial, False)),
            ),
            patch.object(
                service.repository,
                "get_source",
                AsyncMock(side_effect=[initial, started]),
            ),
            patch.object(
                service.repository,
                "update_source_scan_state",
                AsyncMock(return_value=True),
            ) as update,
        ):
            result = await service.begin_source_scan(
                {"source_id": 7},
                db=db,
            )

        self.assertEqual(5, result["version"])
        self.assertEqual(4, update.await_args.kwargs["expected_version"])
        self.assertEqual("scanning", update.await_args.kwargs["status"])

        with (
            patch.object(
                service.repository,
                "update_source_scan_state",
                AsyncMock(return_value=True),
            ) as finish_update,
            patch.object(
                service.repository,
                "get_source",
                AsyncMock(return_value=finished),
            ),
        ):
            result = await service.finish_source_scan(
                7,
                scan_version=5,
                succeeded=True,
                db=db,
            )

        self.assertEqual(6, result["version"])
        self.assertEqual(
            5,
            finish_update.await_args.kwargs["expected_version"],
        )
        self.assertEqual("succeeded", finish_update.await_args.kwargs["status"])

    async def test_stale_scan_is_recovered_but_recent_scan_is_rejected(self):
        db = FakeDatabase()
        scanning = source_row(status="scanning", version=9)
        recovered = source_row(status="scanning", version=10)
        with (
            patch.object(
                service,
                "resolve_source",
                AsyncMock(return_value=(scanning, False)),
            ),
            patch.object(
                service.repository,
                "get_source",
                AsyncMock(side_effect=[scanning, recovered]),
            ),
            patch.object(
                service.repository,
                "source_scan_is_stale",
                AsyncMock(return_value=True),
            ) as is_stale,
            patch.object(
                service.repository,
                "update_source_scan_state",
                AsyncMock(return_value=True),
            ) as update,
        ):
            result = await service.begin_source_scan(
                {"source_id": 7},
                db=db,
            )

        self.assertEqual(10, result["version"])
        self.assertEqual(
            service.SOURCE_SCAN_STALE_AFTER_SECONDS,
            is_stale.await_args.kwargs["stale_after_seconds"],
        )
        self.assertEqual(
            "xiaohongshu_target_stale_scan_recovered",
            update.await_args.kwargs["error_code"],
        )

        with (
            patch.object(
                service,
                "resolve_source",
                AsyncMock(return_value=(scanning, False)),
            ),
            patch.object(
                service.repository,
                "get_source",
                AsyncMock(return_value=scanning),
            ),
            patch.object(
                service.repository,
                "source_scan_is_stale",
                AsyncMock(return_value=False),
            ),
            patch.object(
                service.repository,
                "update_source_scan_state",
                AsyncMock(),
            ) as update,
        ):
            with self.assertRaisesRegex(
                service.XiaohongshuTargetCandidateError,
                "scan_in_progress",
            ):
                await service.begin_source_scan(
                    {"source_id": 7},
                    db=db,
                )
        update.assert_not_awaited()

    async def test_cancelled_scan_is_shielded_and_marked_failed(self):
        source = source_row(status="scanning", version=5)
        request = type("Request", (), {})()
        data = api.XiaohongshuTargetScanRequest(
            source_id=7,
            max_candidates=5,
        )
        with (
            patch.object(
                api,
                "require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch.object(
                service,
                "begin_source_scan",
                AsyncMock(return_value=source),
            ),
            patch(
                "app.services.xiaohongshu_target_pursuit_requests."
                "dispatch_xiaohongshu_target_pursuit_scan",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch.object(
                service,
                "finish_source_scan",
                AsyncMock(return_value=source),
            ) as finish,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await api.scan_target_source(data, request)

        finish.assert_awaited_once_with(
            7,
            scan_version=5,
            succeeded=False,
            error_code="xiaohongshu_target_pursuit_scan_cancelled",
        )

    async def test_empty_scan_is_successful_without_calling_ingest(self):
        source = source_row(status="scanning", version=5)
        finished = source_row(status="succeeded", version=6)
        request = type("Request", (), {})()
        data = api.XiaohongshuTargetScanRequest(
            source_id=7,
            max_candidates=5,
        )
        with (
            patch.object(
                api,
                "require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch.object(
                service,
                "begin_source_scan",
                AsyncMock(return_value=source),
            ),
            patch(
                "app.services.xiaohongshu_target_pursuit_requests."
                "dispatch_xiaohongshu_target_pursuit_scan",
                AsyncMock(
                    return_value={
                        "request_id": "request-1",
                        "status": "completed",
                        "candidates": [],
                    }
                ),
            ),
            patch.object(
                service,
                "ingest_candidates",
                AsyncMock(),
            ) as ingest,
            patch.object(
                service,
                "finish_source_scan",
                AsyncMock(return_value=finished),
            ),
            patch.object(api, "audit_event", AsyncMock()),
            patch.object(api, "record_event", AsyncMock()),
        ):
            result = await api.scan_target_source(data, request)

        ingest.assert_not_awaited()
        self.assertEqual("scanned", result["status"])
        self.assertEqual(0, result["received"])
        self.assertEqual([], result["items"])
        self.assertEqual("succeeded", result["source"]["status"])


class XiaohongshuTargetCandidateDecisionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_skip_never_creates_lottery_or_rule_snapshot(self):
        db = FakeDatabase()
        row = candidate_row()
        snapshot = {
            **row,
            "decision_status": "skipped",
            "decision_reason": "manual reject",
            "accepted_lottery_id": None,
            "version": 4,
            "source_hits": [],
        }
        with (
            patch.object(
                service.repository,
                "get_candidate",
                AsyncMock(return_value=row),
            ),
            patch.object(
                service.repository,
                "persist_candidate_decision",
                AsyncMock(return_value=True),
            ) as persist,
            patch.object(
                service.repository,
                "get_candidate_snapshot",
                AsyncMock(return_value=snapshot),
            ),
            patch.object(
                service.repository,
                "create_or_get_lottery_for_candidate",
                AsyncMock(),
            ) as create_lottery,
            patch.object(service, "ensure_rule_snapshot", AsyncMock()) as ensure,
        ):
            result = await service.decide_candidate(
                11,
                expected_version=3,
                decision_status="skipped",
                decision_reason="manual reject",
                actor_id="operator-1",
                db=db,
            )

        self.assertEqual("skipped", result["decision_status"])
        self.assertIsNone(
            persist.await_args.kwargs["accepted_lottery_id"]
        )
        create_lottery.assert_not_awaited()
        ensure.assert_not_awaited()

    async def test_accept_creates_lottery_and_incomplete_snapshot_atomically(self):
        db = FakeDatabase()
        row = candidate_row()
        source_hit = {
            "source_id": 7,
            "source_type": "keyword",
            "source_value": "抽奖",
        }
        snapshot = {
            **row,
            "decision_status": "accepted",
            "accepted_lottery_id": 41,
            "version": 4,
            "source_hits": [source_hit],
        }
        with (
            patch.object(
                service.repository,
                "get_candidate",
                AsyncMock(return_value=row),
            ),
            patch.object(
                service.repository,
                "latest_candidate_source_hit",
                AsyncMock(return_value=source_hit),
            ),
            patch.object(
                service.repository,
                "create_or_get_lottery_for_candidate",
                AsyncMock(return_value={"id": 41, "platform": "xiaohongshu"}),
            ) as create_lottery,
            patch.object(
                service.repository,
                "persist_candidate_decision",
                AsyncMock(return_value=True),
            ) as persist,
            patch.object(
                service.repository,
                "get_candidate_snapshot",
                AsyncMock(return_value=snapshot),
            ),
            patch.object(
                service,
                "ensure_rule_snapshot",
                AsyncMock(return_value={"id": 51, "is_complete": 0}),
            ) as ensure,
        ):
            result = await service.decide_candidate(
                11,
                expected_version=3,
                decision_status="accepted",
                decision_reason=None,
                actor_id="operator-1",
                db=db,
            )

        self.assertEqual(41, result["accepted_lottery_id"])
        create_lottery.assert_awaited_once()
        self.assertFalse(ensure.await_args.kwargs["complete"])
        self.assertFalse(
            ensure.await_args.kwargs["allow_existing_complete"]
        )
        self.assertEqual(
            41,
            persist.await_args.kwargs["accepted_lottery_id"],
        )

    async def test_version_conflict_precedes_all_writes(self):
        db = FakeDatabase()
        row = candidate_row(version=8)
        with (
            patch.object(
                service.repository,
                "get_candidate",
                AsyncMock(return_value=row),
            ),
            patch.object(
                service.repository,
                "persist_candidate_decision",
                AsyncMock(),
            ) as persist,
            patch.object(
                service.repository,
                "create_or_get_lottery_for_candidate",
                AsyncMock(),
            ) as create_lottery,
        ):
            with self.assertRaises(
                service.XiaohongshuTargetCandidateError
            ) as caught:
                await service.decide_candidate(
                    11,
                    expected_version=7,
                    decision_status="accepted",
                    decision_reason=None,
                    actor_id="operator-1",
                    db=db,
                )

        self.assertEqual(
            "xiaohongshu_target_candidate_version_conflict",
            caught.exception.code,
        )
        self.assertEqual(8, caught.exception.current_version)
        persist.assert_not_awaited()
        create_lottery.assert_not_awaited()


class XiaohongshuTargetRequestModelTests(unittest.TestCase):
    def test_decision_model_accepts_current_fields_and_legacy_aliases(self):
        current = api.XiaohongshuTargetDecisionUpdate(
            expected_version=2,
            decision_status="needs_review",
            decision_reason="check rule",
        )
        legacy = api.XiaohongshuTargetDecisionUpdate(
            expected_version=2,
            status="skipped",
            reason="duplicate",
        )
        self.assertEqual("needs_review", current.decision_status)
        self.assertEqual("skipped", legacy.decision_status)
        self.assertEqual("duplicate", legacy.decision_reason)


class XiaohongshuTargetCandidateBoundaryTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_source_values_are_canonical_and_bounded(self):
        self.assertEqual(
            "抽奖 福利",
            service.normalize_source_value(" 抽奖   福利 ", "keyword"),
        )
        with self.assertRaisesRegex(
            service.XiaohongshuTargetCandidateError,
            "source_value_invalid",
        ):
            service.normalize_source_value("抽" * 65, "keyword")
        self.assertEqual(
            "https://www.xiaohongshu.com/user/profile/abc123",
            service.normalize_source_value(
                (
                    "https://www.xiaohongshu.com/user/profile/abc123"
                    "?xsec_token=must-not-persist#feed"
                ),
                "author_profile",
            ),
        )
        with self.assertRaisesRegex(
            service.XiaohongshuTargetCandidateError,
            "source_value_invalid",
        ):
            service.normalize_source_value(
                "https://evil.example/user/profile/abc123",
                "author_profile",
            )

    def test_sensitive_json_keys_fail_closed_and_urls_are_sanitized(self):
        with self.assertRaisesRegex(
            service.XiaohongshuTargetCandidateError,
            "sensitive_field_forbidden",
        ):
            service._bounded_json_object(
                {"nested": {"accessToken": "must-not-persist"}},
                "evidence",
            )
        with self.assertRaisesRegex(
            service.XiaohongshuTargetCandidateError,
            "sensitive_field_forbidden",
        ):
            service._bounded_json_object(
                {"body_text": "authorization=Bearer-secret"},
                "evidence",
            )
        sanitized = service._bounded_json_object(
            {
                "note_url": (
                    "https://www.xiaohongshu.com/explore/"
                    "64f1a2b3c4d5e6f7a8b9c0d1"
                    "?xsec_token=must-not-persist#feed"
                )
            },
            "evidence",
        )
        self.assertEqual(
            (
                "https://www.xiaohongshu.com/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            ),
            sanitized["note_url"],
        )

    async def test_analyzer_projection_is_authoritative(self):
        raw_url = (
            "https://www.xiaohongshu.com/explore/"
            "64f1a2b3c4d5e6f7a8b9c0d1"
        )
        analysis = {
            "version": 1,
            "platform": "xiaohongshu",
            "initial_decision": "pending",
            "reason_codes": [],
            "rule": {
                "is_lottery": False,
                "required_actions": [],
            },
        }
        with (
            patch.object(
                service,
                "canonicalize_platform_url",
                AsyncMock(
                    return_value=(
                        "canonical://xiaohongshu/note/"
                        "64f1a2b3c4d5e6f7a8b9c0d1"
                    )
                ),
            ),
            patch.object(
                service,
                "_analyze_candidate",
                AsyncMock(return_value=analysis),
            ),
        ):
            prepared = await service._prepare_candidate(
                {
                    "source_type": "offline_search_result",
                    "source_value": "export.jsonl",
                },
                {
                    "raw_url": f"{raw_url}?xsec_token=must-not-persist",
                    "evidence": {"offline_record": {"raw_url": raw_url}},
                    "rule": {
                        "is_lottery": True,
                        "required_actions": ["commented"],
                    },
                    "classification": {
                        "initial_decision": "accepted",
                    },
                },
            )

        self.assertEqual(raw_url, prepared["raw_url"])
        self.assertFalse(prepared["rule"]["is_lottery"])
        self.assertEqual("pending", prepared["initial_decision"])
        self.assertEqual(
            ["rule", "classification"],
            prepared["classification"][
                "ignored_untrusted_projection_fields"
            ],
        )

    def test_trusted_content_snapshots_drive_accepted_rule_text(self):
        candidate = candidate_row()
        candidate["classification"] = {
            "content_snapshots": {
                "body": {"trusted": True, "text": "正文规则"},
                "expanded_body": {
                    "trusted": True,
                    "text": "展开补充",
                },
                "pinned_comment": {
                    "trusted": False,
                    "text": "未核验置顶",
                },
            }
        }
        self.assertEqual(
            "正文规则\n\n展开补充",
            service._rule_text(candidate),
        )


if __name__ == "__main__":
    unittest.main()
