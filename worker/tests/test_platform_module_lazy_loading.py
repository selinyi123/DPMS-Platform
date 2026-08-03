"""Boot and fault-domain contracts for Worker platform runtimes."""

from types import ModuleType
import subprocess
import sys
import unittest
from unittest.mock import patch

from app.platform_modules.registry import (
    LazyPlatformModules,
    PlatformModuleUnavailableError,
    get_platform_module,
)


class LazyWorkerPlatformModuleTests(unittest.TestCase):
    def test_worker_startup_import_does_not_load_platform_runtimes(self):
        runtime_prefixes = (
            "app.bilibili",
            "app.weibo",
            "app.adapters.bilibili",
            "app.adapters.weibo",
            "app.adapters.xiaohongshu",
            "app.adapters.douyin",
            "app.platform_modules.bilibili",
            "app.platform_modules.weibo",
            "app.platform_modules.xiaohongshu",
            "app.platform_modules.douyin",
            "app.platform_modules.contracts.bilibili",
            "app.platform_modules.contracts.weibo",
            "app.platform_modules.contracts.xiaohongshu",
            "app.platform_modules.contracts.douyin",
        )
        script = (
            "import sys; import app.main; "
            f"prefixes={runtime_prefixes!r}; "
            "loaded=[name for name in sys.modules "
            "if any(name == prefix or name.startswith(prefix + '.') "
            "for prefix in prefixes)]; "
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
        registry = LazyPlatformModules(
            {
                "bilibili": (
                    "app.platform_modules.bilibili",
                    "BILIBILI",
                ),
                "weibo": ("app.platform_modules.weibo", "WEIBO"),
            }
        )
        with patch(
            "app.platform_modules.registry.importlib.import_module"
        ) as importer:
            self.assertEqual(tuple(registry), ("bilibili", "weibo"))
            self.assertEqual(len(registry), 2)
            importer.assert_not_called()

    def test_failed_platform_is_cached_and_peer_still_loads(self):
        weibo = get_platform_module("weibo")
        fake_weibo_module = ModuleType("app.platform_modules.weibo")
        fake_weibo_module.WEIBO = weibo
        calls: list[str] = []

        def import_one(module_name: str):
            calls.append(module_name)
            if module_name == "app.platform_modules.bilibili":
                raise ImportError("synthetic bilibili failure")
            if module_name == "app.platform_modules.weibo":
                return fake_weibo_module
            raise AssertionError(module_name)

        registry = LazyPlatformModules(
            {
                "bilibili": (
                    "app.platform_modules.bilibili",
                    "BILIBILI",
                ),
                "weibo": ("app.platform_modules.weibo", "WEIBO"),
            }
        )
        with patch(
            "app.platform_modules.registry.importlib.import_module",
            side_effect=import_one,
        ):
            for _ in range(2):
                with self.assertRaises(PlatformModuleUnavailableError) as caught:
                    registry["bilibili"]
                self.assertEqual(caught.exception.platform, "bilibili")
            self.assertIs(registry["weibo"], weibo)

        self.assertEqual(
            calls.count("app.platform_modules.bilibili"),
            1,
        )
        self.assertEqual(calls.count("app.platform_modules.weibo"), 1)


if __name__ == "__main__":
    unittest.main()
