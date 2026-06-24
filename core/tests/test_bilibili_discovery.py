import unittest

from app.services.bilibili_discovery import parse_dynamic_item, parse_search_item
from app.services.discovery import split_keywords
from app.services.lottery_rules import parse_lottery_rule


class BilibiliDiscoveryTests(unittest.TestCase):
    def test_extracts_lottery_and_required_actions_from_dynamic(self):
        item = {
            "id_str": "123456789",
            "modules": {
                "module_author": {"pub_ts": 1710000000},
                "module_dynamic": {
                    "desc": {"text": "抽奖福利：关注我，点赞并转发本动态，评论区留言参与。"},
                    "major": None,
                },
            },
        }

        candidate = parse_dynamic_item(item)

        self.assertIsNotNone(candidate)
        self.assertEqual("https://t.bilibili.com/123456789", candidate.url)
        self.assertEqual(["followed", "liked", "commented", "reposted"], candidate.action_plan["required_actions"])
        self.assertFalse(candidate.action_plan["review_required"])
        self.assertGreaterEqual(candidate.action_plan["confidence"], 0.8)

    def test_repairs_latin1_mojibake_before_parsing(self):
        text = "互动抽奖福利：关注我，点赞并转发本动态，评论区留言参与。"
        mojibake_text = text.encode("utf-8").decode("latin1")
        item = {
            "id_str": "123456790",
            "modules": {
                "module_dynamic": {
                    "desc": {"text": mojibake_text},
                    "major": None,
                },
            },
        }

        candidate = parse_dynamic_item(item)

        self.assertIsNotNone(candidate)
        self.assertEqual(text, candidate.rule_text)
        self.assertEqual(["followed", "liked", "commented", "reposted"], candidate.action_plan["required_actions"])

    def test_marks_ambiguous_rule_for_review(self):
        plan = parse_lottery_rule("抽奖：转发本动态，关注可选。")

        self.assertTrue(plan["is_lottery"])
        self.assertTrue(plan["review_required"])
        self.assertIn("reposted", plan["required_actions"])
        self.assertTrue(plan["ambiguity_patterns"])

    def test_non_lottery_dynamic_is_not_actionable(self):
        plan = parse_lottery_rule("今天发布了一个新视频，欢迎观看。")

        self.assertFalse(plan["is_lottery"])
        self.assertEqual([], plan["required_actions"])
        self.assertTrue(plan["review_required"])

    def test_ignores_dynamic_without_numeric_id(self):
        item = {
            "id_str": "not-an-id",
            "modules": {"module_dynamic": {"desc": {"text": "抽奖：评论参与"}}},
        }

        self.assertIsNone(parse_dynamic_item(item))

    def test_parses_keyword_search_dynamic_result(self):
        item = {
            "dynamic_id": "1217003060937621510",
            "title": "<em class=\"keyword\">抽奖</em>福利",
            "description": "关注我，点赞并转发，评论区留言参与。",
            "pubdate": 1710000000,
        }

        candidate = parse_search_item(item)

        self.assertIsNotNone(candidate)
        self.assertEqual("https://t.bilibili.com/1217003060937621510", candidate.url)
        self.assertEqual(["followed", "liked", "commented", "reposted"], candidate.action_plan["required_actions"])
        self.assertFalse(candidate.action_plan["review_required"])

    def test_keyword_split_accepts_common_separators(self):
        self.assertEqual(split_keywords("互动抽奖, 福利\n转发抽奖；  "), ["互动抽奖", "福利", "转发抽奖"])


if __name__ == "__main__":
    unittest.main()
