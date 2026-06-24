import unittest

from app.services.lottery_rules import parse_lottery_rule


class WeiboLotteryRuleTests(unittest.TestCase):
    def test_extracts_required_actions_without_review(self):
        plan = parse_lottery_rule("抽奖福利：关注+转发+评论本微博，抽3位粉丝送好礼", "weibo")

        self.assertTrue(plan["is_lottery"])
        self.assertEqual({"followed", "commented", "reposted"}, set(plan["required_actions"]))
        self.assertFalse(plan["review_required"])
        self.assertEqual([], plan["unsupported_actions"])

    def test_flags_friend_mention_as_unsupported(self):
        plan = parse_lottery_rule("转发抽奖：评论区@三个好友，点赞", "weibo")

        self.assertTrue(plan["is_lottery"])
        self.assertIn("mention_friends", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_non_lottery_post_is_not_actionable(self):
        plan = parse_lottery_rule("今天天气不错，分享一下心情。", "weibo")

        self.assertFalse(plan["is_lottery"])
        self.assertTrue(plan["review_required"])


class XiaohongshuLotteryRuleTests(unittest.TestCase):
    def test_extracts_required_actions_without_review(self):
        plan = parse_lottery_rule("福利来啦，关注点赞评论分享，抽2位包邮送同款", "xiaohongshu")

        self.assertTrue(plan["is_lottery"])
        self.assertEqual({"followed", "liked", "commented", "reposted"}, set(plan["required_actions"]))
        self.assertFalse(plan["review_required"])
        self.assertEqual([], plan["unsupported_actions"])

    def test_flags_favorite_as_unsupported(self):
        plan = parse_lottery_rule("抽奖时间到：关注+点赞+收藏+评论，评论区抽1位送同款", "xiaohongshu")

        self.assertTrue(plan["is_lottery"])
        self.assertIn("favorited", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_non_lottery_post_is_not_actionable(self):
        plan = parse_lottery_rule("今天分享一下我的护肤心得。", "xiaohongshu")

        self.assertFalse(plan["is_lottery"])
        self.assertTrue(plan["review_required"])


class DouyinLotteryRuleTests(unittest.TestCase):
    def test_extracts_required_actions_without_review(self):
        plan = parse_lottery_rule("抽奖福利：关注+点赞+评论+转发，抽2位粉丝", "douyin")

        self.assertTrue(plan["is_lottery"])
        self.assertEqual({"followed", "liked", "commented", "reposted"}, set(plan["required_actions"]))
        self.assertFalse(plan["review_required"])

    def test_marks_ambiguous_rule_for_review(self):
        plan = parse_lottery_rule("抽奖：评论区留言即可，关注点赞可选。", "douyin")

        self.assertTrue(plan["is_lottery"])
        self.assertTrue(plan["review_required"])
        self.assertTrue(plan["ambiguity_patterns"])

    def test_non_lottery_post_is_not_actionable(self):
        plan = parse_lottery_rule("今天发布了一个新视频，欢迎观看。", "douyin")

        self.assertFalse(plan["is_lottery"])
        self.assertEqual([], plan["required_actions"])
        self.assertTrue(plan["review_required"])


class UnknownPlatformFallbackTests(unittest.TestCase):
    def test_unknown_platform_falls_back_to_bilibili_patterns(self):
        plan = parse_lottery_rule("抽奖：转发本动态，关注可选。", "unknown_platform")

        self.assertTrue(plan["is_lottery"])
        self.assertIn("reposted", plan["required_actions"])
        self.assertTrue(plan["ambiguity_patterns"])


if __name__ == "__main__":
    unittest.main()
