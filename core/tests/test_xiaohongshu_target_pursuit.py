import hashlib
import json
import unittest

from app.services.xiaohongshu_target_pursuit import (
    MAX_ORIGINAL_TRACE_HOPS,
    analyze_candidate_evidence,
)


NOTE_A = "https://www.xiaohongshu.com/explore/64f1a2b3c4d5e6f7a8b9c0d1"
NOTE_B = "https://www.xiaohongshu.com/explore/74f1a2b3c4d5e6f7a8b9c0d2"
PROFILE_ID = "64f1a2b3c4d5e6f7a8b9c0d9"
PROFILE_URL = f"https://www.xiaohongshu.com/user/profile/{PROFILE_ID}"

BODY = """抽奖福利
奖品：iPhone 17 一台
活动时间：2026年7月23日-2026年8月23日
参与方式：关注、点赞、评论、收藏
"""


def author(stable_id=PROFILE_ID, profile_url=PROFILE_URL):
    return {
        "stable_id": stable_id,
        "profile_url": profile_url,
        "display_name": "抽奖博主",
    }


def evidence(
    *,
    note_url=NOTE_A,
    page_type="note",
    body=BODY,
    expanded_body="",
    pinned_comment=None,
    original_trace=None,
):
    payload = {
        "candidate": {
            "url": note_url,
            "page_type": page_type,
            "author": author(),
        },
        "content": {
            "body": body,
            "expanded_body": expanded_body,
        },
    }
    if pinned_comment is not None:
        payload["content"]["pinned_comment"] = pinned_comment
    if original_trace is not None:
        payload["original_trace"] = original_trace
    return payload


class XiaohongshuTargetPursuitTests(unittest.TestCase):
    def test_offline_direct_note_produces_pending_tamper_evident_result(self):
        payload = evidence()
        payload["offline_record"] = {"raw_url": NOTE_A}
        result = analyze_candidate_evidence(
            "offline_search_result",
            "batch-20260729.json",
            payload,
            observed_at="2026-07-29T15:00:00+08:00",
        )

        self.assertEqual("pending", result["initial_decision"])
        self.assertEqual(
            ["followed", "liked", "commented", "favorited"],
            result["rule"]["required_actions"],
        )
        self.assertEqual("active", result["activity_window"]["status"])
        self.assertEqual("2026-07-23", result["activity_window"]["starts_at"])
        self.assertEqual("2026-08-23", result["activity_window"]["ends_at"])
        self.assertEqual("iPhone 17 一台", result["prizes"][0]["text"])
        self.assertTrue(result["author"]["verified"])
        self.assertEqual(
            hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
            result["content_snapshots"]["body"]["sha256"],
        )
        json.dumps(result, ensure_ascii=False, sort_keys=True)

    def test_keyword_source_requires_matching_offline_search_evidence(self):
        missing = analyze_candidate_evidence(
            "keyword",
            "iPhone 抽奖",
            evidence(),
            observed_at="2026-07-29",
        )
        self.assertEqual("needs_review", missing["initial_decision"])
        self.assertIn(
            "keyword_source_evidence_missing",
            missing["reason_codes"],
        )

        matched_evidence = evidence()
        matched_evidence["search_result"] = {
            "query": "iPhone 抽奖",
            "note_url": NOTE_A,
        }
        matched = analyze_candidate_evidence(
            "keyword",
            "iPhone 抽奖",
            matched_evidence,
            observed_at="2026-07-29",
        )
        self.assertEqual("pending", matched["initial_decision"])
        self.assertTrue(matched["source"]["valid"])

    def test_author_profile_source_binds_stable_id_and_profile_url(self):
        valid = analyze_candidate_evidence(
            "author_profile",
            PROFILE_URL,
            evidence(),
            observed_at="2026-07-29",
        )
        self.assertEqual("pending", valid["initial_decision"])

        mismatched = analyze_candidate_evidence(
            "author_profile",
            "https://www.xiaohongshu.com/user/profile/another_user",
            evidence(),
            observed_at="2026-07-29",
        )
        self.assertEqual("needs_review", mismatched["initial_decision"])
        self.assertIn(
            "author_profile_source_mismatch",
            mismatched["reason_codes"],
        )

    def test_only_direct_xiaohongshu_note_urls_are_accepted(self):
        invalid = analyze_candidate_evidence(
            "offline_search_result",
            "bad-batch.json",
            {
                **evidence(
                note_url=(
                    "https://example.com/explore/"
                    "64f1a2b3c4d5e6f7a8b9c0d1"
                )
                ),
                "offline_record": {
                    "raw_url": (
                        "https://example.com/explore/"
                        "64f1a2b3c4d5e6f7a8b9c0d1"
                    )
                },
            },
            observed_at="2026-07-29",
        )
        self.assertEqual("needs_review", invalid["initial_decision"])
        self.assertIn("candidate_note_url_invalid", invalid["reason_codes"])
        self.assertIn(
            "offline_source_record_note_url_invalid",
            invalid["reason_codes"],
        )

        short = analyze_candidate_evidence(
            "offline_search_result",
            "https://xhslink.com/AbCdEf",
            evidence(),
            observed_at="2026-07-29",
        )
        self.assertEqual("needs_review", short["initial_decision"])
        self.assertTrue(short["source"]["short_link"])
        self.assertIn("short_link_requires_review", short["reason_codes"])

    def test_flat_worker_evidence_and_offline_batch_binding_are_supported(self):
        payload = {
            "raw_url": NOTE_A,
            "title": "iPhone 抽奖",
            "author": author(),
            "body_text": BODY,
            "expanded_text": "参与方式：关注、点赞、评论、收藏",
            "pinned_comment": {
                "text": "由作者置顶补充：参与方式不变",
                "pinned": True,
                "author_stable_id": PROFILE_ID,
                "author_profile_url": PROFILE_URL,
            },
            "offline_record": {"raw_url": NOTE_A},
            "capture_method": "worker_readonly_dom",
        }
        result = analyze_candidate_evidence(
            "offline_search_result",
            "xhs-export-001.jsonl",
            payload,
            observed_at="2026-07-29",
        )

        self.assertEqual("pending", result["initial_decision"])
        self.assertEqual("worker_readonly_dom", result["capture_method"])
        self.assertEqual(
            BODY,
            result["content_snapshots"]["body"]["text"],
        )
        self.assertEqual(
            "参与方式：关注、点赞、评论、收藏",
            result["content_snapshots"]["expanded_body"]["text"],
        )
        self.assertTrue(
            result["content_snapshots"]["pinned_comment"]["author_verified"]
        )

    def test_worker_nested_pinned_author_identity_is_verified(self):
        payload = {
            "raw_url": NOTE_A,
            "author": {
                "id": PROFILE_ID,
                "profile_url": PROFILE_URL,
                "display_name": "作者",
            },
            "body_text": BODY,
            "pinned_comment": {
                "text": "作者补充：评论指定文案",
                "is_pinned": True,
                "author": {
                    "id": PROFILE_ID,
                    "profile_url": PROFILE_URL,
                    "display_name": "作者",
                },
            },
            "offline_record": {"raw_url": NOTE_A},
            "capture_method": "playwright_read_only_dom_v1",
        }

        result = analyze_candidate_evidence(
            "offline_search_result",
            "worker-capture.jsonl",
            payload,
            observed_at="2026-07-29",
        )

        snapshot = result["content_snapshots"]["pinned_comment"]
        self.assertTrue(snapshot["author_verified"])
        self.assertTrue(snapshot["included_in_rule"])
        self.assertIn("commented", result["rule"]["required_actions"])

    def test_flat_collection_original_note_builds_bounded_trace(self):
        payload = {
            "raw_url": NOTE_A,
            "is_collection": True,
            "author": author(),
            "body_text": "合集封面",
            "original_note_url": NOTE_B,
            "original_note": {
                "raw_url": NOTE_B,
                "author": author(),
                "body_text": BODY,
                "expanded_text": "",
            },
            "offline_record": {"raw_url": NOTE_A},
        }
        result = analyze_candidate_evidence(
            "offline_search_result",
            "collection-export.jsonl",
            payload,
            observed_at="2026-07-29",
        )

        self.assertEqual("pending", result["initial_decision"])
        self.assertTrue(result["target"]["is_collection"])
        self.assertEqual(NOTE_B, result["target"]["original_note_url"])
        self.assertEqual(BODY, result["content_snapshots"]["body"]["text"])

    def test_offline_batch_without_record_binding_needs_review(self):
        result = analyze_candidate_evidence(
            "offline_search_result",
            "batch-without-record.jsonl",
            evidence(),
            observed_at="2026-07-29",
        )

        self.assertEqual("needs_review", result["initial_decision"])
        self.assertIn(
            "offline_source_evidence_missing",
            result["reason_codes"],
        )

    def test_collection_trace_resolves_original_with_bounded_audit_chain(self):
        traced = evidence(
            page_type="collection",
            original_trace=[
                {
                    "url": NOTE_A,
                    "page_type": "collection",
                    "next_url": NOTE_B,
                },
                {
                    "url": NOTE_B,
                    "page_type": "original_note",
                    "author": author(),
                },
            ],
        )
        result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            traced,
            observed_at="2026-07-29",
        )

        self.assertEqual("pending", result["initial_decision"])
        self.assertTrue(result["target"]["is_collection"])
        self.assertTrue(result["target"]["trace_complete"])
        self.assertEqual(NOTE_B, result["target"]["original_note_url"])
        self.assertIn(
            "collection_original_trace_resolved",
            result["reason_codes"],
        )
        self.assertNotIn(
            "collection_original_trace_resolved",
            result["review_reason_codes"],
        )

    def test_collection_trace_cycle_and_bound_fail_closed(self):
        cycle = evidence(
            page_type="collection",
            original_trace=[
                {
                    "url": NOTE_A,
                    "page_type": "collection",
                    "next_url": NOTE_B,
                },
                {
                    "url": NOTE_B,
                    "page_type": "repost",
                    "next_url": NOTE_A,
                },
            ],
        )
        cycle_result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            cycle,
            observed_at="2026-07-29",
        )
        self.assertEqual("needs_review", cycle_result["initial_decision"])
        self.assertIn("original_trace_cycle", cycle_result["reason_codes"])

        oversized = evidence(
            page_type="collection",
            original_trace=[
                {
                    "url": NOTE_A,
                    "page_type": "collection",
                    "next_url": NOTE_B,
                }
                for _ in range(MAX_ORIGINAL_TRACE_HOPS + 1)
            ],
        )
        oversized_result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            oversized,
            observed_at="2026-07-29",
        )
        self.assertIn(
            "original_trace_limit_exceeded",
            oversized_result["reason_codes"],
        )
        self.assertFalse(oversized_result["target"]["trace_complete"])

    def test_pinned_comment_is_hashed_but_not_trusted_without_author_match(self):
        pinned_text = "置顶：评论并@两位好友"
        result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            evidence(
                pinned_comment={
                    "text": pinned_text,
                    "pinned": True,
                    "author_stable_id": "different_user",
                    "author_profile_url": (
                        "https://www.xiaohongshu.com/user/profile/"
                        "different_user"
                    ),
                }
            ),
            observed_at="2026-07-29",
        )

        snapshot = result["content_snapshots"]["pinned_comment"]
        self.assertEqual(
            hashlib.sha256(pinned_text.encode("utf-8")).hexdigest(),
            snapshot["sha256"],
        )
        self.assertFalse(snapshot["author_verified"])
        self.assertFalse(snapshot["included_in_rule"])
        self.assertIn(
            "pinned_comment_author_unverified",
            result["reason_codes"],
        )
        self.assertNotIn(
            "friend_or_account_mention",
            [item["code"] for item in result["complex_conditions"]],
        )

    def test_verified_pinned_comment_contributes_rules_and_complexity(self):
        result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            evidence(
                body=(
                    "抽奖福利\n奖品：咖喱套餐\n"
                    "活动时间：2026年6月30日—2026年7月29日"
                ),
                pinned_comment={
                    "text": "参与方式：关注并评论，晒出相关照片或故事",
                    "pinned": True,
                    "author_stable_id": PROFILE_ID,
                    "author_profile_url": PROFILE_URL,
                },
            ),
            observed_at="2026-07-30",
        )

        snapshot = result["content_snapshots"]["pinned_comment"]
        self.assertTrue(snapshot["author_verified"])
        self.assertTrue(snapshot["included_in_rule"])
        self.assertIn("followed", result["rule"]["required_actions"])
        self.assertIn("commented", result["rule"]["required_actions"])
        self.assertIn(
            "media_or_story_submission",
            [item["code"] for item in result["complex_conditions"]],
        )
        self.assertEqual("expired", result["activity_window"]["status"])
        self.assertIn("activity_expired", result["reason_codes"])
        self.assertEqual("needs_review", result["initial_decision"])

    def test_missing_observation_time_never_uses_wall_clock(self):
        result = analyze_candidate_evidence(
            "offline_search_result",
            NOTE_A,
            evidence(),
        )

        self.assertIsNone(result["observed_at"])
        self.assertEqual("unknown", result["activity_window"]["status"])
        self.assertIn(
            "observed_at_required_for_time_check",
            result["reason_codes"],
        )
        self.assertEqual("needs_review", result["initial_decision"])


if __name__ == "__main__":
    unittest.main()
