"""Boot and fault-domain contracts for Core platform modules."""

from types import ModuleType
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

from app.api import lotteries
from app.platform_modules import (
    LazyPlatformRegistry,
    PlatformModuleUnavailableError,
    get_platform_module,
)
from app.platform_modules.catalog import PLATFORM_MODULE_SPECS
from app.services import real_run_readiness


class LazyPlatformModuleTests(unittest.TestCase):
    def test_core_startup_import_does_not_load_platform_runtimes(self):
        runtime_modules = (
            "app.platform_modules.bilibili",
            "app.platform_modules.weibo",
            "app.platform_modules.douyin",
            "app.platform_modules.xiaohongshu",
            "app.services.bilibili_discovery",
            "app.services.bilibili_preflight_evidence",
            "app.services.bilibili_qr",
        )
        script = (
            "import sys; import app.main; "
            f"names={runtime_modules!r}; "
            "loaded=[name for name in names if name in sys.modules]; "
            "raise SystemExit('unexpected eager modules: '+repr(loaded) "
            "if loaded else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )

    def test_enumeration_does_not_import_runtime_modules(self):
        registry = LazyPlatformRegistry(PLATFORM_MODULE_SPECS)
        with patch(
            "app.platform_modules.registry.importlib.import_module"
        ) as importer:
            self.assertEqual(
                tuple(registry),
                ("bilibili", "weibo", "douyin", "xiaohongshu"),
            )
            self.assertEqual(len(registry), 4)
            importer.assert_not_called()

    def test_one_import_failure_is_cached_and_does_not_poison_peer(self):
        weibo = get_platform_module("weibo")
        fake_weibo_module = ModuleType("app.platform_modules.weibo")
        fake_weibo_module.WEIBO_PLATFORM = weibo
        calls: list[str] = []

        def import_one(module_name: str):
            calls.append(module_name)
            if module_name == "app.platform_modules.bilibili":
                raise ImportError("synthetic bilibili failure")
            if module_name == "app.platform_modules.weibo":
                return fake_weibo_module
            raise AssertionError(module_name)

        registry = LazyPlatformRegistry(
            {
                "bilibili": PLATFORM_MODULE_SPECS["bilibili"],
                "weibo": PLATFORM_MODULE_SPECS["weibo"],
            }
        )
        with patch(
            "app.platform_modules.registry.importlib.import_module",
            side_effect=import_one,
        ):
            for _ in range(2):
                with self.assertRaises(PlatformModuleUnavailableError) as caught:
                    registry.require("bilibili")
                self.assertEqual(caught.exception.platform, "bilibili")
            self.assertIs(registry.require("weibo"), weibo)

        self.assertEqual(
            calls.count("app.platform_modules.bilibili"),
            1,
        )
        self.assertEqual(calls.count("app.platform_modules.weibo"), 1)


class CrossPlatformReadIsolationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _module_loader_with_bilibili_failure():
        modules = {
            platform: get_platform_module(platform)
            for platform in PLATFORM_MODULE_SPECS
        }

        def load(platform):
            if platform == "bilibili":
                raise PlatformModuleUnavailableError("bilibili")
            return modules.get(platform)

        return load

    async def test_adapter_lists_keep_weibo_when_bilibili_module_fails(self):
        load = self._module_loader_with_bilibili_failure()
        with (
            patch.object(
                lotteries,
                "load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                lotteries,
                "get_platform_module",
                side_effect=load,
            ),
        ):
            adapters = await lotteries.list_adapters()
            config = await lotteries.get_adapter_config_status()

        by_platform = {item["platform"]: item for item in adapters}
        self.assertFalse(by_platform["bilibili"]["module_available"])
        self.assertFalse(by_platform["bilibili"]["real_actions"])
        self.assertEqual(
            by_platform["bilibili"]["adapter_status"],
            "module_unavailable",
        )
        self.assertTrue(by_platform["weibo"]["module_available"])
        self.assertEqual(by_platform["weibo"]["phases"][0], "followed")

        config_by_platform = {
            item["platform"]: item for item in config["platforms"]
        }
        self.assertFalse(config_by_platform["bilibili"]["module_available"])
        self.assertFalse(config_by_platform["bilibili"]["configured"])
        self.assertTrue(config_by_platform["weibo"]["module_available"])

    async def test_source_list_marks_only_failed_platform_unavailable(self):
        load = self._module_loader_with_bilibili_failure()
        rows = [
            {
                "id": 1,
                "platform": "bilibili",
                "source_type": "url_list",
                "source_value": "https://t.bilibili.com/123",
                "active": 1,
            },
            {
                "id": 2,
                "platform": "weibo",
                "source_type": "url_list",
                "source_value": "https://weibo.com/detail/PCAGRFqKj",
                "active": 1,
            },
        ]
        with (
            patch.object(
                lotteries.database,
                "fetch_all",
                new=AsyncMock(return_value=rows),
            ),
            patch.object(
                lotteries,
                "get_platform_module",
                side_effect=load,
            ),
        ):
            sources = await lotteries.list_tracked_sources()

        by_platform = {item["platform"]: item for item in sources}
        self.assertFalse(by_platform["bilibili"]["effective_active"])
        self.assertEqual(
            by_platform["bilibili"]["validation_error"],
            "platform_module_unavailable",
        )
        self.assertTrue(by_platform["weibo"]["effective_active"])
        self.assertIsNone(by_platform["weibo"]["validation_error"])

    async def test_evidence_list_keeps_weibo_when_bilibili_module_fails(self):
        class LotteryDatabase:
            async def fetch_all(self, query, values=None):
                if "SELECT * FROM lotteries" in query:
                    return [
                        {
                            "id": 1,
                            "platform": "bilibili",
                            "status": "pending",
                            "raw_url": "https://www.bilibili.com/opus/123",
                            "action_plan": None,
                            "execution_lock": None,
                        },
                        {
                            "id": 2,
                            "platform": "weibo",
                            "status": "pending",
                            "raw_url": (
                                "https://weibo.com/detail/PCAGRFqKj"
                            ),
                            "action_plan": None,
                            "execution_lock": None,
                        },
                    ]
                raise AssertionError(f"Unexpected query: {query}")

        load = self._module_loader_with_bilibili_failure()
        unavailable_blocker = lotteries.ExternalActionAuthorityBlocker(
            intent_id="",
            action="",
            status="unavailable",
            effect_certainty="unknown",
            outcome="",
            reason="platform_module_unavailable",
        )
        authorities = {
            1: lotteries.RealRunCompletionAuthority(
                completed_actions=(),
                blockers=(unavailable_blocker,),
            ),
            2: lotteries.RealRunCompletionAuthority(completed_actions=()),
        }
        account_summary = {
            "ready_accounts": 1,
            "runnable_accounts": 1,
            "latest_recent_risk": None,
        }
        healthy_evidence = {
            "blockers": [],
            "account_risk": None,
            "probe_ready": True,
            "shadow_ready": True,
            "action_plan_ready": True,
        }

        with (
            patch.object(lotteries, "database", LotteryDatabase()),
            patch.object(
                lotteries,
                "load_runtime_selector_config",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                lotteries,
                "is_real_run_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                lotteries,
                "load_real_run_evidence_batch",
                new=AsyncMock(return_value=object()),
            ),
            patch.object(
                lotteries,
                "real_run_account_risk_summaries",
                new=AsyncMock(
                    return_value={
                        "bilibili": account_summary,
                        "weibo": account_summary,
                    }
                ),
            ),
            patch.object(
                lotteries,
                "load_real_run_completion_authorities_for_lotteries",
                new=AsyncMock(return_value=authorities),
            ),
            patch.object(
                lotteries,
                "load_lottery_execution_intents",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                lotteries,
                "bilibili_action_ledgers_for_lotteries",
                new=AsyncMock(return_value={1: [], 2: []}),
            ),
            patch.object(
                real_run_readiness,
                "get_platform_module",
                side_effect=load,
            ),
            patch.object(
                real_run_readiness,
                "validate_real_run_evidence",
                new=AsyncMock(return_value=healthy_evidence),
            ) as validate_evidence,
        ):
            response = await lotteries.list_real_run_evidence(limit=2)

        by_platform = {
            item["platform"]: item
            for item in response["items"]
        }
        self.assertFalse(by_platform["bilibili"]["allowed"])
        self.assertEqual(
            by_platform["bilibili"]["blockers"],
            ["platform_module_unavailable"],
        )
        self.assertEqual(
            by_platform["bilibili"]["next_action"],
            "blocked",
        )
        self.assertTrue(by_platform["weibo"]["allowed"])
        self.assertEqual(validate_evidence.await_count, 1)
        self.assertEqual(
            validate_evidence.await_args.args[0]["platform"],
            "weibo",
        )


if __name__ == "__main__":
    unittest.main()
