import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from app import main as worker_main
from app.runtime_scope import (
    WORKER_ROLE_ALL,
    WORKER_ROLE_CONTROL,
    WORKER_ROLE_PLATFORM,
    build_worker_runtime_plan,
    validate_worker_deployment_plan,
)
from shared.platform_ids import PLATFORM_IDS
from shared.platform_scope import PlatformScopeError


class WorkerRuntimeScopeTests(unittest.TestCase):
    def test_runtime_roles_have_mutually_exclusive_ownership(self):
        all_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_ALL,
            platform_scope="all",
        )
        self.assertEqual(all_plan.platforms, PLATFORM_IDS)
        self.assertTrue(all_plan.owns_platform_lanes)
        self.assertTrue(all_plan.owns_control_loops)
        self.assertTrue(all_plan.owns_legacy_fanout)

        control_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_CONTROL,
            platform_scope="all",
        )
        self.assertEqual(control_plan.platforms, ())
        self.assertFalse(control_plan.owns_platform_lanes)
        self.assertTrue(control_plan.owns_control_loops)
        self.assertTrue(control_plan.owns_legacy_fanout)

        platform_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_PLATFORM,
            platform_scope="weibo",
        )
        self.assertEqual(platform_plan.platforms, ("weibo",))
        self.assertTrue(platform_plan.owns_platform_lanes)
        self.assertFalse(platform_plan.owns_control_loops)
        self.assertFalse(platform_plan.owns_legacy_fanout)

    def test_invalid_role_scope_combinations_fail_closed(self):
        cases = (
            {"role": "missing", "platform_scope": "all"},
            {"role": WORKER_ROLE_CONTROL, "platform_scope": "bilibili"},
            {"role": WORKER_ROLE_ALL, "platform_scope": "weibo"},
            {"role": WORKER_ROLE_PLATFORM, "platform_scope": "all"},
            {"role": WORKER_ROLE_PLATFORM, "platform_scope": "unknown"},
        )
        for kwargs in cases:
            with self.subTest(**kwargs), self.assertRaises(
                (ValueError, PlatformScopeError)
            ):
                build_worker_runtime_plan(**kwargs)

    def test_production_rejects_compatibility_monolith(self):
        all_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_ALL,
            platform_scope="all",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "worker_all_role_forbidden_in_production",
        ):
            validate_worker_deployment_plan(
                all_plan,
                deployment_mode="production",
            )

        control_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_CONTROL,
            platform_scope="all",
        )
        self.assertIs(
            validate_worker_deployment_plan(
                control_plan,
                deployment_mode="production",
            ),
            control_plan,
        )
        self.assertIs(
            validate_worker_deployment_plan(
                all_plan,
                deployment_mode="dev",
            ),
            all_plan,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "worker_fixed_instance_id_forbidden_in_production",
        ):
            validate_worker_deployment_plan(
                control_plan,
                deployment_mode="production",
                configured_instance_id="dpms-worker-control",
            )

    def test_control_production_rejects_unimplemented_isolated_database_routing(self):
        control_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_CONTROL,
            platform_scope="all",
        )
        with patch.dict(
            os.environ,
            {"DPMS_MYSQL_PLATFORM_DATABASE_MODE": "isolated"},
            clear=False,
        ), self.assertRaisesRegex(
            RuntimeError,
            "worker_control_isolated_database_routing_unimplemented",
        ):
            validate_worker_deployment_plan(
                control_plan,
                deployment_mode="production",
            )

    def test_platform_preflight_failure_is_lane_local(self):
        plans = {
            platform: build_worker_runtime_plan(
                role=WORKER_ROLE_PLATFORM,
                platform_scope=platform,
            )
            for platform in ("bilibili", "weibo")
        }
        control_plan = build_worker_runtime_plan(
            role=WORKER_ROLE_CONTROL,
            platform_scope="all",
        )

        def load(platform):
            if platform == "bilibili":
                raise ImportError("synthetic bilibili import failure")
            return SimpleNamespace(platform_id=platform)

        with patch.object(
            worker_main,
            "get_platform_module",
            side_effect=load,
        ) as loader:
            with self.assertRaisesRegex(
                ImportError,
                "synthetic bilibili import failure",
            ):
                worker_main.preflight_worker_platform_modules(
                    plans["bilibili"]
                )
            worker_main.preflight_worker_platform_modules(plans["weibo"])
            worker_main.preflight_worker_platform_modules(control_plan)

        self.assertEqual(
            [call.args[0] for call in loader.call_args_list],
            ["bilibili", "weibo"],
        )

    def test_platform_preflight_rejects_missing_or_mismatched_module(self):
        plan = build_worker_runtime_plan(
            role=WORKER_ROLE_PLATFORM,
            platform_scope="douyin",
        )
        for module in (
            None,
            SimpleNamespace(platform_id="xiaohongshu"),
        ):
            with (
                self.subTest(module=module),
                patch.object(
                    worker_main,
                    "get_platform_module",
                    return_value=module,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "worker_platform_preflight_mismatch:douyin",
                ),
            ):
                worker_main.preflight_worker_platform_modules(plan)


class WorkerRuntimeTaskOwnershipTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_control_and_platform_task_specs_are_disjoint(self):
        async def block(*_args, **_kwargs):
            await asyncio.Event().wait()

        class Pool:
            async def context_reaper_loop(self, *_args, **_kwargs):
                await asyncio.Event().wait()

        loop_names = (
            "heartbeat_loop",
            "reload_signal_loop",
            "start_task_outbox_dispatcher",
            "login_loop",
            "login_profile_cleanup_loop",
            "heartbeat_retention_loop",
            "legacy_probe_fanout_loop",
            "legacy_calibration_fanout_loop",
            "calibration_loop",
            "probe_loop",
            "task_loop",
            "account_profile_cleanup_loop",
        )
        mocks = {
            name: AsyncMock(side_effect=block)
            for name in loop_names
        }
        pools = [Pool(), Pool(), Pool()]
        shutdowns = [
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        ]
        plans = (
            build_worker_runtime_plan(
                role=WORKER_ROLE_CONTROL,
                platform_scope="all",
            ),
            build_worker_runtime_plan(
                role=WORKER_ROLE_PLATFORM,
                platform_scope="weibo",
            ),
            build_worker_runtime_plan(
                role=WORKER_ROLE_ALL,
                platform_scope="all",
            ),
        )
        patches = [
            patch.object(worker_main, name, new=mock)
            for name, mock in mocks.items()
        ]
        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        with patch.object(
            worker_main.settings,
            "legacy_control_stream_drain_enabled",
            True,
        ):
            task_sets = tuple(
                worker_main.start_worker_runtime_tasks(
                    pool,
                    shutdown,
                    plan,
                )
                for pool, shutdown, plan in zip(
                    pools,
                    shutdowns,
                    plans,
                )
            )
        disabled_pool = Pool()
        disabled_shutdown = asyncio.Event()
        with patch.object(
            worker_main.settings,
            "legacy_control_stream_drain_enabled",
            False,
        ):
            disabled_tasks = worker_main.start_worker_runtime_tasks(
                disabled_pool,
                disabled_shutdown,
                plans[0],
            )
        try:
            await asyncio.sleep(0)
            control_names, platform_names, all_names = (
                {task.get_name() for task in tasks}
                for tasks in task_sets
            )
            for fanout_name in (
                "worker:legacy-probe-fanout",
                "worker:legacy-calibration-fanout",
            ):
                self.assertIn(fanout_name, control_names)
                self.assertNotIn(fanout_name, platform_names)
                self.assertIn(fanout_name, all_names)
                self.assertNotIn(
                    fanout_name,
                    {task.get_name() for task in disabled_tasks},
                )
            for lane_name in (
                "worker:calibration",
                "worker:probe",
                "worker:task-consumer",
                "worker:account-profile-cleanup",
            ):
                self.assertNotIn(lane_name, control_names)
                self.assertIn(lane_name, platform_names)
                self.assertIn(lane_name, all_names)

            for control_name in (
                "worker:login",
                "worker:login-profile-cleanup",
            ):
                self.assertIn(control_name, control_names)
                self.assertNotIn(control_name, platform_names)
                self.assertIn(control_name, all_names)

            self.assertEqual(
                mocks["calibration_loop"].await_args_list,
                [
                    call(
                        pools[1],
                        shutdowns[1],
                        platforms=("weibo",),
                        include_legacy_fanout=False,
                    ),
                    call(
                        pools[2],
                        shutdowns[2],
                        platforms=PLATFORM_IDS,
                        include_legacy_fanout=False,
                    ),
                ],
            )
            self.assertEqual(
                mocks["probe_loop"].await_args_list,
                [
                    call(
                        pools[1],
                        shutdowns[1],
                        platforms=("weibo",),
                        include_legacy_fanout=False,
                    ),
                    call(
                        pools[2],
                        shutdowns[2],
                        platforms=PLATFORM_IDS,
                        include_legacy_fanout=False,
                    ),
                ],
            )
            self.assertEqual(
                mocks[
                    "account_profile_cleanup_loop"
                ].await_args_list,
                [
                    call(
                        pools[1],
                        shutdowns[1],
                        platforms=("weibo",),
                    ),
                    call(
                        pools[2],
                        shutdowns[2],
                        platforms=PLATFORM_IDS,
                    ),
                ],
            )
            self.assertEqual(
                mocks[
                    "legacy_probe_fanout_loop"
                ].await_args_list,
                [call(shutdowns[0]), call(shutdowns[2])],
            )
            self.assertEqual(
                mocks[
                    "legacy_calibration_fanout_loop"
                ].await_args_list,
                [call(shutdowns[0]), call(shutdowns[2])],
            )
        finally:
            for tasks in (*task_sets, disabled_tasks):
                for task in tasks:
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for tasks in (*task_sets, disabled_tasks)
                    for task in tasks
                ),
                return_exceptions=True,
            )


if __name__ == "__main__":
    unittest.main()
