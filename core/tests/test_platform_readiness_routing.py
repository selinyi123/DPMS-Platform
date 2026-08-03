import base64
import json
import os
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.platform_modules import (  # noqa: E402
    PLATFORM_REGISTRY,
    PlatformModuleUnavailableError,
    PlatformPolicyConflict,
    get_platform_module,
)
from app.services import real_run_readiness  # noqa: E402


class PlatformReadinessProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_xiaohongshu_shadow_evidence_accepts_only_canonical_subsets(self):
        payload = {
            "qualified": False,
            "side_effects": False,
            "selector_observation_complete": True,
            "manual_confirmation_required": True,
            "real_run_capable": False,
            "capability_block_reason": (
                "xiaohongshu_no_official_interaction_api"
            ),
            "required_phases": ["liked", "commented", "favorited"],
            "visible_phases": {
                "liked": "button.like",
                "commented": {
                    "input": "textarea.comment",
                    "submit": "button.send",
                },
                "favorited": "button.favorite",
            },
            "screenshot_path": "/profiles/xiaohongshu/shadow-runs/task.png",
        }

        qualifies = (
            real_run_readiness.qualified_xiaohongshu_manual_shadow_observation
        )
        self.assertTrue(qualifies(payload))
        for phases in (
            [],
            ["commented", "liked"],
            ["liked", "reposted"],
        ):
            with self.subTest(phases=phases):
                invalid = dict(payload, required_phases=phases)
                self.assertFalse(qualifies(invalid))

    async def test_bilibili_locked_dispatch_rechecks_materialized_risk(self):
        from app.platform_modules import bilibili
        from app.services import bilibili_preflight_evidence

        evidence = AsyncMock(return_value={"id": "evidence-1"})
        risk = AsyncMock(return_value={"has_recent_risk": False})
        with (
            patch.object(
                bilibili_preflight_evidence,
                "extract_bilibili_dynamic_id",
                return_value="123456789",
            ),
            patch.object(
                real_run_readiness,
                "load_exact_bilibili_execution_evidence",
                evidence,
            ),
            patch.object(
                real_run_readiness,
                "recent_account_risk",
                risk,
            ),
        ):
            await bilibili.revalidate_exact_execution_evidence(
                lottery={
                    "canonical_url": (
                        "canonical://bilibili/dynamic/123456789"
                    ),
                    "raw_url": "https://t.bilibili.com/123456789",
                },
                lottery_id=7,
                account={"id": 9},
                plan_binding={
                    "rule_snapshot_id": 1,
                    "execution_path_id": "bilibili_api_v2",
                    "target_hash": "a" * 64,
                    "rule_hash": "b" * 64,
                    "action_plan_hash": "c" * 64,
                    "config_hash": "d" * 64,
                    "required_actions": ("liked",),
                    "execution_revision": 2,
                    "follow_target_handle": None,
                },
                execution_evidence_id="evidence-1",
            )

        risk.assert_awaited_once_with(9, for_update=True)

    async def test_bilibili_locked_dispatch_rejects_new_account_risk(self):
        from app.platform_modules import bilibili
        from app.services import bilibili_preflight_evidence

        with (
            patch.object(
                bilibili_preflight_evidence,
                "extract_bilibili_dynamic_id",
                return_value="123456789",
            ),
            patch.object(
                real_run_readiness,
                "load_exact_bilibili_execution_evidence",
                new=AsyncMock(return_value={"id": "evidence-1"}),
            ),
            patch.object(
                real_run_readiness,
                "recent_account_risk",
                new=AsyncMock(return_value={"has_recent_risk": True}),
            ),
        ):
            with self.assertRaises(PlatformPolicyConflict) as caught:
                await bilibili.revalidate_exact_execution_evidence(
                    lottery={
                        "canonical_url": (
                            "canonical://bilibili/dynamic/123456789"
                        ),
                        "raw_url": "https://t.bilibili.com/123456789",
                    },
                    lottery_id=7,
                    account={"id": 9},
                    plan_binding={
                        "rule_snapshot_id": 1,
                        "execution_path_id": "bilibili_api_v2",
                        "target_hash": "a" * 64,
                        "rule_hash": "b" * 64,
                        "action_plan_hash": "c" * 64,
                        "config_hash": "d" * 64,
                        "required_actions": ("liked",),
                        "execution_revision": 2,
                        "follow_target_handle": None,
                    },
                    execution_evidence_id="evidence-1",
                )

        self.assertEqual(
            caught.exception.detail["blockers"],
            ["recent_account_risk_event"],
        )

    async def test_unknown_historical_platform_is_a_local_fail_closed_gate(self):
        result = await real_run_readiness.real_run_gate_status(
            {
                "id": 91,
                "platform": "legacy-platform",
                "status": "pending",
                "raw_url": "https://legacy.example/lottery/91",
                "action_plan": None,
            },
            selector_config={},
            real_run_enabled=True,
        )

        self.assertFalse(result["allowed"])
        self.assertFalse(result["target_valid"])
        self.assertEqual(["unsupported_platform"], result["blockers"])
        self.assertEqual("blocked", result["next_action"])
        self.assertFalse(result["real_run_supported"])

    async def test_unavailable_module_still_raises_on_single_target_authority(self):
        lottery = {
            "id": 92,
            "platform": "bilibili",
            "status": "pending",
            "raw_url": "https://www.bilibili.com/opus/123",
            "action_plan": None,
        }
        with patch.object(
            real_run_readiness,
            "get_platform_module",
            side_effect=PlatformModuleUnavailableError("bilibili"),
        ):
            with self.assertRaises(PlatformModuleUnavailableError):
                await real_run_readiness.validate_real_run_evidence(
                    lottery,
                    for_update=True,
                )

    async def test_provider_import_failure_is_a_local_read_gate(self):
        lottery = {
            "id": 93,
            "platform": "bilibili",
            "status": "pending",
            "raw_url": "https://www.bilibili.com/opus/123",
            "action_plan": None,
        }
        with patch.object(
            real_run_readiness,
            "validate_real_run_evidence",
            new=AsyncMock(side_effect=ImportError("provider unavailable")),
        ):
            result = await real_run_readiness.real_run_gate_status(
                lottery,
                selector_config={},
                real_run_enabled=True,
                account_summary={
                    "ready_accounts": 1,
                    "runnable_accounts": 1,
                    "latest_recent_risk": None,
                },
            )

        self.assertFalse(result["allowed"])
        self.assertEqual(
            ["platform_module_unavailable"],
            result["blockers"],
        )
        self.assertEqual("blocked", result["next_action"])

    async def test_each_platform_descriptor_selects_only_its_own_validator(self):
        calls = []
        manual_calls = []
        readiness = ModuleType("app.services.real_run_readiness")

        async def bilibili(lottery, account_id):
            calls.append(("bilibili", account_id, None))
            return {"provider": "bilibili"}

        async def manual(
            lottery,
            account_id=None,
            *,
            platform,
            execution_path_id,
            capability_blocker,
            evidence_batch=None,
            **policies,
        ):
            manual_calls.append(
                {
                    "platform": platform,
                    "account_id": account_id,
                    "evidence_batch": evidence_batch,
                    "execution_path_id": execution_path_id,
                    "capability_blocker": capability_blocker,
                    **policies,
                }
            )
            provider = "weibo_manual" if platform == "weibo" else platform
            return {"provider": provider}

        async def weibo_oauth(
            lottery,
            account_id=None,
            evidence_batch=None,
            for_update=False,
        ):
            calls.append(("weibo_oauth", account_id, for_update))
            return {"provider": "weibo_oauth"}

        async def xiaohongshu_browser(
            lottery,
            account_id=None,
            evidence_batch=None,
        ):
            calls.append(
                ("xiaohongshu_browser", account_id, evidence_batch)
            )
            return {"provider": "xiaohongshu_browser"}

        async def douyin_device(
            lottery,
            account_id=None,
            evidence_batch=None,
        ):
            calls.append(("douyin_device", account_id, evidence_batch))
            return {"provider": "douyin"}

        readiness.validate_bilibili_v2_evidence = bilibili
        readiness.validate_manual_only_contract = manual
        readiness.validate_weibo_oauth_contract = weibo_oauth
        readiness.validate_xiaohongshu_browser_contract = (
            xiaohongshu_browser
        )
        readiness.validate_douyin_device_contract = douyin_device

        with patch.dict(
            sys.modules,
            {"app.services.real_run_readiness": readiness},
        ):
            self.assertEqual(
                {"provider": "bilibili"},
                await get_platform_module("bilibili").validate_real_run_readiness(
                    lottery={}, account_id=11
                ),
            )
            self.assertEqual(
                {"provider": "xiaohongshu_browser"},
                await get_platform_module(
                    "xiaohongshu"
                ).validate_real_run_readiness(
                    lottery={}, account_id=12, evidence_batch="xhs-batch"
                ),
            )
            self.assertEqual(
                {"provider": "xiaohongshu"},
                await get_platform_module(
                    "xiaohongshu"
                ).validate_real_run_readiness(
                    lottery={
                        "action_plan": {
                            "execution_path_id": "xiaohongshu_manual_v1"
                        }
                    },
                    account_id=16,
                    evidence_batch="xhs-manual-batch",
                ),
            )
            self.assertEqual(
                {"provider": "douyin"},
                await get_platform_module("douyin").validate_real_run_readiness(
                    lottery={}, account_id=13, evidence_batch="douyin-batch"
                ),
            )
            self.assertEqual(
                {"provider": "weibo_manual"},
                await get_platform_module("weibo").validate_real_run_readiness(
                    lottery={
                        "action_plan": json.dumps(
                            {"execution_path_id": "weibo_manual_v1"}
                        )
                    },
                    account_id=14,
                    evidence_batch="weibo-batch",
                    for_update=True,
                ),
            )
            self.assertEqual(
                {"provider": "weibo_oauth"},
                await get_platform_module("weibo").validate_real_run_readiness(
                    lottery={
                        "action_plan": {
                            "execution_path_id": "weibo_oauth_v1"
                        }
                    },
                    account_id=15,
                    for_update=True,
                ),
            )

        self.assertEqual(
            [
                ("bilibili", 11, None),
                ("xiaohongshu_browser", 12, "xhs-batch"),
                ("douyin_device", 13, "douyin-batch"),
                ("weibo_oauth", 15, True),
            ],
            calls,
        )
        self.assertEqual(
            [
                {
                    "platform": "xiaohongshu",
                    "account_id": 16,
                    "evidence_batch": "xhs-manual-batch",
                    "execution_path_id": "xiaohongshu_manual_v1",
                    "capability_blocker": (
                        "xiaohongshu_manual_execution_selected"
                    ),
                    "execution_path_blocker": (
                        "xiaohongshu_execution_path_not_supported"
                    ),
                },
                {
                    "platform": "weibo",
                    "account_id": 14,
                    "evidence_batch": "weibo-batch",
                    "execution_path_id": "weibo_manual_v1",
                    "capability_blocker": "weibo_manual_execution_selected",
                    "execution_path_blocker": "weibo_execution_path_invalid",
                    "media_capability_blocker": (
                        "weibo_media_submission_unsupported"
                    ),
                },
            ],
            manual_calls,
        )

    async def test_central_readiness_delegates_without_platform_branching(self):
        expected = {"allowed": False, "blockers": ["provider-result"]}
        provider = AsyncMock(return_value=expected)
        module = SimpleNamespace(validate_real_run_readiness=provider)
        with patch.object(
            real_run_readiness,
            "get_platform_module",
            return_value=module,
        ) as get_module:
            result = await real_run_readiness.validate_real_run_evidence(
                {"platform": "future-api-platform"},
                account_id=21,
                for_update=True,
            )

        self.assertIs(expected, result)
        get_module.assert_called_once_with("future-api-platform")
        provider.assert_awaited_once_with(
            lottery={"platform": "future-api-platform"},
            account_id=21,
            evidence_batch=None,
            for_update=True,
        )

    async def test_weibo_for_update_locks_account_and_latest_calibration(self):
        database = SimpleNamespace(fetch_one=AsyncMock(return_value=None))
        base_result = {
            "allowed": False,
            "blockers": [],
            "action_plan_ready": False,
            "account_risk": None,
        }
        with (
            patch.object(real_run_readiness, "database", database),
            patch.object(
                real_run_readiness,
                "validate_manual_only_contract",
                new=AsyncMock(return_value=base_result),
            ),
        ):
            await real_run_readiness.validate_weibo_oauth_contract(
                {"id": 1, "action_plan": {}},
                account_id=9,
                for_update=True,
            )

        query = database.fetch_one.await_args.args[0]
        self.assertIn("FROM accounts a", query)
        self.assertIn("LEFT JOIN account_calibrations c", query)
        self.assertTrue(query.rstrip().endswith("LIMIT 1\n                 FOR UPDATE"))

    async def test_weibo_for_update_locks_dry_run_and_lease_evidence(self):
        account = {
            "id": 9,
            "status": "ready",
            "execution_revision": 3,
            "encrypted_credential": b"encrypted",
            "calibration_id": "cal-1",
            "calibration_status": "succeeded",
            "calibration_result": "{}",
            "calibration_fresh": 1,
        }

        async def fetch_one(query, values=None):
            if "FROM accounts a" in query:
                return account
            if "FROM task_runs tr" in query:
                return {"task_id": "dry-run-1"}
            raise AssertionError(query)

        database = SimpleNamespace(fetch_one=AsyncMock(side_effect=fetch_one))
        plan = SimpleNamespace(
            required_actions=("liked",),
            runtime_capability_requirements={},
            rule_snapshot_id=4,
            rule_hash="r" * 64,
            plan_hash="p" * 64,
        )
        base_result = {
            "allowed": False,
            "blockers": [],
            "action_plan_ready": True,
            "account_risk": None,
        }
        with (
            patch.object(real_run_readiness, "database", database),
            patch.object(
                real_run_readiness,
                "validate_manual_only_contract",
                new=AsyncMock(return_value=base_result),
            ),
            patch.object(
                real_run_readiness,
                "validate_action_plan_v2",
                return_value=plan,
            ),
            patch.object(
                real_run_readiness,
                "parse_weibo_oauth_credential",
                return_value={"uid": "123"},
            ),
            patch.object(
                real_run_readiness.cookie_vault,
                "decrypt_strict",
                return_value=b"credential",
                create=True,
            ),
            patch.object(
                real_run_readiness,
                "validate_weibo_oauth_capability_attestation",
                return_value={
                    "ready": True,
                    "blockers": [],
                    "denied_actions": [],
                    "evidence": {"calibration_id": "cal-1"},
                },
            ),
            patch.object(
                real_run_readiness,
                "recent_account_risk",
                new=AsyncMock(
                    return_value={
                        "has_recent_risk": False,
                        "latest_recent_risk": None,
                    }
                ),
            ) as risk,
        ):
            await real_run_readiness.validate_weibo_oauth_contract(
                {
                    "id": 1,
                    "canonical_url": "https://weibo.com/123/AbCdEf",
                    "action_plan": {},
                },
                account_id=9,
                for_update=True,
            )

        dry_run_query = database.fetch_one.await_args_list[1].args[0]
        self.assertIn("JOIN account_operation_leases lease", dry_run_query)
        self.assertTrue(
            dry_run_query.rstrip().endswith("LIMIT 1\n                                 FOR UPDATE")
        )
        risk.assert_awaited_once_with(9, for_update=True)

    async def test_for_update_risk_check_locks_the_account_event_range(self):
        database = SimpleNamespace(fetch_one=AsyncMock(return_value=None))
        with patch.object(real_run_readiness, "database", database):
            result = await real_run_readiness.recent_account_risk(
                9,
                now=datetime(2026, 7, 23, 12, 0, 0),
                for_update=True,
            )

        self.assertFalse(result["has_recent_risk"])
        query = database.fetch_one.await_args.args[0]
        self.assertIn("FROM account_active_risk_states", query)
        self.assertIn("JOIN risk_events", query)
        self.assertTrue(query.rstrip().endswith("FOR UPDATE"))

    async def test_api_adapter_label_does_not_impose_bilibili_target_kind(self):
        evidence = {
            "blockers": [],
            "account_risk": None,
            "probe_ready": False,
            "shadow_ready": False,
            "action_plan_ready": True,
        }
        with (
            patch.object(
                real_run_readiness,
                "validate_real_run_evidence",
                new=AsyncMock(return_value=evidence),
            ),
            patch.object(
                real_run_readiness,
                "platform_has_api_real_adapter",
                return_value=True,
            ),
        ):
            result = await real_run_readiness.real_run_gate_status(
                {
                    "id": 2,
                    "platform": "douyin",
                    "status": "pending",
                    "raw_url": "https://www.douyin.com/video/1234567890",
                    "action_plan": {},
                },
                selector_config={},
                real_run_enabled=True,
                account_summary={
                    "ready_accounts": 1,
                    "runnable_accounts": 1,
                    "latest_recent_risk": None,
                },
            )

        self.assertTrue(result["target_valid"])
        self.assertEqual("video", result["target_kind"])
        self.assertIsNone(result["target_error"])

    def test_all_registered_platforms_publish_readiness_providers(self):
        for platform, module in PLATFORM_REGISTRY.items():
            with self.subTest(platform=platform):
                self.assertIsNotNone(module.real_run_readiness_provider)
                self.assertEqual(
                    f"app.platform_modules.{platform}",
                    module.real_run_readiness_provider.__module__,
                )


if __name__ == "__main__":
    unittest.main()
