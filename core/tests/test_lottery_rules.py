import unittest

from app.services.lottery_rules import parse_lottery_rule


class BilibiliLotteryRuleTests(unittest.TestCase):
    def test_extracts_all_actions_from_combined_action_permutations(self):
        for combined_action in ("转评赞", "转赞评", "评转赞", "评赞转", "赞转评", "赞评转"):
            with self.subTest(combined_action=combined_action):
                plan = parse_lottery_rule(f"抽奖：关注并{combined_action}本条动态", "bilibili")

                self.assertTrue(plan["is_lottery"])
                self.assertEqual(
                    {"followed", "liked", "commented", "reposted"},
                    set(plan["required_actions"]),
                )
                self.assertFalse(plan["review_required"])
                self.assertEqual([], plan["unsupported_actions"])

    def test_asus_rule_is_fail_closed_when_content_requirements_are_unsupported(self):
        plan = parse_lottery_rule(
            "带话题 #ASUS翻转夏日#并@ASUS华硕官方UP 晒出你家“踩稿官”的视频/照片+翻译，"
            "赢ROG键盘，这波不亏！关注@ASUS华硕官方UP +转评赞本条动态",
            "bilibili",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            {"followed", "liked", "commented", "reposted"},
            set(plan["required_actions"]),
        )
        self.assertTrue(plan["review_required"])
        self.assertEqual(
            {"topic_tag", "mention_account", "media_submission", "translation_required"},
            set(plan["unsupported_actions"]),
        )
        self.assertEqual(
            {
                "follow_targets": ["@ASUS华硕官方UP"],
                "commented": {
                    "topic_tags": ["#ASUS翻转夏日#"],
                    "mentions": ["@ASUS华硕官方UP"],
                },
                "reposted": {"topic_tags": [], "mentions": []},
            },
            plan["content_requirements"],
        )

    def test_normal_post_context_does_not_create_unsupported_requirements(self):
        plan = parse_lottery_rule(
            "#新品发布# 发布新视频，感谢字幕组翻译。抽奖：关注@ASUS华硕官方UP并转评赞本条动态；"
            "一等奖送键盘，二等奖送鼠标。",
            "bilibili",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            {"followed", "liked", "commented", "reposted"},
            set(plan["required_actions"]),
        )
        self.assertFalse(plan["review_required"])
        self.assertEqual([], plan["unsupported_actions"])
        self.assertEqual(
            {
                "follow_targets": ["@ASUS华硕官方UP"],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            },
            plan["content_requirements"],
        )

    def test_follow_target_is_not_misclassified_as_comment_mention(self):
        plan = parse_lottery_rule(
            "抽奖：关注@ASUS华硕官方UP，评论带上#ASUS翻转夏日#并@ROG玩家国度。",
            "bilibili",
        )

        self.assertEqual(
            {
                "follow_targets": ["@ASUS华硕官方UP"],
                "commented": {
                    "topic_tags": ["#ASUS翻转夏日#"],
                    "mentions": ["@ROG玩家国度"],
                },
                "reposted": {"topic_tags": [], "mentions": []},
            },
            plan["content_requirements"],
        )

    def test_compact_follow_action_suffix_is_not_part_of_target_handle(self):
        plan = parse_lottery_rule(
            "抽奖：关注@ASUS华硕官方UP并转评赞本条动态。",
            "bilibili",
        )

        self.assertEqual(
            ["@ASUS华硕官方UP"],
            plan["content_requirements"]["follow_targets"],
        )
        self.assertEqual(
            [],
            plan["content_requirements"]["commented"]["mentions"],
        )

    def test_account_name_ending_in_action_word_is_not_truncated(self):
        plan = parse_lottery_rule(
            "抽奖：关注@每日评论，点赞并转发本条动态。",
            "bilibili",
        )

        self.assertEqual(
            ["@每日评论"],
            plan["content_requirements"]["follow_targets"],
        )

    def test_full_opus_binds_only_participation_instruction_mention(self):
        plan = parse_lottery_rule(
            "七月的暑期抽奖给大家安排上！\n"
            "本期帽子由 @旅客君 和 @日边 联合设计，购买请前往 "
            "@绒爪实验室 的店铺。\n"
            "参与方式：关注本账号、评论并转发本动态，同时评论请记得 "
            "@旅客君LookUplus，否则无效。",
            "bilibili",
        )

        self.assertEqual(
            ["@旅客君LookUplus"],
            plan["content_requirements"]["commented"]["mentions"],
        )
        self.assertEqual(
            [], plan["content_requirements"]["reposted"]["mentions"]
        )
        self.assertEqual([], plan["content_requirements"]["follow_targets"])

    def test_generic_friend_placeholder_has_no_fake_exact_identity(self):
        plan = parse_lottery_rule("@两位好友并转发，抽奖送键盘", "bilibili")

        self.assertIn("mention_account", plan["unsupported_actions"])
        self.assertEqual(
            [], plan["content_requirements"]["commented"]["mentions"]
        )

    def test_flags_content_specific_and_unsupported_interactions(self):
        cases = {
            "抽奖：关注点赞转发，评论指定文案“ASUS翻转夏日”。": "comment_content",
            "抽奖：关注点赞转发，评论区回答你最喜欢的产品。": "comment_content",
            "抽奖：关注点赞评论，转发时带上文案“支持华硕”。": "repost_content",
            "抽奖：关注点赞评论转发并投币。": "coined",
            "抽奖：关注点赞评论转发并收藏。": "favorited",
            "抽奖方式A：关注转评赞；方式B：评论指定文案参与。": "multiple_prize_branches",
            "抽奖第一种方法关注转评赞，第二种方法评论参与。": "multiple_prize_branches",
        }

        for text, unsupported_action in cases.items():
            with self.subTest(text=text):
                plan = parse_lottery_rule(text, "bilibili")
                self.assertTrue(plan["is_lottery"])
                self.assertIn(unsupported_action, plan["unsupported_actions"])
                self.assertTrue(plan["review_required"])

    def test_flags_comment_content_when_instruction_precedes_comment_word(self):
        plan = parse_lottery_rule(
            "说说你最喜欢的ROG产品，评论区抽一位送键盘",
            "bilibili",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("commented", plan["required_actions"])
        self.assertIn("comment_content", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_flags_numbered_friend_mentions_at_start_of_rule(self):
        plan = parse_lottery_rule(
            "@两位好友并转发，抽奖送键盘",
            "bilibili",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("mention_account", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_flags_topic_immediately_after_comment_instruction(self):
        for text in (
            "评论 #ASUS翻转夏日# 并转发，抽奖送键盘",
            "评论区#ASUS翻转夏日#参与，抽一位送键盘",
        ):
            with self.subTest(text=text):
                plan = parse_lottery_rule(text, "bilibili")
                self.assertIn("topic_tag", plan["unsupported_actions"])
                self.assertTrue(plan["review_required"])

    def test_flags_leave_content_and_question_before_comment(self):
        for text in (
            "评论区留下你最喜欢的产品，抽奖送键盘",
            "你最喜欢哪款？评论区留言抽奖送键盘",
        ):
            with self.subTest(text=text):
                plan = parse_lottery_rule(text, "bilibili")
                self.assertIn("comment_content", plan["unsupported_actions"])
                self.assertTrue(plan["review_required"])

    def test_flags_direct_comment_payload_synonyms(self):
        for text in (
            "抽奖：点赞并评论你的幸运数字",
            "抽奖：关注并评论你所在的城市",
            "抽奖：点赞后在评论区打出 666",
        ):
            with self.subTest(text=text):
                plan = parse_lottery_rule(text, "bilibili")
                self.assertIn("commented", plan["required_actions"])
                self.assertIn("comment_content", plan["unsupported_actions"])
                self.assertTrue(plan["review_required"])

    def test_non_lottery_win_wording_is_not_actionable(self):
        plan = parse_lottery_rule("打游戏赢了画面也不卡，欢迎点赞评论转发关注。", "bilibili")

        self.assertFalse(plan["is_lottery"])
        self.assertTrue(plan["review_required"])

    def test_specific_card_prize_still_identifies_lottery(self):
        plan = parse_lottery_rule("关注并转评赞，赢10元京东E卡。", "bilibili")

        self.assertTrue(plan["is_lottery"])
        self.assertFalse(plan["review_required"])

    def test_flags_separate_post_and_multiple_prize_branches(self):
        plan = parse_lottery_rule(
            "抽奖有两种参与方式：方式一关注并转评赞，方式二另发动态投稿；"
            "一等奖送键盘，二等奖送鼠标。",
            "bilibili",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("separate_post", plan["unsupported_actions"])
        self.assertIn("multiple_prize_branches", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])


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

    def test_flags_content_bearing_comment_and_topic_as_unsupported(self):
        plan = parse_lottery_rule(
            "转发抽奖：关注点赞，评论区回答你最喜欢的产品并带上#新品体验#。",
            "weibo",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("comment_content", plan["unsupported_actions"])
        self.assertIn("topic_tag", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_non_lottery_post_is_not_actionable(self):
        plan = parse_lottery_rule("今天天气不错，分享一下心情。", "weibo")

        self.assertFalse(plan["is_lottery"])

    def test_weibo_prize_wording_identifies_realistic_lotteries(self):
        cases = (
            "带话题#ASUS翻转夏日#并@ASUS华硕官方UP晒图，赢ROG键盘！关注@ASUS华硕官方UP+转评赞本条动态",
            "关注并转发，评论区抽一位送同款键盘",
            "关注+转评赞，送出奖品一份",
            "关注点赞评论转发，抽ROG键盘",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(parse_lottery_rule(text, "weibo")["is_lottery"])

    def test_weibo_non_lottery_keyboard_review_is_not_misclassified(self):
        plan = parse_lottery_rule(
            "今天抽空评测ROG键盘，欢迎关注点赞评论转发。",
            "weibo",
        )
        self.assertFalse(plan["is_lottery"])
        self.assertTrue(plan["review_required"])


class XiaohongshuLotteryRuleTests(unittest.TestCase):
    def test_extracts_strict_four_actions_without_review(self):
        plan = parse_lottery_rule(
            "福利来啦，关注点赞评论收藏，抽2位包邮送同款",
            "xiaohongshu",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            ["followed", "liked", "commented", "favorited"],
            plan["required_actions"],
        )
        self.assertFalse(plan["review_required"])
        self.assertEqual([], plan["unsupported_actions"])

    def test_four_interactions_shorthand_expands_to_exact_contract(self):
        plan = parse_lottery_rule("四连参与抽奖，评论区抽一位送同款", "xiaohongshu")

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            ["followed", "liked", "commented", "favorited"],
            plan["required_actions"],
        )
        self.assertFalse(plan["review_required"])

    def test_favorite_is_a_required_action(self):
        plan = parse_lottery_rule("抽奖时间到：关注+点赞+收藏+评论，评论区抽1位送同款", "xiaohongshu")

        self.assertTrue(plan["is_lottery"])
        self.assertIn("favorited", plan["required_actions"])
        self.assertNotIn("favorited", plan["unsupported_actions"])

    def test_participation_context_recognizes_emoji_action_subset(self):
        plan = parse_lottery_rule(
            "抽奖福利，参与方式：👍+💬+⭐，抽1位送同款",
            "xiaohongshu",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            ["liked", "commented", "favorited"],
            plan["required_actions"],
        )
        self.assertFalse(plan["review_required"])

    def test_action_emoji_outside_participation_context_are_not_actions(self):
        plan = parse_lottery_rule(
            "抽奖福利，奖品太喜欢啦👍，评论区抽1位送同款⭐",
            "xiaohongshu",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(["commented"], plan["required_actions"])
        self.assertNotIn("liked", plan["required_actions"])
        self.assertNotIn("favorited", plan["required_actions"])

    def test_share_is_unresolved_and_never_substitutes_for_favorite(self):
        plan = parse_lottery_rule(
            "抽奖：关注、点赞、评论、收藏并分享本篇笔记",
            "xiaohongshu",
        )

        self.assertEqual(
            ["followed", "liked", "commented", "favorited"],
            plan["required_actions"],
        )
        self.assertIn("reposted", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

    def test_flags_required_comment_text_and_friend_mention_as_unsupported(self):
        plan = parse_lottery_rule(
            "抽奖福利：关注点赞，评论指定口令“小红书夏日”并@两位好友。",
            "xiaohongshu",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("comment_content", plan["unsupported_actions"])
        self.assertIn("mention_account", plan["unsupported_actions"])
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

    def test_favorite_and_repost_are_not_interchangeable(self):
        favorite = parse_lottery_rule(
            "抽奖福利：关注+点赞+评论+收藏本视频，抽2位粉丝", "douyin"
        )
        repost = parse_lottery_rule(
            "抽奖福利：关注+点赞+评论+转发本视频，抽2位粉丝", "douyin"
        )

        self.assertIn("favorited", favorite["required_actions"])
        self.assertNotIn("reposted", favorite["required_actions"])
        self.assertIn("reposted", repost["required_actions"])
        self.assertNotIn("favorited", repost["required_actions"])

        both = parse_lottery_rule(
            "抽奖福利：关注+点赞+评论+收藏本视频并转发本视频，抽2位粉丝",
            "douyin",
        )
        self.assertIn("favorited", both["required_actions"])
        self.assertIn("reposted", both["required_actions"])

    def test_comment_word_share_does_not_invent_repost_action(self):
        plan = parse_lottery_rule(
            "抽奖福利：关注点赞，评论区分享你的夏日故事。", "douyin"
        )

        self.assertIn("commented", plan["required_actions"])
        self.assertNotIn("reposted", plan["required_actions"])
        self.assertIn("comment_content", plan["unsupported_actions"])

    def test_topic_and_mention_are_bound_to_comment_requirements(self):
        plan = parse_lottery_rule(
            "抽奖：关注点赞收藏，评论带#夏日好物#并@品牌官方。", "douyin"
        )

        self.assertEqual(
            ["#夏日好物#"], plan["content_requirements"]["commented"]["topic_tags"]
        )
        self.assertEqual(
            ["@品牌官方"], plan["content_requirements"]["commented"]["mentions"]
        )
        self.assertTrue(plan["review_required"])

    def test_marks_ambiguous_rule_for_review(self):
        plan = parse_lottery_rule("抽奖：评论区留言即可，关注点赞可选。", "douyin")

        self.assertTrue(plan["is_lottery"])
        self.assertTrue(plan["review_required"])
        self.assertTrue(plan["ambiguity_patterns"])

    def test_flags_media_and_custom_comment_requirements_as_unsupported(self):
        plan = parse_lottery_rule(
            "抽奖福利：关注点赞转发，晒出宠物视频并在评论区留下你的作品链接。",
            "douyin",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("media_submission", plan["unsupported_actions"])
        self.assertIn("comment_content", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])

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

    def test_unknown_platform_cannot_be_auto_approved(self):
        plan = parse_lottery_rule(
            "抽奖：关注并转评赞本条动态",
            "unknown_platform",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertEqual(
            {"followed", "liked", "commented", "reposted"},
            set(plan["required_actions"]),
        )
        self.assertTrue(plan["review_required"])

    def test_case_variant_keeps_bilibili_content_blockers(self):
        plan = parse_lottery_rule(
            "抽奖：关注并转评赞本条动态，评论带上#ASUS翻转夏日#。",
            " BILIBILI ",
        )

        self.assertTrue(plan["is_lottery"])
        self.assertIn("topic_tag", plan["unsupported_actions"])
        self.assertTrue(plan["review_required"])


if __name__ == "__main__":
    unittest.main()
