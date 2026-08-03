"""Isolation and routing contracts for Worker platform modules."""

from __future__ import annotations

import copy
import inspect
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch


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

from app import real_run_gate, task_runner  # noqa: E402
from app.platform_modules import bilibili as bilibili_module  # noqa: E402
from app.platform_modules import douyin as douyin_module  # noqa: E402
from app.platform_modules import weibo as weibo_module  # noqa: E402
from app.platform_modules import xiaohongshu as xiaohongshu_module  # noqa: E402
from app.action_plan import (  # noqa: E402
    BILIBILI_API_EXECUTION_PATH,
    DOUYIN_DEVICE_EXECUTION_PATH,
    DOUYIN_MANUAL_EXECUTION_PATH,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    XIAOHONGSHU_BROWSER_EXECUTION_PATH,
    XIAOHONGSHU_MANUAL_EXECUTION_PATH,
    compute_action_plan_hash,
)
from app.platform_modules.base import PlatformRoutingError  # noqa: E402
from app.platform_modules.contracts import (  # noqa: E402
    ACTION_PLAN_CONTRACTS,
    action_plan_contract_for,
)
from app.platform_modules.registry import (  # noqa: E402
    PLATFORM_MODULES,
    get_platform_module,
    registered_platforms,
)
from app.platform_modules.services import TaskExecutionServices  # noqa: E402
from app.platform_modules import bilibili as bilibili_module  # noqa: E402
from app.platform_modules import weibo as weibo_module  # noqa: E402
from app.platform_modules.evidence import RealRunGateBlocked  # noqa: E402


class PlatformRegistryTests(unittest.TestCase):
    def test_platform_evidence_validators_do_not_depend_on_central_gate(self):
        self.assertNotIn(
            "app.real_run_gate",
            inspect.getsource(bilibili_module),
        )
        self.assertNotIn(
            "app.real_run_gate",
            inspect.getsource(weibo_module),
        )
        self.assertFalse(
            hasattr(real_run_gate, "_validate_execution_evidence")
        )
        self.assertFalse(
            hasattr(
                real_run_gate,
                "_validate_weibo_oauth_execution_evidence",
            )
        )
        self.assertIs(real_run_gate.RealRunGateBlocked, RealRunGateBlocked)

    def test_shared_gate_query_has_no_platform_evidence_tables(self):
        shared_query = " ".join(real_run_gate._TASK_DECISION_QUERY.split())
        bilibili_query = " ".join(
            bilibili_module.BILIBILI_REAL_RUN_EVIDENCE_QUERY.split()
        )
        weibo_query = " ".join(
            weibo_module.WEIBO_REAL_RUN_EVIDENCE_QUERY.split()
        )

        for platform_table in (
            "execution_evidence_bindings",
            "adapter_calibrations",
            "account_calibrations",
        ):
            self.assertNotIn(platform_table, shared_query)
        self.assertIn("execution_evidence_bindings", bilibili_query)
        self.assertIn("adapter_calibrations", bilibili_query)
        self.assertNotIn("account_calibrations", bilibili_query)
        self.assertIn("account_calibrations", weibo_query)
        self.assertNotIn("execution_evidence_bindings", weibo_query)
        self.assertNotIn("adapter_calibrations", weibo_query)
        self.assertIn("WHERE tr.task_id = :task_id", bilibili_query)
        self.assertIn("WHERE tr.task_id = :task_id", weibo_query)

    def test_four_platforms_have_independent_immutable_descriptors(self):
        self.assertEqual(
            registered_platforms(),
            ("bilibili", "weibo", "xiaohongshu", "douyin"),
        )
        self.assertEqual(len({id(value) for value in PLATFORM_MODULES.values()}), 4)
        with self.assertRaises(TypeError):
            PLATFORM_MODULES["other"] = get_platform_module("bilibili")

    def test_account_credential_kind_contract_is_consistent(self):
        expected = {
            ("bilibili", BILIBILI_API_EXECUTION_PATH): "browser_session",
            ("weibo", WEIBO_MANUAL_EXECUTION_PATH): "browser_session",
            ("weibo", WEIBO_OAUTH_EXECUTION_PATH): "weibo_oauth",
            ("xiaohongshu", XIAOHONGSHU_MANUAL_EXECUTION_PATH): "browser_session",
            ("xiaohongshu", XIAOHONGSHU_BROWSER_EXECUTION_PATH): "browser_session",
            ("douyin", DOUYIN_MANUAL_EXECUTION_PATH): "browser_session",
            ("douyin", DOUYIN_DEVICE_EXECUTION_PATH): "device_agent",
        }
        observed = {
            (module.platform_id, path.path_id): path.credential_kind
            for module in PLATFORM_MODULES.values()
            for path in module.execution_paths
        }
        self.assertEqual(observed, expected)

    def test_accepted_targets_do_not_imply_real_executor_support(self):
        self.assertEqual(
            get_platform_module("bilibili").real_target_kinds,
            frozenset({"dynamic"}),
        )
        self.assertEqual(
            get_platform_module("weibo").real_target_kinds,
            frozenset({"status"}),
        )
        self.assertEqual(
            get_platform_module("xiaohongshu").real_target_kinds,
            frozenset({"note"}),
        )
        self.assertEqual(
            get_platform_module("douyin").real_target_kinds,
            frozenset({"video", "note"}),
        )

    def test_execution_modules_match_their_platform_owned_plan_contracts(self):
        with self.assertRaises(TypeError):
            ACTION_PLAN_CONTRACTS["other"] = action_plan_contract_for("bilibili")
        for platform, module in PLATFORM_MODULES.items():
            with self.subTest(platform=platform):
                contract = action_plan_contract_for(platform)
                self.assertEqual(module.action_order, contract.action_order)
                self.assertEqual(
                    module.default_execution_path,
                    contract.default_execution_path,
                )

    def test_douyin_action_evolution_does_not_change_weibo_hash_vector(self):
        weibo_contract = action_plan_contract_for("weibo")
        douyin_contract = action_plan_contract_for("douyin")
        weibo_vector = {
            "platform": "weibo",
            "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
            "required_actions": list(weibo_contract.action_order),
        }
        original_hash = compute_action_plan_hash(weibo_vector)

        evolved_douyin = replace(
            douyin_contract,
            action_order=douyin_contract.action_order + ("shared_video",),
        )

        self.assertEqual(
            weibo_contract.action_order,
            ("followed", "liked", "commented", "favorited", "reposted"),
        )
        self.assertNotEqual(evolved_douyin.action_order, weibo_contract.action_order)
        self.assertEqual(
            original_hash,
            "cf9ddf4524014e15614cdc37137492a83325bb846a0425f00d804225cd60893f",
        )
        self.assertEqual(compute_action_plan_hash(weibo_vector), original_hash)

    def test_execution_paths_do_not_leak_between_platforms(self):
        path_by_platform = {
            "bilibili": WEIBO_OAUTH_EXECUTION_PATH,
            "weibo": DOUYIN_MANUAL_EXECUTION_PATH,
            "xiaohongshu": BILIBILI_API_EXECUTION_PATH,
            "douyin": XIAOHONGSHU_MANUAL_EXECUTION_PATH,
        }
        for platform, foreign_path in path_by_platform.items():
            with self.subTest(platform=platform, foreign_path=foreign_path):
                with self.assertRaises(PlatformRoutingError):
                    get_platform_module(platform).route(
                        "shadow_run", {"execution_path_id": foreign_path}
                    )

    def test_task_validation_preserves_platform_specific_invalid_path_codes(self):
        expected_codes = {
            "bilibili": "bilibili_execution_path_not_supported",
            "weibo": "weibo_execution_path_invalid",
            "xiaohongshu": "xiaohongshu_execution_path_not_supported",
            "douyin": "douyin_execution_path_invalid",
        }
        for platform, expected_code in expected_codes.items():
            with self.subTest(platform=platform):
                message = {
                    "task_id": f"{platform}-bad-path",
                    "account_id": "7",
                    "lottery_id": "9",
                    "platform": platform,
                    "mode": "shadow_run",
                    "action_plan": {"execution_path_id": "foreign_path_v1"},
                }
                with self.assertRaises(task_runner.InvalidTaskMessage) as ctx:
                    task_runner.validate_task_message(message)
                self.assertEqual(str(ctx.exception), expected_code)

    def test_platform_phase_selection_isolated(self):
        self.assertEqual(
            get_platform_module("bilibili").phases(
                {"required_actions": ["favorited"]}
            ),
            ["followed", "liked", "commented", "reposted"],
        )
        self.assertEqual(
            get_platform_module("douyin").phases(
                {"required_actions": ["liked", "favorited"]}
            ),
            ["liked", "favorited"],
                )


class PlatformEvidenceContextLoaderTests(unittest.IsolatedAsyncioTestCase):
    class RecordingDatabase:
        def __init__(self, *, fail_table: str | None = None):
            self.fail_table = fail_table
            self.calls = []

        async def fetch_one(self, query, values=None):
            self.calls.append((query, dict(values or {})))
            if self.fail_table and self.fail_table in query:
                raise RuntimeError(f"{self.fail_table} unavailable")
            return {
                "evidence_id": "bilibili-evidence",
                "oauth_calibration_id": "weibo-calibration",
            }

    async def test_each_loader_executes_only_its_platform_query(self):
        db = self.RecordingDatabase()

        bilibili = await get_platform_module(
            "bilibili"
        ).load_real_run_evidence_context(
            db=db,
            task_id="bilibili-task",
        )
        weibo = await get_platform_module(
            "weibo"
        ).load_real_run_evidence_context(
            db=db,
            task_id="weibo-task",
        )

        self.assertEqual(bilibili["evidence_id"], "bilibili-evidence")
        self.assertNotIn("oauth_calibration_id", bilibili)
        self.assertEqual(weibo["oauth_calibration_id"], "weibo-calibration")
        self.assertNotIn("evidence_id", weibo)
        self.assertEqual(
            [values for _query, values in db.calls],
            [
                {"task_id": "bilibili-task"},
                {"task_id": "weibo-task"},
            ],
        )

    async def test_one_loader_failure_does_not_poison_its_peer(self):
        db = self.RecordingDatabase(
            fail_table="execution_evidence_bindings"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "execution_evidence_bindings unavailable",
        ):
            await get_platform_module(
                "bilibili"
            ).load_real_run_evidence_context(
                db=db,
                task_id="bilibili-task",
            )

        weibo = await get_platform_module(
            "weibo"
        ).load_real_run_evidence_context(
            db=db,
            task_id="weibo-task",
        )
        self.assertEqual(
            weibo["oauth_calibration_id"],
            "weibo-calibration",
        )
        self.assertEqual(len(db.calls), 2)


class WeiboModeAwareRoutingTests(unittest.TestCase):
    def test_oauth_plan_uses_browser_session_only_for_shadow(self):
        module = get_platform_module("weibo")
        plan = {"execution_path_id": WEIBO_OAUTH_EXECUTION_PATH}
        original = copy.deepcopy(plan)

        shadow_path, shadow_executor = module.route("shadow_run", plan)
        self.assertEqual(shadow_path.path_id, WEIBO_MANUAL_EXECUTION_PATH)
        self.assertEqual(shadow_path.credential_kind, "browser_session")
        self.assertEqual(shadow_executor, "browser_observation")
        self.assertTrue(module.requires_selector_binding("shadow_run", plan))

        for mode in ("dry_run", "real_run"):
            with self.subTest(mode=mode):
                oauth_path, executor = module.route(mode, plan)
                self.assertEqual(oauth_path.path_id, WEIBO_OAUTH_EXECUTION_PATH)
                self.assertEqual(oauth_path.credential_kind, "weibo_oauth")
                self.assertEqual(
                    executor,
                    "dry_run" if mode == "dry_run" else "weibo_oauth",
                )

        self.assertEqual(plan, original, "runtime routing must not rewrite the plan")

    def test_manual_plan_remains_shadow_only(self):
        module = get_platform_module("weibo")
        plan = {"execution_path_id": WEIBO_MANUAL_EXECUTION_PATH}
        path, executor = module.route("shadow_run", plan)
        self.assertEqual(path.path_id, WEIBO_MANUAL_EXECUTION_PATH)
        self.assertEqual(executor, "browser_observation")
        for mode in ("dry_run", "real_run"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    PlatformRoutingError, "weibo_manual_shadow_only"
                ):
                    module.route(mode, plan)


class PlatformExecutionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_handler_selection_is_owned_by_platform_modules(self):
        bilibili_probe = get_platform_module("bilibili").probe_handler
        self.assertIs(
            bilibili_probe,
            bilibili_module.execute_bilibili_api_probe,
        )
        self.assertEqual(
            bilibili_probe.__module__,
            "app.platform_modules.bilibili",
        )

        generic_browser_handlers = {
            get_platform_module("weibo").probe_handler
        }
        self.assertIs(
            get_platform_module("douyin").probe_handler,
            douyin_module.execute_douyin_device_probe,
        )
        self.assertIs(
            get_platform_module("xiaohongshu").probe_handler,
            xiaohongshu_module.execute_xiaohongshu_browser_probe,
        )
        self.assertNotIn(
            get_platform_module("bilibili").probe_handler,
            generic_browser_handlers,
        )

    async def test_platform_paths_own_distinct_real_execution_entrypoints(self):
        cases = (
            (
                "bilibili",
                BILIBILI_API_EXECUTION_PATH,
                "execute_bilibili_api_real_task",
            ),
            (
                "weibo",
                WEIBO_OAUTH_EXECUTION_PATH,
                "execute_weibo_oauth_real_task",
            ),
        )
        observed_handlers = []
        owned_modules = {
            "bilibili": (
                bilibili_module,
                "_execute_bilibili_api_real_owned",
            ),
            "weibo": (
                weibo_module,
                "_execute_weibo_oauth_real_owned",
            ),
        }
        for platform, path_id, _facade_name in cases:
            with self.subTest(platform=platform):
                path, _ = get_platform_module(platform).route(
                    "real_run",
                    {"execution_path_id": path_id},
                )
                observed_handlers.append(path.real_handler)
                task = {
                    "platform": platform,
                    "action_plan": {"execution_path_id": path_id},
                }
                module, owned_name = owned_modules[platform]
                with patch.object(
                    module,
                    owned_name,
                    new_callable=AsyncMock,
                ) as owned:
                    services = task_runner._task_execution_services()
                    await path.execute(
                        "real_run",
                        task,
                        None,
                        None,
                        runtime=services,
                    )
                owned.assert_awaited_once_with(
                    task,
                    runtime=services,
                )

        self.assertEqual(len(set(observed_handlers)), len(cases))

    async def test_platform_paths_select_api_or_shared_shadow_infrastructure(self):
        bilibili_task = {
            "platform": "bilibili",
            "action_plan": {
                "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            },
        }
        bilibili_path, _ = get_platform_module("bilibili").route(
            "shadow_run",
            bilibili_task["action_plan"],
        )
        self.assertIs(
            bilibili_path.shadow_handler,
            bilibili_module.execute_bilibili_api_shadow,
        )
        self.assertEqual(
            bilibili_path.shadow_handler.__module__,
            "app.platform_modules.bilibili",
        )

        browser_handlers = []
        for platform, path_id in (
            ("weibo", WEIBO_OAUTH_EXECUTION_PATH),
            ("xiaohongshu", XIAOHONGSHU_MANUAL_EXECUTION_PATH),
            ("douyin", DOUYIN_MANUAL_EXECUTION_PATH),
        ):
            with self.subTest(platform=platform):
                task = {
                    "platform": platform,
                    "action_plan": {"execution_path_id": path_id},
                }
                path, _ = get_platform_module(platform).route(
                    "shadow_run",
                    task["action_plan"],
                )
                browser_handlers.append(path.shadow_handler)
                adapter = object()
                pool = object()
                with patch.object(
                    task_runner,
                    "execute_browser_observation_shadow",
                    new_callable=AsyncMock,
                ) as browser_shadow:
                    services = task_runner._task_execution_services()
                    await path.execute(
                        "shadow_run",
                        task,
                        adapter,
                        pool,
                        runtime=services,
                    )
                browser_shadow.assert_awaited_once_with(task, adapter, pool)

        self.assertEqual(len(set(browser_handlers)), 1)

    async def test_task_runner_dispatch_does_not_depend_on_executor_label(self):
        module = get_platform_module("bilibili")
        original_path = module.execution_path(BILIBILI_API_EXECUTION_PATH)
        handler = AsyncMock(return_value=None)
        opaque_path = replace(
            original_path,
            real_executor="opaque_platform_owned_strategy",
            real_handler=handler,
        )
        opaque_module = replace(module, execution_paths=(opaque_path,))
        task = {
            "task_id": "task-platform-dispatch",
            "account_id": "7",
            "lottery_id": "9",
            "platform": "bilibili",
            "mode": "real_run",
            "action_plan": {
                "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            },
        }
        binding = task_runner.CanonicalTaskBinding(
            task_id=task["task_id"],
            account_id=7,
            lottery_id=9,
            task_mode="real_run",
        )

        with patch.object(
            task_runner,
            "get_platform_module",
            return_value=opaque_module,
        ), patch.object(
            task_runner,
            "enforce_task_real_run_gate",
            new_callable=AsyncMock,
        ), patch.object(
            task_runner,
            "ensure_account_can_run",
            new_callable=AsyncMock,
        ), patch.object(
            task_runner,
            "mark_task_started",
            AsyncMock(return_value=binding),
        ), patch.object(
            task_runner,
            "mark_task_finished",
            AsyncMock(return_value=True),
        ):
            result = await task_runner.execute_task_with_phases(
                task,
                adapter=None,
                pool=None,
            )

        self.assertTrue(result)
        handler.assert_awaited_once()
        call = handler.await_args
        self.assertEqual(call.args, (task, None, None))
        self.assertIsInstance(
            call.kwargs.get("runtime"),
            TaskExecutionServices,
        )

    def test_platform_execution_modules_have_no_central_back_imports(self):
        for module in (
            bilibili_module,
            weibo_module,
            xiaohongshu_module,
        ):
            source = inspect.getsource(module)
            self.assertNotIn("from app import task_runner", source)
            self.assertNotIn("from app import adapter_probe", source)
            self.assertNotIn("task_runner.", source)
            self.assertNotIn("adapter_probe.", source)

    def test_only_real_mutation_paths_can_confirm_intent_settlement(self):
        self.assertTrue(
            get_platform_module("bilibili")
            .execution_path(BILIBILI_API_EXECUTION_PATH)
            .confirmed_intent_settlement
        )
        self.assertTrue(
            get_platform_module("weibo")
            .execution_path(WEIBO_OAUTH_EXECUTION_PATH)
            .confirmed_intent_settlement
        )
        self.assertTrue(
            get_platform_module("xiaohongshu")
            .execution_path(XIAOHONGSHU_BROWSER_EXECUTION_PATH)
            .confirmed_intent_settlement
        )
        for platform, path_id in (
            ("weibo", WEIBO_MANUAL_EXECUTION_PATH),
            ("xiaohongshu", XIAOHONGSHU_MANUAL_EXECUTION_PATH),
            ("douyin", DOUYIN_MANUAL_EXECUTION_PATH),
        ):
            with self.subTest(platform=platform):
                self.assertFalse(
                    get_platform_module(platform)
                    .execution_path(path_id)
                    .confirmed_intent_settlement
                )


if __name__ == "__main__":
    unittest.main()
