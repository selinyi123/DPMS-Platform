import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import main


class CoreBackgroundSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_startup_only_verifies_schema(self):
        with (
            patch.object(
                main,
                "verify_migrations_current",
                new=AsyncMock(),
            ) as verify,
            patch.object(
                main,
                "ensure_runtime_schema",
                new=AsyncMock(),
            ) as ensure,
        ):
            applied = await main.prepare_runtime_schema("production")

        self.assertEqual(applied, [])
        verify.assert_awaited_once()
        ensure.assert_not_awaited()

    async def test_development_also_requires_explicit_migration(self):
        with (
            patch.object(
                main,
                "verify_migrations_current",
                new=AsyncMock(),
            ) as verify,
            patch.object(
                main,
                "ensure_runtime_schema",
                new=AsyncMock(),
            ) as ensure,
        ):
            applied = await main.prepare_runtime_schema("dev")

        self.assertEqual(applied, [])
        verify.assert_awaited_once()
        ensure.assert_not_awaited()

    async def test_exception_and_normal_return_are_recorded(self):
        async def returns():
            return None

        async def raises():
            raise RuntimeError("sensitive failure detail")

        app = SimpleNamespace(state=SimpleNamespace())
        with (
            patch.object(
                main,
                "CORE_BACKGROUND_TASKS",
                (("test-return", returns), ("test-exception", raises)),
            ),
            patch.object(main, "structured_log") as log,
        ):
            tasks = main._start_core_background_tasks(app)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertEqual(
            app.state.background_task_failures,
            {
                "core:test-return": {
                    "outcome": "unexpected_return",
                    "exception_type": None,
                },
                "core:test-exception": {
                    "outcome": "exception",
                    "exception_type": "RuntimeError",
                },
            },
        )
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], RuntimeError)
        self.assertEqual(log.call_count, 2)

    async def test_health_returns_503_without_leaking_exception_detail(self):
        main.app.state.background_task_failures = {
            "core:outbox-dispatcher": {
                "outcome": "exception",
                "exception_type": "RuntimeError",
            }
        }
        try:
            with (
                patch.object(
                    main.database,
                    "fetch_one",
                    new=AsyncMock(return_value={"ok": 1}),
                ),
                patch.object(
                    main.redis,
                    "_conn",
                    SimpleNamespace(ping=AsyncMock(return_value=True)),
                ),
            ):
                response = await main.health()
        finally:
            main.app.state.background_task_failures = {}

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["background_tasks"])
        self.assertEqual(
            payload["failed_background_tasks"],
            {"core:outbox-dispatcher": "exception"},
        )
        self.assertNotIn("RuntimeError", response.body.decode("utf-8"))

    async def test_health_returns_503_when_a_required_dependency_is_down(self):
        main.app.state.background_task_failures = {}
        cases = (
            (RuntimeError("database unavailable"), True, False, True),
            ({"ok": 1}, RuntimeError("redis unavailable"), True, False),
        )
        for database_result, redis_result, expected_db, expected_redis in cases:
            database_call = (
                AsyncMock(side_effect=database_result)
                if isinstance(database_result, Exception)
                else AsyncMock(return_value=database_result)
            )
            redis_call = (
                AsyncMock(side_effect=redis_result)
                if isinstance(redis_result, Exception)
                else AsyncMock(return_value=redis_result)
            )
            with (
                self.subTest(db=expected_db, redis=expected_redis),
                patch.object(main.database, "fetch_one", new=database_call),
                patch.object(
                    main.redis,
                    "_conn",
                    SimpleNamespace(ping=redis_call),
                ),
            ):
                response = await main.health()
                self.assertEqual(response.status_code, 503)
                payload = json.loads(response.body)
                self.assertEqual(payload["status"], "degraded")
                self.assertEqual(payload["db"], expected_db)
                self.assertEqual(payload["redis"], expected_redis)

    async def test_api_fails_closed_after_background_task_exit(self):
        main.app.state.background_task_failures = {
            "core:recovery": {
                "outcome": "unexpected_return",
                "exception_type": None,
            }
        }
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/lotteries/"),
            state=SimpleNamespace(),
        )
        call_next = AsyncMock()
        try:
            response = await main.require_admin_token(request, call_next)
        finally:
            main.app.state.background_task_failures = {}

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.body),
            {"detail": "Core background service unavailable"},
        )
        call_next.assert_not_awaited()

    async def test_lifespan_stops_background_tasks_before_clients_close(self):
        release = asyncio.Event()
        events = []

        def loop_factory(name):
            async def loop():
                try:
                    await release.wait()
                finally:
                    events.append(f"{name}:stopped")

            return loop

        task_factories = tuple(
            (name, loop_factory(name))
            for name in ("recovery", "notification", "outbox", "scheduler")
        )

        async def disconnect():
            self.assertEqual(
                {event for event in events if event.endswith(":stopped")},
                {
                    "recovery:stopped",
                    "notification:stopped",
                    "outbox:stopped",
                    "scheduler:stopped",
                },
            )
            events.append("database:closed")

        async def close_redis():
            self.assertIn("database:closed", events)
            events.append("redis:closed")

        app = SimpleNamespace(state=SimpleNamespace())
        with (
            patch.object(main, "CORE_BACKGROUND_TASKS", task_factories),
            patch.object(
                main.database,
                "connect",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                main.redis,
                "initialize",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                main,
                "ensure_runtime_schema",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                main,
                "verify_migrations_current",
                new=AsyncMock(return_value=None),
            ),
            patch.object(main, "secret_posture", return_value=[]),
            patch.object(
                main.database,
                "fetch_one",
                new=AsyncMock(return_value={"ok": 1}),
            ),
            patch.object(
                main.redis,
                "_conn",
                SimpleNamespace(ping=AsyncMock(return_value=True)),
            ),
            patch.object(
                main,
                "reconcile_owned_stream_epochs",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                main.database,
                "disconnect",
                new=AsyncMock(side_effect=disconnect),
            ),
            patch.object(
                main.redis,
                "close",
                new=AsyncMock(side_effect=close_redis),
            ),
        ):
            context = main.lifespan(app)
            await context.__aenter__()
            await asyncio.sleep(0)
            await context.__aexit__(None, None, None)

        self.assertEqual(events[-2:], ["database:closed", "redis:closed"])
        self.assertEqual(app.state.background_tasks, ())
        self.assertEqual(app.state.background_task_failures, {})


if __name__ == "__main__":
    unittest.main()
