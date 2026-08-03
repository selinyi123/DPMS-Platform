import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app import login_broker
from app.worker_identity import WORKER_ID
from shared.login_streams import (
    LOGIN_REQUEST_GROUP_NAME,
    LOGIN_REQUEST_STREAM_KEY,
)


class _Transaction:
    def __init__(self, database):
        self.database = database

    async def __aenter__(self):
        self.database.depth += 1
        return self

    async def __aexit__(self, _exc_type, *_args):
        self.database.depth -= 1
        return False


class LoginBrokerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enqueue_cleanup = AsyncMock()
        self.ensure_cleanup = AsyncMock(return_value=True)
        enqueue_patch = patch.object(
            login_broker,
            "enqueue_login_profile_cleanup",
            new=self.enqueue_cleanup,
        )
        ensure_patch = patch.object(
            login_broker,
            "ensure_login_profile_cleanup_completed",
            new=self.ensure_cleanup,
        )
        enqueue_patch.start()
        ensure_patch.start()
        self.addCleanup(enqueue_patch.stop)
        self.addCleanup(ensure_patch.stop)

    def test_login_transport_uses_shared_contract_and_stable_identity(self):
        self.assertEqual(
            login_broker.STREAM_KEY,
            LOGIN_REQUEST_STREAM_KEY,
        )
        self.assertEqual(
            login_broker.GROUP_NAME,
            LOGIN_REQUEST_GROUP_NAME,
        )
        self.assertEqual(login_broker.CONSUMER_NAME, WORKER_ID)

    def test_xiaohongshu_login_uses_explore_entry_contract(self):
        config = login_broker.get_platform("xiaohongshu")

        self.assertEqual(
            config["login_url"],
            "https://www.xiaohongshu.com/explore",
        )
        self.assertEqual(config["login_url"], config["account_check_url"])
        self.assertTrue(config["qr_login"])

    def test_known_provider_denials_fail_with_fixed_safe_reasons(self):
        xiaohongshu = login_broker.classify_login_page_blocker(
            "xiaohongshu",
            title="小红书",
            visible_text="安全限制 IP存在风险 300012",
        )
        self.assertEqual(
            xiaohongshu["error_code"],
            "xiaohongshu_login_network_risk_300012",
        )
        self.assertIn("official app or site", xiaohongshu["error_message"])
        self.assertNotIn("proxy", xiaohongshu["error_message"].casefold())
        douyin = login_broker.classify_login_page_blocker(
            "douyin",
            title="",
            visible_text=(
                '{"description":"非法应用","error_code":22}'
            ),
        )
        self.assertEqual(
            douyin["error_code"],
            "douyin_legacy_qr_endpoint_rejected",
        )
        verification = login_broker.classify_login_page_blocker(
            "douyin",
            title="验证码中间页",
            visible_text="",
        )
        self.assertEqual(
            verification["error_code"],
            "douyin_web_login_verification_required",
        )

    async def test_blocker_detection_keeps_title_when_body_is_unavailable(self):
        body = SimpleNamespace(
            inner_text=AsyncMock(side_effect=TimeoutError),
        )
        page = SimpleNamespace(
            title=AsyncMock(return_value="安全限制 300012"),
            locator=MagicMock(return_value=body),
        )

        blocker = await login_broker.detect_login_page_blocker(
            page,
            "xiaohongshu",
        )

        self.assertEqual(
            blocker["error_code"],
            "xiaohongshu_login_network_risk_300012",
        )

    def test_unrelated_login_page_is_not_classified_as_blocked(self):
        self.assertIsNone(
            login_broker.classify_login_page_blocker(
                "weibo",
                title="登录 - 微博",
                visible_text="请使用微博客户端扫码登录",
            )
        )

    async def test_xiaohongshu_qr_capture_uses_qr_element(self):
        qr = SimpleNamespace(
            wait_for=AsyncMock(),
            screenshot=AsyncMock(),
        )
        page = SimpleNamespace(
            locator=MagicMock(return_value=qr),
            screenshot=AsyncMock(),
        )
        image_path = Path("/profiles/login-sessions/qr.png")

        captured = await login_broker.capture_login_qr_image(
            page,
            "xiaohongshu",
            image_path,
            wait_milliseconds=500,
        )

        self.assertTrue(captured)
        page.locator.assert_called_once_with(
            login_broker.XIAOHONGSHU_QR_SELECTOR
        )
        qr.wait_for.assert_awaited_once_with(
            state="visible",
            timeout=500,
        )
        qr.screenshot.assert_awaited_once_with(path=str(image_path))
        page.screenshot.assert_not_awaited()

    async def test_xiaohongshu_cookie_requires_authenticated_dom(self):
        authenticated = SimpleNamespace(
            is_visible=AsyncMock(return_value=False),
        )
        page = SimpleNamespace(locator=MagicMock(return_value=authenticated))
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=[{"name": "web_session", "value": "present"}]
            )
        )

        cookies = await login_broker.authenticated_login_cookies(
            page,
            context,
            "xiaohongshu",
            {"web_session"},
        )

        self.assertIsNone(cookies)
        authenticated.is_visible.assert_awaited_once_with(timeout=3_000)

    async def test_superseded_login_owner_releases_its_slot(self):
        fake_database = SimpleNamespace(
            fetch_one=AsyncMock(
                return_value={"status": "expired", "not_expired": 0}
            )
        )
        with patch.object(login_broker, "database", fake_database):
            waiting = await login_broker.login_session_is_waiting("session-1")

        self.assertFalse(waiting)
        fake_database.fetch_one.assert_awaited_once()

    async def test_xiaohongshu_fast_scan_does_not_overwrite_real_qr(self):
        session_id = str(uuid.uuid4())
        login_url = "https://www.xiaohongshu.com/explore"
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
            screenshot=AsyncMock(),
        )
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        pool = SimpleNamespace(
            get_transient_context=AsyncMock(return_value=context),
            close_transient_context=AsyncMock(return_value=True),
        )
        capture = AsyncMock(return_value=True)
        complete = AsyncMock(side_effect=[False, True])
        settle = AsyncMock()

        with (
            patch.object(
                login_broker,
                "get_platform",
                return_value={
                    "login_url": login_url,
                    "required_cookies": ["web_session"],
                },
            ),
            patch.object(
                login_broker,
                "claim_login_session_for_processing",
                new=AsyncMock(
                    return_value={
                        "state": "claimed",
                        "remaining_seconds": 300.0,
                    }
                ),
            ),
            patch.object(
                login_broker,
                "login_session_owner_state",
                new=AsyncMock(return_value="owned"),
            ),
            patch.object(
                login_broker,
                "detect_login_page_blocker",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                login_broker,
                "complete_authenticated_login_if_ready",
                new=complete,
            ),
            patch.object(
                login_broker,
                "capture_login_qr_image",
                new=capture,
            ),
            patch.object(
                login_broker,
                "transition_login_session_to_waiting_scan",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                login_broker,
                "settle_login_session_without_account",
                new=settle,
            ),
        ):
            await login_broker.handle_login_session(
                pool,
                {
                    "session_id": session_id,
                    "platform": "xiaohongshu",
                    "login_url": login_url,
                },
            )

        page.goto.assert_awaited_once_with(
            login_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        self.assertEqual(capture.await_count, 1)
        self.assertLessEqual(
            capture.await_args.kwargs["wait_milliseconds"],
            login_broker.XIAOHONGSHU_QR_POLL_MILLISECONDS,
        )
        page.screenshot.assert_not_awaited()
        self.assertEqual(complete.await_count, 2)
        settle.assert_not_awaited()
        page.close.assert_awaited_once_with()
        pool.close_transient_context.assert_awaited_once_with(
            context,
            reason="qr_login_finished",
        )

    async def test_replaced_xiaohongshu_owner_stops_before_qr_wait(self):
        session_id = str(uuid.uuid4())
        login_url = "https://www.xiaohongshu.com/explore"
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
        )
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        pool = SimpleNamespace(
            get_transient_context=AsyncMock(return_value=context),
            close_transient_context=AsyncMock(return_value=True),
        )
        capture = AsyncMock()
        complete = AsyncMock()
        transition = AsyncMock()

        with (
            patch.object(
                login_broker,
                "get_platform",
                return_value={
                    "login_url": login_url,
                    "required_cookies": ["web_session"],
                },
            ),
            patch.object(
                login_broker,
                "claim_login_session_for_processing",
                new=AsyncMock(
                    return_value={
                        "state": "claimed",
                        "remaining_seconds": 300.0,
                    }
                ),
            ),
            patch.object(
                login_broker,
                "login_session_owner_state",
                new=AsyncMock(return_value="released"),
            ),
            patch.object(
                login_broker,
                "detect_login_page_blocker",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                login_broker,
                "complete_authenticated_login_if_ready",
                new=complete,
            ),
            patch.object(
                login_broker,
                "capture_login_qr_image",
                new=capture,
            ),
            patch.object(
                login_broker,
                "transition_login_session_to_waiting_scan",
                new=transition,
            ),
            patch.object(login_broker, "structured_log"),
        ):
            await login_broker.handle_login_session(
                pool,
                {
                    "session_id": session_id,
                    "platform": "xiaohongshu",
                    "login_url": login_url,
                },
            )

        complete.assert_not_awaited()
        capture.assert_not_awaited()
        transition.assert_not_awaited()
        page.close.assert_awaited_once_with()
        pool.close_transient_context.assert_awaited_once_with(
            context,
            reason="qr_login_finished",
        )

    async def test_cancelled_delivery_is_not_acknowledged(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_pool, _session):
            entered.set()
            await release.wait()

        fake_redis = SimpleNamespace(eval=AsyncMock())
        session_id = str(uuid.uuid4())
        with (
            patch.object(login_broker, "redis", fake_redis),
            patch.object(
                login_broker,
                "handle_login_session",
                new=handler,
            ),
        ):
            task = asyncio.create_task(
                login_broker.handle_and_ack(
                    object(),
                    "1-0",
                    {
                        "session_id": session_id,
                        "platform": "douyin",
                        "login_url": "https://example.invalid/login",
                    },
                    asyncio.Semaphore(1),
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        fake_redis.eval.assert_not_awaited()

    async def test_malformed_login_message_is_terminally_acked(self):
        fake_redis = SimpleNamespace(
            eval=AsyncMock(return_value=[1, 1])
        )
        handler = AsyncMock()
        with (
            patch.object(login_broker, "redis", fake_redis),
            patch.object(
                login_broker,
                "handle_login_session",
                new=handler,
            ),
            patch.object(login_broker, "structured_log"),
        ):
            await login_broker.handle_and_ack(
                object(),
                "1-0",
                {
                    "session_id": "../not-a-uuid",
                    "platform": "douyin",
                    "login_url": "https://example.invalid/login",
                },
                asyncio.Semaphore(1),
            )

        handler.assert_not_awaited()
        fake_redis.eval.assert_awaited_once()
        self.assertEqual(
            fake_redis.eval.await_args.args[1:],
            (
                1,
                login_broker.STREAM_KEY,
                login_broker.GROUP_NAME,
                "1-0",
            ),
        )

    async def test_terminal_ack_retains_entry_while_another_group_blocks(self):
        fake_redis = SimpleNamespace(
            eval=AsyncMock(return_value=[1, 0])
        )
        with patch.object(login_broker, "redis", fake_redis):
            result = (
                await login_broker.acknowledge_terminal_login_message(
                    "1-0"
                )
            )

        self.assertEqual(
            result,
            {"acknowledged": 1, "deleted": 0},
        )

    async def test_terminal_ack_accepts_delete_after_prior_partial_ack(self):
        fake_redis = SimpleNamespace(
            eval=AsyncMock(return_value=[0, 1])
        )
        with patch.object(login_broker, "redis", fake_redis):
            result = (
                await login_broker.acknowledge_terminal_login_message(
                    "1-0"
                )
            )

        self.assertEqual(
            result,
            {"acknowledged": 0, "deleted": 1},
        )

    async def test_login_loop_cancels_and_awaits_active_children(self):
        shutdown_event = asyncio.Event()
        child_started = asyncio.Event()
        child_cleaned = asyncio.Event()

        class FakeRedis:
            def __init__(self):
                self.reads = 0

            async def xinfo_groups(self, stream_key):
                self.asserted_stream = stream_key
                return [{"name": login_broker.GROUP_NAME}]

            async def xreadgroup(self, *_args, **_kwargs):
                self.reads += 1
                if self.reads == 1:
                    return [
                        (
                            login_broker.STREAM_KEY,
                            [
                                (
                                    "1-0",
                                    {
                                        "session_id": "session-1",
                                        "platform": "douyin",
                                    },
                                )
                            ],
                        )
                    ]
                await child_started.wait()
                shutdown_event.set()
                return []

        async def child(*_args, **_kwargs):
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                child_cleaned.set()

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(login_broker, "redis", FakeRedis()),
            patch.object(
                login_broker,
                "reclaim_stale_login_messages",
                new=AsyncMock(return_value=0),
            ),
            patch.object(login_broker, "handle_and_ack", new=child),
            patch.object(
                login_broker,
                "PROFILE_ROOT",
                Path(temp_dir),
            ),
        ):
            await login_broker.login_loop(object(), shutdown_event)

        self.assertTrue(child_started.is_set())
        self.assertTrue(child_cleaned.is_set())

    async def test_login_loop_leaves_backlog_in_redis_at_capacity(self):
        shutdown_event = asyncio.Event()
        children_started = 0
        capacity_reached = asyncio.Event()

        class FakeRedis:
            def __init__(self):
                self.reads = 0

            async def xinfo_groups(self, stream_key):
                self.asserted_stream = stream_key
                return [{"name": login_broker.GROUP_NAME}]

            async def xreadgroup(self, *_args, **_kwargs):
                self.reads += 1
                return [
                    (
                        login_broker.STREAM_KEY,
                        [
                            (
                                f"{self.reads}-0",
                                {
                                    "session_id": f"session-{self.reads}",
                                    "platform": "douyin",
                                },
                            )
                        ],
                    )
                ]

        fake_redis = FakeRedis()

        async def child(*_args, **_kwargs):
            nonlocal children_started
            children_started += 1
            if (
                children_started
                == login_broker.MAX_ACTIVE_LOGIN_SESSIONS
            ):
                capacity_reached.set()
            await asyncio.Event().wait()

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(login_broker, "redis", fake_redis),
            patch.object(
                login_broker,
                "reclaim_stale_login_messages",
                new=AsyncMock(return_value=0),
            ),
            patch.object(login_broker, "handle_and_ack", new=child),
            patch.object(
                login_broker,
                "PROFILE_ROOT",
                Path(temp_dir),
            ),
        ):
            loop_task = asyncio.create_task(
                login_broker.login_loop(object(), shutdown_event)
            )
            await capacity_reached.wait()
            await asyncio.sleep(0.05)
            self.assertEqual(
                fake_redis.reads,
                login_broker.MAX_ACTIVE_LOGIN_SESSIONS,
            )
            shutdown_event.set()
            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task

    async def test_stale_delivery_is_acked_only_after_db_settlement(self):
        session_id = str(uuid.uuid4())
        fake_redis = SimpleNamespace(
            xpending_range=AsyncMock(
                return_value=[{"message_id": "1-0"}]
            ),
            xclaim=AsyncMock(
                return_value=[
                    (
                        "1-0",
                        {
                            "session_id": session_id,
                            "platform": "douyin",
                            "login_url": "https://example.test/login",
                        },
                    )
                ]
            ),
            eval=AsyncMock(return_value=[1, 1]),
        )
        settle = AsyncMock(return_value=True)
        with (
            patch.object(login_broker, "redis", fake_redis),
            patch.object(
                login_broker,
                "settle_stale_login_session",
                new=settle,
            ),
        ):
            self.assertEqual(
                1,
                await login_broker.reclaim_stale_login_messages(
                    object()
                ),
            )

        settle.assert_awaited_once_with(session_id)
        fake_redis.eval.assert_awaited_once()
        self.assertEqual(
            fake_redis.eval.await_args.args[1:],
            (
                1,
                login_broker.STREAM_KEY,
                login_broker.GROUP_NAME,
                "1-0",
            ),
        )

    async def test_stale_poison_delivery_is_acked_without_db_settlement(self):
        fake_redis = SimpleNamespace(
            xpending_range=AsyncMock(
                return_value=[{"message_id": "1-0"}]
            ),
            xclaim=AsyncMock(
                return_value=[
                    (
                        "1-0",
                        {
                            "session_id": "../not-a-uuid",
                            "platform": "douyin",
                            "login_url": "https://example.test/login",
                        },
                    )
                ]
            ),
            eval=AsyncMock(return_value=[1, 1]),
        )
        settle = AsyncMock()
        with (
            patch.object(login_broker, "redis", fake_redis),
            patch.object(
                login_broker,
                "settle_stale_login_session",
                new=settle,
            ),
            patch.object(login_broker, "structured_log"),
        ):
            self.assertEqual(
                1,
                await login_broker.reclaim_stale_login_messages(
                    object()
                ),
            )

        settle.assert_not_awaited()
        fake_redis.eval.assert_awaited_once()

    async def test_account_and_confirmed_session_commit_in_one_transaction(self):
        class Database:
            def __init__(self):
                self.depth = 0
                self.calls = []

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, query, values=None):
                self.calls.append(("fetch", query, values, self.depth))
                if "ROW_COUNT()" in query:
                    return {"affected": 1}
                return {
                    "status": "waiting_scan",
                    "account_id": None,
                    "platform": "douyin",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "not_expired": 1,
                }

            async def execute(self, query, values=None):
                self.calls.append(("execute", query, values, self.depth))
                if "INSERT INTO accounts" in query:
                    return 7
                return 1

        database = Database()
        event_depths = []

        async def record_event(**_kwargs):
            event_depths.append(database.depth)

        with (
            patch.object(login_broker, "database", database),
            patch.object(
                login_broker,
                "ensure_default_fingerprint",
                new=AsyncMock(return_value=3),
            ),
            patch.object(
                login_broker,
                "queue_account_calibration",
                new=AsyncMock(
                    return_value={"calibration_id": "calibration-1"}
                ),
            ),
            patch.object(
                login_broker.cookie_vault,
                "encrypt",
                return_value=b"encrypted",
            ),
            patch.object(
                login_broker,
                "serialize_cookies",
                return_value="[]",
            ),
            patch.object(
                login_broker,
                "record_event",
                new=record_event,
            ),
        ):
            created = await login_broker.create_account_from_cookies(
                "douyin",
                [{"name": "sessionid", "value": "redacted"}],
                session_id="session-1",
            )

        self.assertTrue(created["created"])
        account_insert = next(
            call
            for call in database.calls
            if "INSERT INTO accounts" in call[1]
        )
        session_update = next(
            call
            for call in database.calls
            if "UPDATE login_sessions" in call[1]
        )
        self.assertGreater(account_insert[3], 0)
        self.assertGreater(session_update[3], 0)
        self.assertEqual(session_update[2]["account_id"], 7)
        self.assertEqual(len(event_depths), 2)
        self.assertTrue(all(depth > 0 for depth in event_depths))

    async def test_expired_session_cannot_create_an_account(self):
        class Database:
            def __init__(self):
                self.depth = 0
                self.execute = AsyncMock()

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, _query, _values=None):
                return {
                    "status": "waiting_scan",
                    "account_id": None,
                    "platform": "douyin",
                    "expires_at": "2020-01-01T00:00:00Z",
                    "not_expired": 0,
                }

        database = Database()
        settle = AsyncMock(return_value="expired")
        with (
            patch.object(login_broker, "database", database),
            patch.object(
                login_broker,
                "ensure_default_fingerprint",
                new=AsyncMock(return_value=3),
            ),
            patch.object(
                login_broker.cookie_vault,
                "encrypt",
                return_value=b"encrypted",
            ),
            patch.object(
                login_broker,
                "serialize_cookies",
                return_value="[]",
            ),
            patch.object(
                login_broker,
                "settle_login_session_without_account",
                new=settle,
            ),
        ):
            result = await login_broker.create_account_from_cookies(
                "douyin",
                [{"name": "sessionid", "value": "redacted"}],
                session_id="session-1",
            )

        self.assertEqual(result["status"], "expired")
        self.assertIsNone(result["account_id"])
        self.assertFalse(
            any(
                "INSERT INTO accounts" in call.args[0]
                for call in database.execute.await_args_list
            )
        )
        settle.assert_awaited_once()

    async def test_failure_settlement_never_overwrites_confirmed_session(self):
        class Database:
            def __init__(self):
                self.depth = 0
                self.execute = AsyncMock()

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, _query, _values=None):
                return {
                    "status": "confirmed",
                    "account_id": 7,
                    "platform": "douyin",
                }

        database = Database()
        event = AsyncMock()
        with (
            patch.object(login_broker, "database", database),
            patch.object(login_broker, "record_event", new=event),
        ):
            state = await login_broker.settle_login_session_without_account(
                "session-1",
                status="failed",
                error_message="safe failure",
                event_type="QrLoginFailed",
                error_code="safe_failure",
            )

        self.assertEqual(state, "confirmed")
        database.execute.assert_not_awaited()
        event.assert_not_awaited()

    async def test_duplicate_delivery_cannot_reopen_confirmed_session(self):
        session_id = str(uuid.uuid4())

        class Database:
            def __init__(self):
                self.depth = 0
                self.execute = AsyncMock()

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, _query, _values=None):
                return {
                    "status": "confirmed",
                    "account_id": 7,
                    "platform": "douyin",
                    "login_url": "https://example.invalid/login",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "not_expired": 1,
                    "remaining_microseconds": 1_000_000,
                }

        database = Database()
        pool = SimpleNamespace(
            get_transient_context=AsyncMock(),
        )
        with (
            patch.object(login_broker, "database", database),
            patch.object(login_broker, "structured_log"),
        ):
            await login_broker.handle_login_session(
                pool,
                {
                    "session_id": session_id,
                    "platform": "douyin",
                    "login_url": "https://example.invalid/login",
                },
            )

        database.execute.assert_not_awaited()
        pool.get_transient_context.assert_not_awaited()

    async def test_terminal_binding_mismatch_repairs_cleanup_intent(self):
        session_id = str(uuid.uuid4())

        class Database:
            def __init__(self):
                self.depth = 0
                self.execute = AsyncMock()

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, _query, _values=None):
                return {
                    "status": "confirmed",
                    "account_id": 7,
                    "platform": "weibo",
                    "login_url": "https://example.invalid/weibo-login",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "not_expired": 1,
                    "remaining_microseconds": 1_000_000,
                }

        database = Database()
        with patch.object(login_broker, "database", database):
            result = (
                await login_broker.claim_login_session_for_processing(
                    {
                        "session_id": session_id,
                        "platform": "douyin",
                        "login_url": "https://example.invalid/login",
                    },
                    qr_image_path="/profiles/login.png",
                )
            )

        self.assertEqual(result, {"state": "binding_mismatch"})
        self.enqueue_cleanup.assert_awaited_once_with(session_id)
        database.execute.assert_not_awaited()

    async def test_claim_expiry_boundary_converges_queued_to_expired(self):
        session_id = str(uuid.uuid4())

        class Database:
            def __init__(self):
                self.depth = 0
                self.row_counts = iter((0, 1))
                self.executions = []

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, query, _values=None):
                if "FROM login_sessions" in query:
                    return {
                        "status": "queued",
                        "account_id": None,
                        "platform": "douyin",
                        "login_url": "https://example.invalid/login",
                        "expires_at": "2099-01-01T00:00:00Z",
                        "not_expired": 1,
                        "remaining_microseconds": 1,
                    }
                if "ROW_COUNT()" in query:
                    return {"affected": next(self.row_counts)}
                raise AssertionError(query)

            async def execute(self, query, values=None):
                self.executions.append((query, values))

        database = Database()
        with (
            patch.object(login_broker, "database", database),
            patch.object(
                login_broker,
                "record_event",
                new=AsyncMock(),
            ),
        ):
            result = (
                await login_broker.claim_login_session_for_processing(
                    {
                        "session_id": session_id,
                        "platform": "douyin",
                        "login_url": "https://example.invalid/login",
                    },
                    qr_image_path="/profiles/login.png",
                )
            )

        self.assertEqual(result["state"], "expired")
        self.assertEqual(len(database.executions), 2)
        self.assertIn(
            "expires_at <= NOW()",
            database.executions[1][0],
        )

    async def test_waiting_scan_expiry_boundary_converges_opening(self):
        class Database:
            def __init__(self):
                self.depth = 0
                self.row_counts = iter((0, 1))
                self.executions = []

            def transaction(self):
                return _Transaction(self)

            async def fetch_one(self, query, _values=None):
                if "FROM login_sessions" in query:
                    return {
                        "status": "opening",
                        "not_expired": 1,
                    }
                if "ROW_COUNT()" in query:
                    return {"affected": next(self.row_counts)}
                raise AssertionError(query)

            async def execute(self, query, values=None):
                self.executions.append((query, values))

        database = Database()
        with (
            patch.object(login_broker, "database", database),
            patch.object(
                login_broker,
                "record_event",
                new=AsyncMock(),
            ),
        ):
            transitioned = await (
                login_broker.transition_login_session_to_waiting_scan(
                    "session-1",
                    platform="douyin",
                    qr_image_path="/profiles/login.png",
                )
            )

        self.assertFalse(transitioned)
        self.assertEqual(len(database.executions), 2)
        self.assertIn(
            "expires_at <= NOW()",
            database.executions[1][0],
        )

    async def test_cancelled_browser_session_closes_tracked_context(self):
        goto_started = asyncio.Event()
        page = SimpleNamespace(
            goto=AsyncMock(),
            close=AsyncMock(),
        )

        async def goto(*_args, **_kwargs):
            goto_started.set()
            await asyncio.Event().wait()

        page.goto.side_effect = goto
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
        )
        pool = SimpleNamespace(
            get_transient_context=AsyncMock(return_value=context),
            close_transient_context=AsyncMock(return_value=True),
        )
        with (
            patch.object(
                login_broker,
                "get_platform",
                return_value={
                    "login_url": "https://example.invalid/login",
                    "required_cookies": ["sessionid"],
                },
            ),
            patch.object(
                login_broker,
                "claim_login_session_for_processing",
                new=AsyncMock(
                    return_value={
                        "state": "claimed",
                        "remaining_seconds": 300.0,
                    }
                ),
            ),
            patch.object(
                login_broker,
                "transition_login_session_to_waiting_scan",
                new=AsyncMock(return_value=True),
            ),
        ):
            task = asyncio.create_task(
                login_broker.handle_login_session(
                    pool,
                    {
                        "session_id": str(uuid.uuid4()),
                        "platform": "douyin",
                        "login_url": "https://example.invalid/login",
                    },
                )
            )
            await goto_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        page.close.assert_awaited_once_with()
        pool.close_transient_context.assert_awaited_once_with(
            context,
            reason="qr_login_finished",
        )


if __name__ == "__main__":
    unittest.main()
