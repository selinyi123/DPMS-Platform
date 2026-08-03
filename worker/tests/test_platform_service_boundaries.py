"""Contracts that keep platform business code out of central runtimes."""

from __future__ import annotations

import inspect
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

from app import adapter_probe, task_runner  # noqa: E402
from app.action_plan import BILIBILI_API_EXECUTION_PATH  # noqa: E402
from app.platform_modules import bilibili, weibo  # noqa: E402
from app.platform_modules.registry import get_platform_module  # noqa: E402
from app.platform_modules.services import (  # noqa: E402
    ProbeExecutionServices,
    TaskExecutionServices,
)


class PlatformServiceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_task_facade_is_frozen_and_contains_no_platform_symbols(self):
        services = task_runner._task_execution_services()
        self.assertIsInstance(services, TaskExecutionServices)
        names = {field.name.casefold() for field in fields(services)}
        for platform in ("bilibili", "weibo", "xiaohongshu", "douyin"):
            self.assertFalse(
                any(platform in name for name in names),
                f"platform-specific service leaked: {platform}",
            )
        for forbidden in (
            "BilibiliApiClient",
            "WeiboApiClient",
            "get_completed_bilibili_phases",
            "load_weibo_oauth_credential",
            "preflight_weibo_friend_mentions",
        ):
            self.assertFalse(hasattr(services, forbidden), forbidden)
        with self.assertRaises(FrozenInstanceError):
            services.WORKER_ID = "mutated"

    def test_probe_facade_is_minimal_and_frozen(self):
        services = adapter_probe._probe_execution_services()
        self.assertIsInstance(services, ProbeExecutionServices)
        self.assertEqual(
            {field.name for field in fields(services)},
            {
                "database",
                "ProbeObservation",
                "credential_to_cookie_header",
                "execute_browser_observation_probe",
                "load_probe_credential",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            services.database = object()

    async def test_central_module_object_is_not_an_accepted_runtime(self):
        path = get_platform_module("bilibili").execution_path(
            BILIBILI_API_EXECUTION_PATH
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "platform_task_services_required",
        ):
            await path.execute(
                "real_run",
                {
                    "platform": "bilibili",
                    "action_plan": {
                        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
                    },
                },
                None,
                None,
                runtime=task_runner,
            )

    def test_platform_implementations_have_no_dynamic_central_lookup(self):
        for module in (bilibili, weibo):
            source = inspect.getsource(module)
            self.assertNotIn("_platform_runtime_symbol", source)
            self.assertNotIn("runtime_registry", source)
            self.assertNotIn("sys.modules", source)
            self.assertNotIn("app.task_runner", source)
            self.assertNotIn("app.adapter_probe", source)

    def test_orchestrators_do_not_publish_themselves_as_platform_runtime(self):
        for module in (task_runner, adapter_probe):
            source = inspect.getsource(module)
            self.assertNotIn("runtime_registry", source)
            self.assertNotIn("sys.modules[__name__]", source)


if __name__ == "__main__":
    unittest.main()
