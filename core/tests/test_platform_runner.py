import asyncio
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import platform_runner
from app.services import outbox as outbox_service
from app.runtime_scope import (
    CORE_ROLE_ALL,
    CORE_ROLE_CONTROL,
    build_core_runtime_plan,
    validate_core_deployment_plan,
)
from shared.platform_ids import PLATFORM_IDS
from shared.platform_scope import PlatformScopeError


class CoreRuntimeScopeTests(unittest.TestCase):
    def test_runtime_roles_have_mutually_exclusive_platform_ownership(self):
        all_plan = build_core_runtime_plan(
            role=CORE_ROLE_ALL,
            platform_scope="all",
        )
        self.assertEqual(all_plan.platforms, PLATFORM_IDS)
        self.assertTrue(all_plan.owns_platform_lanes)
        self.assertTrue(all_plan.owns_shared_loops)

        control_plan = build_core_runtime_plan(
            role=CORE_ROLE_CONTROL,
            platform_scope="all",
        )
        self.assertEqual(control_plan.platforms, ())
        self.assertFalse(control_plan.owns_platform_lanes)
        self.assertTrue(control_plan.owns_shared_loops)

    def test_invalid_role_scope_combinations_fail_closed(self):
        cases = (
            {"role": "missing", "platform_scope": "all"},
            {"role": CORE_ROLE_CONTROL, "platform_scope": "bilibili"},
            {"role": CORE_ROLE_ALL, "platform_scope": "weibo"},
            {"role": CORE_ROLE_ALL, "platform_scope": "unknown"},
        )
        for kwargs in cases:
            with self.subTest(**kwargs), self.assertRaises(
                (ValueError, PlatformScopeError)
            ):
                build_core_runtime_plan(**kwargs)

    def test_production_rejects_compatibility_monolith(self):
        all_plan = build_core_runtime_plan(
            role=CORE_ROLE_ALL,
            platform_scope="all",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "core_all_role_forbidden_in_production",
        ):
            validate_core_deployment_plan(
                all_plan,
                deployment_mode="production",
            )

        control_plan = build_core_runtime_plan(
            role=CORE_ROLE_CONTROL,
            platform_scope="all",
        )
        self.assertIs(
            validate_core_deployment_plan(
                control_plan,
                deployment_mode="production",
            ),
            control_plan,
        )
        self.assertIs(
            validate_core_deployment_plan(
                all_plan,
                deployment_mode="dev",
            ),
            all_plan,
        )

    def test_control_production_rejects_unimplemented_isolated_database_routing(self):
        control_plan = build_core_runtime_plan(
            role=CORE_ROLE_CONTROL,
            platform_scope="all",
        )
        with patch.dict(
            os.environ,
            {"DPMS_MYSQL_PLATFORM_DATABASE_MODE": "isolated"},
            clear=False,
        ), self.assertRaisesRegex(
            RuntimeError,
            "core_control_isolated_database_routing_unimplemented",
        ):
            validate_core_deployment_plan(
                control_plan,
                deployment_mode="production",
            )


class CorePlatformRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_production_runner_rejects_fixed_consumer_identity(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "core_runner_fixed_instance_id_forbidden_in_production",
        ):
            platform_runner.validate_platform_runner_instance_identity(
                "production",
                "dpms-core-weibo-runner",
            )
        platform_runner.validate_platform_runner_instance_identity(
            "dev",
            "dpms-core-weibo-runner",
        )
        platform_runner.validate_platform_runner_instance_identity(
            "production",
            "",
        )

    def test_startup_reconciliation_phase_is_stable_and_platform_specific(
        self,
    ):
        phase_for = (
            outbox_service.outbox_reconciliation_startup_phase_seconds
        )
        phases = {
            platform: phase_for(platform)
            for platform in PLATFORM_IDS
        }

        self.assertEqual(
            phases,
            {
                platform: phase_for(platform)
                for platform in PLATFORM_IDS
            },
        )
        self.assertTrue(
            all(0 <= phase < 5 for phase in phases.values())
        )
        self.assertEqual(
            len(set(phases.values())),
            len(PLATFORM_IDS),
        )

    async def test_startup_phase_precedes_strict_continuity_check(self):
        events = []

        async def sleep(seconds):
            events.append(("phase", seconds))

        async def reconcile(**kwargs):
            events.append(("reconcile", kwargs))
            return 7

        with (
            patch.object(
                platform_runner,
                "outbox_reconciliation_startup_phase_seconds",
                return_value=1.234,
            ),
            patch.object(
                platform_runner.asyncio,
                "sleep",
                side_effect=sleep,
            ),
            patch.object(
                platform_runner,
                "reconcile_owned_stream_epochs",
                side_effect=reconcile,
            ),
        ):
            result = await (
                platform_runner.reconcile_platform_runner_startup_streams(
                    "weibo"
                )
            )

        self.assertEqual(result, 7)
        self.assertEqual(events[0], ("phase", 1.234))
        self.assertEqual(
            events[1],
            (
                "reconcile",
                {
                    "platforms": ("weibo",),
                    "include_shared": False,
                    "require_all_owned_lanes": True,
                },
            ),
        )

    def test_main_reconciles_before_starting_delivery_tasks(self):
        source = inspect.getsource(platform_runner.main)
        self.assertLess(
            source.index("await verify_migrations_current()"),
            source.index(
                "await reconcile_platform_runner_startup_streams("
            ),
        )
        self.assertLess(
            source.index(
                "await reconcile_platform_runner_startup_streams("
            ),
            source.index("tasks = start_platform_runner_tasks("),
        )

    async def test_main_clears_old_marker_before_platform_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "runner-health"
            marker.write_text("old", encoding="utf-8")
            with (
                patch.object(
                    platform_runner,
                    "selected_platform",
                    return_value="weibo",
                ),
                patch.object(
                    platform_runner,
                    "platform_runner_health_file",
                    return_value=marker,
                ),
                patch.object(
                    platform_runner,
                    "preflight_core_platform_module",
                    side_effect=RuntimeError("synthetic preflight failure"),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic preflight failure",
                ),
            ):
                await platform_runner.main()

            self.assertFalse(marker.exists())

    async def test_preflight_failure_is_lane_local(self):
        def load(platform):
            if platform == "bilibili":
                raise ImportError("synthetic bilibili import failure")
            return SimpleNamespace(platform_id=platform)

        with patch.object(
            platform_runner,
            "get_platform_module",
            side_effect=load,
        ) as loader:
            with self.assertRaisesRegex(
                ImportError,
                "synthetic bilibili import failure",
            ):
                platform_runner.preflight_core_platform_module(
                    "bilibili"
                )
            platform_runner.preflight_core_platform_module("weibo")

        self.assertEqual(
            [call.args[0] for call in loader.call_args_list],
            ["bilibili", "weibo"],
        )

    async def test_preflight_rejects_missing_or_mismatched_module(self):
        for module in (
            None,
            SimpleNamespace(platform_id="xiaohongshu"),
        ):
            with (
                self.subTest(module=module),
                patch.object(
                    platform_runner,
                    "get_platform_module",
                    return_value=module,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "core_platform_preflight_failed:douyin",
                ),
            ):
                platform_runner.preflight_core_platform_module("douyin")

    async def test_runner_starts_only_exact_platform_lanes(self):
        gate = asyncio.Event()

        async def wait_forever(*_args, **_kwargs):
            await gate.wait()

        mocks = {
            "recovery": AsyncMock(side_effect=wait_forever),
            "outbox": AsyncMock(side_effect=wait_forever),
            "scheduler": AsyncMock(side_effect=wait_forever),
            "discovery": AsyncMock(side_effect=wait_forever),
            "heartbeat": AsyncMock(side_effect=wait_forever),
        }
        shutdown = asyncio.Event()
        with (
            patch.object(
                platform_runner,
                "start_recovery_daemon",
                new=mocks["recovery"],
            ),
            patch.object(
                platform_runner,
                "start_outbox_dispatcher",
                new=mocks["outbox"],
            ),
            patch.object(
                platform_runner,
                "scheduler_loop",
                new=mocks["scheduler"],
            ),
            patch.object(
                platform_runner,
                "discovery_scan_request_loop",
                new=mocks["discovery"],
            ),
            patch.object(
                platform_runner,
                "platform_runner_heartbeat",
                new=mocks["heartbeat"],
            ),
        ):
            tasks = platform_runner.start_platform_runner_tasks(
                shutdown,
                platform="weibo",
            )
            await asyncio.sleep(0)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual(
            {task.get_name() for task in tasks},
            {
                "core-platform:weibo:recovery",
                "core-platform:weibo:outbox",
                "core-platform:weibo:scheduler",
                "core-platform:weibo:manual-discovery",
                "core-platform:weibo:heartbeat",
            },
        )
        mocks["recovery"].assert_awaited_once_with(
            platforms=("weibo",),
            include_shared=False,
            fail_closed=True,
        )
        mocks["outbox"].assert_awaited_once_with(
            platforms=("weibo",),
            include_shared=False,
        )
        mocks["scheduler"].assert_awaited_once_with(
            platforms=("weibo",),
            include_global=False,
            fail_closed=True,
        )
        mocks["discovery"].assert_awaited_once_with("weibo")
        mocks["heartbeat"].assert_awaited_once_with(
            shutdown,
            platform="weibo",
            supervised_tasks=tasks[:4],
        )

    async def test_unexpected_task_return_is_fatal_and_cancels_peers(self):
        peer_stopped = asyncio.Event()

        async def returns():
            return None

        async def peer():
            try:
                await asyncio.Event().wait()
            finally:
                peer_stopped.set()

        shutdown = asyncio.Event()
        completed = asyncio.create_task(
            returns(),
            name="core-platform:weibo:returned",
        )
        sibling = asyncio.create_task(
            peer(),
            name="core-platform:weibo:peer",
        )
        await asyncio.sleep(0)

        with self.assertRaisesRegex(
            RuntimeError,
            "platform_runner_task_returned:"
            "core-platform:weibo:returned",
        ):
            await platform_runner.supervise_platform_runner_tasks(
                (completed, sibling),
                shutdown,
            )

        self.assertTrue(shutdown.is_set())
        self.assertTrue(sibling.done())
        self.assertTrue(peer_stopped.is_set())

    async def test_health_marker_requires_db_redis_and_live_owned_loops(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "runner-health"
            shutdown = asyncio.Event()
            owned = asyncio.create_task(
                asyncio.Event().wait(),
                name="core-platform:weibo:owned",
            )

            async def healthy_database(_query):
                shutdown.set()
                return {"healthy": 1}

            with (
                patch.object(
                    platform_runner,
                    "platform_runner_health_file",
                    return_value=marker,
                ),
                patch.object(
                    platform_runner.database,
                    "fetch_one",
                    new=AsyncMock(side_effect=healthy_database),
                ),
                patch.object(
                    platform_runner,
                    "redis",
                    SimpleNamespace(
                        ping=AsyncMock(return_value=True)
                    ),
                ),
            ):
                await platform_runner.platform_runner_heartbeat(
                    shutdown,
                    platform="weibo",
                    supervised_tasks=(owned,),
                )

            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "ok",
            )
            owned.cancel()
            await asyncio.gather(owned, return_exceptions=True)

    async def test_dependency_failure_removes_preexisting_health_marker(
        self,
    ):
        for dependency in ("database", "redis"):
            with self.subTest(dependency=dependency):
                with tempfile.TemporaryDirectory() as temp_dir:
                    marker = Path(temp_dir) / "runner-health"
                    marker.write_text("old", encoding="utf-8")
                    shutdown = asyncio.Event()
                    owned = asyncio.create_task(
                        asyncio.Event().wait(),
                        name="core-platform:bilibili:owned",
                    )

                    async def database_probe(_query):
                        if dependency == "database":
                            shutdown.set()
                            raise RuntimeError("database unavailable")
                        return {"healthy": 1}

                    async def redis_probe():
                        shutdown.set()
                        if dependency == "redis":
                            raise RuntimeError("redis unavailable")
                        return True

                    with (
                        patch.object(
                            platform_runner,
                            "platform_runner_health_file",
                            return_value=marker,
                        ),
                        patch.object(
                            platform_runner.database,
                            "fetch_one",
                            new=AsyncMock(
                                side_effect=database_probe
                            ),
                        ),
                        patch.object(
                            platform_runner,
                            "redis",
                            SimpleNamespace(
                                ping=AsyncMock(
                                    side_effect=redis_probe
                                )
                            ),
                        ),
                        patch.object(
                            platform_runner,
                            "structured_log",
                        ),
                    ):
                        await (
                            platform_runner.platform_runner_heartbeat(
                                shutdown,
                                platform="bilibili",
                                supervised_tasks=(owned,),
                            )
                        )

                    self.assertFalse(marker.exists())
                    owned.cancel()
                    await asyncio.gather(
                        owned,
                        return_exceptions=True,
                    )

    async def test_completed_owned_loop_removes_marker_and_fails_runner(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "runner-health"
            marker.write_text("old", encoding="utf-8")

            async def returned():
                return None

            owned = asyncio.create_task(
                returned(),
                name="core-platform:douyin:owned",
            )
            await owned
            with (
                patch.object(
                    platform_runner,
                    "platform_runner_health_file",
                    return_value=marker,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "platform_runner_owned_loop_exited:douyin",
                ),
            ):
                await platform_runner.platform_runner_heartbeat(
                    asyncio.Event(),
                    platform="douyin",
                    supervised_tasks=(owned,),
                )

            self.assertFalse(marker.exists())

    async def test_acl_operation_failure_is_unhealthy_even_when_ping_works(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "runner-health"
            marker.write_text("old", encoding="utf-8")
            shutdown = asyncio.Event()
            release_failure = asyncio.Event()

            async def denied_flush(**_kwargs):
                await release_failure.wait()
                raise PermissionError("NOPERM synthetic")

            async def redis_probe():
                release_failure.set()
                await asyncio.sleep(0)
                shutdown.set()
                return True

            with (
                patch.object(
                    outbox_service,
                    "flush_pending_outbox",
                    side_effect=denied_flush,
                ),
                patch.object(
                    outbox_service,
                    "structured_log",
                ),
            ):
                owned = asyncio.create_task(
                    outbox_service._outbox_delivery_loop(
                        lane="lottery_tasks:bilibili",
                        stream_key="lottery_tasks:bilibili",
                        fail_closed=True,
                    ),
                    name="core-platform:bilibili:outbox-lane",
                )
                await asyncio.sleep(0)
                redis_ping = AsyncMock(side_effect=redis_probe)
                with (
                    patch.object(
                        platform_runner,
                        "platform_runner_health_file",
                        return_value=marker,
                    ),
                    patch.object(
                        platform_runner.database,
                        "fetch_one",
                        new=AsyncMock(
                            return_value={"healthy": 1}
                        ),
                    ),
                    patch.object(
                        platform_runner,
                        "redis",
                        SimpleNamespace(ping=redis_ping),
                    ),
                    patch.object(
                        platform_runner,
                        "structured_log",
                    ),
                ):
                    await platform_runner.platform_runner_heartbeat(
                        shutdown,
                        platform="bilibili",
                        supervised_tasks=(owned,),
                    )

            results = await asyncio.gather(
                owned,
                return_exceptions=True,
            )
            self.assertIsInstance(results[0], PermissionError)
            redis_ping.assert_awaited_once()
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
