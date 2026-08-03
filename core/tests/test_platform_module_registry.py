from dataclasses import replace
import sys
from types import MappingProxyType, ModuleType
import unittest
from unittest.mock import patch

from app.action_plan import (
    action_order_for_platform,
    default_execution_path_for_platform,
)
from app.platform_modules import (
    PLATFORM_REGISTRY,
    PlatformCapabilityError,
    PlatformPolicyConflict,
    PlatformRegistry,
    get_platform_module,
)
from app.platform_modules.base import (
    ExecutionPathMetadata,
    LotteryTargetValidation,
)
from app.utils.lottery_targets import validate_lottery_target


class PlatformModuleRegistryTests(unittest.TestCase):
    def test_four_platforms_have_independent_business_descriptors(self):
        self.assertEqual(
            ("bilibili", "weibo", "douyin", "xiaohongshu"),
            tuple(PLATFORM_REGISTRY),
        )
        modules = list(PLATFORM_REGISTRY.values())
        self.assertEqual(4, len({id(module) for module in modules}))
        self.assertEqual(4, len({id(module.execution_paths) for module in modules}))

    def test_only_bilibili_accepts_keyword_and_up_discovery(self):
        bilibili = get_platform_module("bilibili")
        self.assertEqual(
            frozenset({"url_list", "keyword", "up"}),
            bilibili.discovery_source_types,
        )
        for platform in ("weibo", "xiaohongshu", "douyin"):
            module = get_platform_module(platform)
            self.assertEqual(frozenset({"url_list"}), module.discovery_source_types)
            for source_type in ("keyword", "up"):
                with self.subTest(platform=platform, source_type=source_type):
                    with self.assertRaises(PlatformCapabilityError) as caught:
                        module.validate_discovery_source(source_type)
                    self.assertEqual(
                        "platform_discovery_source_type_not_supported",
                        caught.exception.code,
                    )
                    self.assertEqual(("url_list",), caught.exception.allowed)

    def test_discovery_source_value_rejects_whitespace_only_input(self):
        module = get_platform_module("weibo")
        with self.assertRaises(PlatformCapabilityError) as caught:
            module.validate_discovery_source_value(" \t\r\n ")
        self.assertEqual(
            "platform_discovery_source_value_required",
            caught.exception.code,
        )
        self.assertEqual("source_value", caught.exception.capability)
        self.assertEqual(
            "https://weibo.com/123/status",
            module.validate_discovery_source_value(
                "  https://weibo.com/123/status  "
            ),
        )

    def test_discovery_source_config_is_validated_before_persistence(self):
        bilibili = get_platform_module("bilibili")
        invalid_values = (
            ("up", "not-a-number", "bilibili_discovery_up_uid_invalid"),
            ("up", "0", "bilibili_discovery_up_uid_invalid"),
            (
                "keyword",
                "长" * 65,
                "bilibili_discovery_keyword_invalid",
            ),
            (
                "keyword",
                f"ok,{'长' * 65}",
                "bilibili_discovery_keyword_invalid",
            ),
            (
                "keyword",
                ",".join(f"keyword-{index}" for index in range(9)),
                "bilibili_discovery_keyword_invalid",
            ),
        )
        for source_type, source_value, expected_code in invalid_values:
            with self.subTest(source_type=source_type, source_value=source_value):
                with self.assertRaises(PlatformCapabilityError) as caught:
                    bilibili.validate_discovery_source_config(
                        source_type,
                        source_value,
                    )
                self.assertEqual(expected_code, caught.exception.code)

        self.assertEqual(
            ("up", "123456"),
            bilibili.validate_discovery_source_config("up", " 123456 "),
        )
        self.assertEqual(
            ("keyword", "抽奖,giveaway"),
            bilibili.validate_discovery_source_config(
                "keyword",
                " 抽奖,giveaway ",
            ),
        )

    def test_url_list_requires_a_target_owned_by_that_platform(self):
        valid_urls = {
            "bilibili": "https://t.bilibili.com/123456789",
            "weibo": "https://weibo.com/detail/4890123456789012",
            "xiaohongshu": (
                "https://www.xiaohongshu.com/explore/"
                "64f1a2b3c4d5e6f7a8b9c0d1"
            ),
            "douyin": "https://www.douyin.com/video/7300000000000000000",
        }
        for platform, valid_url in valid_urls.items():
            module = get_platform_module(platform)
            with self.subTest(platform=platform):
                self.assertEqual(
                    ("url_list", valid_url),
                    module.validate_discovery_source_config("url_list", valid_url),
                )
                with self.assertRaises(PlatformCapabilityError) as caught:
                    module.validate_discovery_source_config(
                        "url_list",
                        "https://example.test/not-a-platform-target",
                    )
                self.assertEqual(
                    "platform_discovery_url_list_target_required",
                    caught.exception.code,
                )
                with self.assertRaises(PlatformCapabilityError) as mixed:
                    module.validate_discovery_source_config(
                        "url_list",
                        f"{valid_url}\nhttps://example.test/foreign-target",
                    )
                self.assertEqual(
                    "platform_discovery_url_list_target_required",
                    mixed.exception.code,
                )

    def test_action_plan_reads_each_target_platform_descriptor(self):
        for platform, module in PLATFORM_REGISTRY.items():
            with self.subTest(platform=platform):
                self.assertEqual(
                    module.action_order,
                    action_order_for_platform(platform),
                )
                self.assertEqual(
                    module.default_execution_path_id,
                    default_execution_path_for_platform(platform),
                )

    def test_execution_blocker_strings_remain_compatible(self):
        self.assertEqual(
            [],
            get_platform_module("bilibili").execution_path_blockers(
                "bilibili_api_v2"
            ),
        )
        self.assertEqual(
            ["weibo_manual_execution_selected"],
            get_platform_module("weibo").execution_path_blockers(
                "weibo_manual_v1"
            ),
        )
        self.assertEqual(
            [],
            get_platform_module("xiaohongshu").execution_path_blockers(
                "xiaohongshu_browser_v1"
            ),
        )
        self.assertEqual(
            ["xiaohongshu_manual_execution_selected"],
            get_platform_module("xiaohongshu").execution_path_blockers(
                "xiaohongshu_manual_v1"
            ),
        )
        self.assertEqual(
            ["douyin_execution_path_invalid"],
            get_platform_module("douyin").execution_path_blockers("invalid"),
        )
        self.assertIsNone(
            get_platform_module("xiaohongshu").non_executable_error(
                "foreign_path"
            )
        )
        self.assertEqual(
            "douyin_manual_plan_must_be_non_executable",
            get_platform_module("douyin").non_executable_error(
                "douyin_manual_v1"
            ),
        )
        self.assertIsNone(
            get_platform_module("weibo").non_executable_error("foreign_path")
        )

    def test_weibo_shadow_changes_only_the_account_execution_channel(self):
        module = get_platform_module("weibo")
        self.assertEqual(
            "weibo_manual_v1",
            module.account_execution_path_for_dispatch(
                task_mode="shadow_run",
                stored_execution_path="weibo_oauth_v1",
            ),
        )
        self.assertEqual(
            "weibo_oauth_v1",
            module.account_execution_path_for_dispatch(
                task_mode="real_run",
                stored_execution_path="weibo_oauth_v1",
            ),
        )
        self.assertEqual(
            "weibo_oauth",
            module.execution_path_map["weibo_oauth_v1"].credential_kind,
        )

    def test_weibo_dispatch_policies_are_module_owned_callbacks(self):
        module = get_platform_module("weibo")
        self.assertTrue(
            module.requires_public_ingress(required_actions={"commented"})
        )
        self.assertFalse(
            module.requires_public_ingress(required_actions={"liked"})
        )
        self.assertEqual(
            (),
            module.account_required_actions_for_dispatch(
                required_actions=("commented",),
                task_mode="shadow_run",
            ),
        )
        self.assertEqual(
            ("commented",),
            module.account_required_actions_for_dispatch(
                required_actions=("commented",),
                task_mode="real_run",
            ),
        )
        for platform in ("bilibili", "xiaohongshu", "douyin"):
            with self.subTest(platform=platform):
                other = get_platform_module(platform)
                self.assertFalse(
                    other.requires_public_ingress(
                        required_actions={"commented"}
                    )
                )
                self.assertEqual(
                    (),
                    other.account_required_actions_for_dispatch(
                        required_actions=("commented",),
                        task_mode="real_run",
                    ),
                )

    def test_dispatch_binding_policies_are_owned_by_their_platform_modules(self):
        for platform in PLATFORM_REGISTRY:
            with self.subTest(platform=platform):
                handler = get_platform_module(platform).dispatch_plan_binding_handler
                self.assertIsNotNone(handler)
                self.assertEqual(
                    f"app.platform_modules.{platform}",
                    handler.__module__,
                )

        with self.assertRaises(PlatformPolicyConflict) as caught:
            get_platform_module("bilibili").build_dispatch_plan_binding(
                lottery={},
                task_mode="real_run",
                account={"execution_revision": 7},
            )
        self.assertEqual(
            ["action_plan_binding_invalid"],
            caught.exception.detail["blockers"],
        )

    def test_action_plan_authoring_policies_do_not_leak_between_platforms(self):
        context = {
            "required_actions": ["followed"],
            "action_payloads": {
                "followed": {"target_handle": "@brand"},
            },
            "content_requirements": {
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            },
            "friend_mention_requirements": {},
            "source_content_requirements": {
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            },
            "selected_required_actions": {"followed"},
            "payload_validation_errors": ["existing", "existing"],
        }

        for platform in ("bilibili", "weibo", "xiaohongshu", "douyin"):
            with self.subTest(platform=platform):
                content, errors = get_platform_module(
                    platform
                ).apply_action_plan_authoring_policy(**context)
                self.assertEqual(["@brand"], content["follow_targets"])
                self.assertEqual(["existing"], errors)

    def test_weibo_authoring_preserves_error_order(self):
        _, errors = get_platform_module("weibo").apply_action_plan_authoring_policy(
            required_actions=["followed"],
            action_payloads={"followed": {"target_handle": "@brand"}},
            content_requirements={
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            },
            friend_mention_requirements={
                "commented": {"mode": "exact", "count": 1}
            },
            source_content_requirements={
                "follow_targets": [],
                "commented": {"topic_tags": [], "mentions": []},
                "reposted": {"topic_tags": [], "mentions": []},
            },
            selected_required_actions={"followed"},
            payload_validation_errors=["existing"],
        )
        self.assertEqual(
            ["existing", "action_plan_friend_mention_action_missing"],
            errors,
        )

    def test_strategy_probe_and_exact_evidence_policies_are_isolated(self):
        bilibili = get_platform_module("bilibili")
        self.assertTrue(
            bilibili.strategy_target_is_real_valid(
                LotteryTargetValidation(True, kind="dynamic")
            )
        )
        self.assertFalse(
            bilibili.strategy_target_is_real_valid(
                LotteryTargetValidation(True, kind="article")
            )
        )
        self.assertEqual(
            "bilibili_dynamic_target_required",
            bilibili.strategy_target_error(
                LotteryTargetValidation(True, kind="article")
            ),
        )
        self.assertTrue(bilibili.probe_requires_plan_binding)
        self.assertIsNotNone(bilibili.exact_execution_evidence_revalidator)

        weibo = get_platform_module("weibo")
        self.assertFalse(weibo.probe_requires_plan_binding)
        self.assertIsNotNone(weibo.exact_execution_evidence_revalidator)
        xiaohongshu = get_platform_module("xiaohongshu")
        self.assertTrue(xiaohongshu.probe_requires_plan_binding)
        self.assertIsNotNone(
            xiaohongshu.exact_execution_evidence_revalidator
        )

        douyin = get_platform_module("douyin")
        self.assertTrue(douyin.probe_requires_plan_binding)
        self.assertIsNotNone(douyin.exact_execution_evidence_revalidator)
        self.assertTrue(
            douyin.strategy_target_is_real_valid(
                LotteryTargetValidation(True, kind="video")
            )
        )
        self.assertFalse(
            douyin.strategy_target_is_real_valid(
                LotteryTargetValidation(True, kind="platform-specific")
            )
        )

    def test_account_candidate_policies_are_platform_local(self):
        candidate = {
            "id": 7,
            "latest_calibration_status": "succeeded",
            "encrypted_credential": b"not-an-aad-bound-credential",
        }
        credential_helpers = ModuleType("app.utils.credential_kind")
        credential_helpers.account_credential_kind = lambda *_args: "invalid"
        credential_helpers.decrypt_weibo_oauth_credential = lambda *_args: {}
        readiness = ModuleType("app.services.real_run_readiness")
        readiness.validate_weibo_oauth_capability_attestation = (
            lambda *_args, **_kwargs: {"ready": False}
        )
        with patch.dict(
            sys.modules,
            {
                "app.utils.credential_kind": credential_helpers,
                "app.services.real_run_readiness": readiness,
            },
        ):
            self.assertFalse(
                get_platform_module("weibo").account_candidate_supports_execution(
                    row=candidate,
                    execution_path_id="weibo_oauth_v1",
                    required_actions=("liked",),
                    require_capability=False,
                )
            )
            self.assertFalse(
                get_platform_module("douyin").account_candidate_supports_execution(
                    row=candidate,
                    execution_path_id="douyin_device_v1",
                    required_actions=("liked",),
                    require_capability=True,
                )
            )
        for platform in ("bilibili", "xiaohongshu"):
            with self.subTest(platform=platform):
                self.assertTrue(
                    get_platform_module(
                        platform
                    ).account_candidate_supports_execution(
                        row=candidate,
                        execution_path_id="foreign",
                        required_actions=("liked",),
                        require_capability=True,
                    )
                )

    def test_registry_fails_closed_when_safety_hooks_are_missing(self):
        bilibili = get_platform_module("bilibili")
        with self.assertRaisesRegex(
            ValueError,
            "platform_real_run_readiness_provider_required:bilibili",
        ):
            PlatformRegistry(
                (
                    replace(bilibili, real_run_readiness_provider=None),
                )
            )
        with self.assertRaisesRegex(
            ValueError,
            "platform_exact_evidence_revalidator_required:bilibili",
        ):
            PlatformRegistry(
                (
                    replace(
                        bilibili,
                        exact_execution_evidence_revalidator=None,
                    ),
                )
            )
        with self.assertRaisesRegex(
            ValueError,
            "platform_probe_plan_binding_handler_required:bilibili",
        ):
            PlatformRegistry(
                (
                    replace(
                        bilibili,
                        probe_requires_plan_binding=True,
                        dispatch_plan_binding_handler=None,
                    ),
                )
            )
        weibo = get_platform_module("weibo")
        with self.assertRaisesRegex(
            ValueError,
            "platform_account_candidate_validator_required:weibo",
        ):
            PlatformRegistry(
                (
                    replace(weibo, account_candidate_validator=None),
                )
            )

    def test_bilibili_article_target_accepts_only_read_cv_numeric_path(self):
        accepted = validate_lottery_target(
            "bilibili",
            "https://www.bilibili.com/read/cv123456?from=search",
        )
        self.assertTrue(accepted.valid)
        self.assertEqual("article", accepted.kind)

        for invalid in (
            "https://www.bilibili.com/read/123456",
            "https://www.bilibili.com/read/CV123456",
            "https://www.bilibili.com/read/cv123456/extra",
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    validate_lottery_target("bilibili", invalid).valid
                )

    def test_execution_path_credential_kinds_match_public_account_contract(self):
        self.assertEqual(
            "browser_session",
            get_platform_module("bilibili")
            .execution_path_map["bilibili_api_v2"]
            .credential_kind,
        )
        for platform, path_id in (
            ("xiaohongshu", "xiaohongshu_browser_v1"),
            ("xiaohongshu", "xiaohongshu_manual_v1"),
            ("douyin", "douyin_manual_v1"),
        ):
            with self.subTest(platform=platform):
                self.assertEqual(
                    "browser_session",
                    get_platform_module(platform)
                    .execution_path_map[path_id]
                    .credential_kind,
                )

    def test_execution_evidence_kinds_are_owned_by_execution_paths(self):
        self.assertEqual(
            "exact_execution_evidence",
            get_platform_module("bilibili")
            .execution_path_map["bilibili_api_v2"]
            .execution_evidence_kind,
        )
        self.assertEqual(
            "oauth_account_calibration",
            get_platform_module("weibo")
            .execution_path_map["weibo_oauth_v1"]
            .execution_evidence_kind,
        )
        self.assertIsNone(
            get_platform_module("weibo")
            .execution_path_map["weibo_manual_v1"]
            .execution_evidence_kind,
        )
        self.assertEqual(
            "exact_execution_evidence",
            get_platform_module("xiaohongshu")
            .execution_path_map["xiaohongshu_browser_v1"]
            .execution_evidence_kind,
        )

    def test_external_action_names_are_normalized_by_each_platform(self):
        bilibili = get_platform_module("bilibili")
        self.assertEqual(
            ("followed", "liked", "commented", "reposted"),
            tuple(
                bilibili.normalize_external_action(action)
                for action in ("follow", "like", "comment", "repost")
            ),
        )
        self.assertEqual(
            "liked",
            bilibili.normalize_external_action("liked"),
        )
        self.assertIsNone(
            bilibili.normalize_external_action("unknown_remote_action")
        )

        weibo = get_platform_module("weibo")
        self.assertEqual(
            "liked",
            weibo.normalize_external_action("liked"),
        )
        self.assertIsNone(weibo.normalize_external_action("like"))

        xiaohongshu = get_platform_module("xiaohongshu")
        self.assertEqual(
            ("followed", "liked", "commented", "favorited"),
            tuple(
                xiaohongshu.normalize_external_action(action)
                for action in ("follow", "like", "comment", "favorite")
            ),
        )

    def test_real_path_requires_one_supported_evidence_authority(self):
        with self.assertRaisesRegex(
            ValueError,
            "execution_path_evidence_kind_invalid",
        ):
            ExecutionPathMetadata(
                path_id="unsafe_real_path",
                adapter_kind="api",
                task_modes=frozenset({"real_run"}),
                real_actions=True,
            )

    def test_shadow_selector_requirements_are_platform_owned_and_plan_scoped(self):
        self.assertEqual(
            (),
            get_platform_module("bilibili").shadow_phases_for_actions(
                ["commented"]
            ),
        )
        self.assertEqual(
            ("commented",),
            get_platform_module("weibo").shadow_phases_for_actions(
                ["followed", "commented", "reposted"]
            ),
        )
        self.assertEqual(
            ("commented",),
            get_platform_module("xiaohongshu").shadow_phases_for_actions(
                ["followed", "liked", "commented", "favorited"]
            ),
        )
        self.assertEqual(
            ("commented", "favorited"),
            get_platform_module("douyin").shadow_phases_for_actions(
                ["commented", "favorited", "reposted"]
            ),
        )
        self.assertEqual(
            ("favorited",),
            get_platform_module("weibo").missing_shadow_configured_phases(
                ["commented", "favorited", "reposted"],
                lambda phase: phase == "commented",
            ),
        )

    def test_registry_has_no_shared_mutable_capability_state(self):
        bilibili = get_platform_module("bilibili")
        weibo = get_platform_module("weibo")
        changed_bilibili = replace(
            bilibili,
            discovery_source_types=frozenset({"url_list"}),
        )
        isolated = PlatformRegistry(
            (
                changed_bilibili,
                weibo,
                get_platform_module("douyin"),
                get_platform_module("xiaohongshu"),
            )
        )

        self.assertEqual(frozenset({"url_list"}), isolated.require("bilibili").discovery_source_types)
        self.assertEqual(
            frozenset({"url_list", "keyword", "up"}),
            PLATFORM_REGISTRY.require("bilibili").discovery_source_types,
        )
        self.assertIs(weibo, isolated.require("weibo"))
        self.assertEqual(
            frozenset({"url_list"}),
            isolated.require("weibo").discovery_source_types,
        )
        with self.assertRaises(TypeError):
            bilibili.execution_path_map["unexpected"] = object()

    def test_replacing_one_platform_policy_cannot_change_another(self):
        weibo = get_platform_module("weibo")
        changed_weibo = replace(
            weibo,
            action_order=("liked",),
            shadow_required_configured_phases=frozenset(),
            shadow_phase_contracts=MappingProxyType(
                {"liked": "click_or_state"}
            ),
            max_text_utf16_units=1,
            public_ingress_requirement_handler=None,
            account_required_actions_handler=None,
        )
        isolated = PlatformRegistry(
            (
                get_platform_module("bilibili"),
                changed_weibo,
                get_platform_module("douyin"),
                get_platform_module("xiaohongshu"),
            )
        )

        self.assertEqual(("liked",), isolated.require("weibo").action_order)
        self.assertEqual(
            ("followed", "liked", "commented", "favorited"),
            isolated.require("douyin").action_order,
        )
        self.assertEqual(
            140,
            PLATFORM_REGISTRY.require("weibo").max_text_utf16_units,
        )

    def test_discovery_sessions_are_fresh_per_run(self):
        bilibili = get_platform_module("bilibili")
        first = bilibili.create_discovery_session()
        second = bilibili.create_discovery_session()

        self.assertIsNot(first, second)
        self.assertIsNot(first.expansion_budget, second.expansion_budget)
        self.assertTrue(first.expansion_budget.consume())
        self.assertEqual(
            first.expansion_budget.limit - 1,
            first.expansion_budget.remaining,
        )
        self.assertEqual(
            second.expansion_budget.limit,
            second.expansion_budget.remaining,
        )


class PlatformDiscoveryRoutingTests(unittest.IsolatedAsyncioTestCase):
    def fake_discovery_module(self):
        module = ModuleType("app.services.discovery")
        module.extract_urls = lambda value: value.split()

        async def fetch_up_dynamics(value):
            return [{"raw_url": f"https://t.bilibili.com/{value}"}]

        async def fetch_keyword_dynamics(value, *, search_budget=None):
            return [
                {
                    "raw_url": f"https://t.bilibili.com/{value}",
                    "budget": search_budget,
                }
            ]

        module.fetch_up_dynamics = fetch_up_dynamics
        module.fetch_keyword_dynamics = fetch_keyword_dynamics
        return module

    async def test_url_list_is_routed_within_each_platform_capability(self):
        fake_discovery = self.fake_discovery_module()
        with patch.dict(sys.modules, {"app.services.discovery": fake_discovery}):
            for platform in PLATFORM_REGISTRY:
                with self.subTest(platform=platform):
                    candidates = await get_platform_module(
                        platform
                    ).fetch_discovery_candidates(
                        {
                            "platform": platform,
                            "source_type": "url_list",
                            "source_value": "https://example.test/one https://example.test/two",
                        }
                    )
                    self.assertEqual(2, len(candidates))

    async def test_bilibili_owns_keyword_and_up_handlers(self):
        fake_discovery = self.fake_discovery_module()
        bilibili = get_platform_module("bilibili")
        with patch.dict(sys.modules, {"app.services.discovery": fake_discovery}):
            keyword = await bilibili.fetch_discovery_candidates(
                {
                    "platform": "bilibili",
                    "source_type": "keyword",
                    "source_value": "giveaway",
                },
                keyword_search_budget="budget-token",
            )
            up = await bilibili.fetch_discovery_candidates(
                {
                    "platform": "bilibili",
                    "source_type": "up",
                    "source_value": "12345",
                }
            )

        self.assertEqual("budget-token", keyword[0]["budget"])
        self.assertEqual(
            "https://t.bilibili.com/12345",
            up[0]["raw_url"],
        )


if __name__ == "__main__":
    unittest.main()
