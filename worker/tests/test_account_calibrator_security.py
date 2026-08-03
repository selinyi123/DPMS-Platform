"""Security regression tests for account calibration authority and identity."""

from __future__ import annotations

import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import account_calibrator, safety  # noqa: E402


CALIBRATION_ID = "91fa2070-e210-47ba-9d2a-0a0bbcc4cc00"
ACCOUNT_ID = 73
AUTHORITATIVE_URL = "https://www.xiaohongshu.com/explore"
EXECUTION_REVISION = 7


class ClaimDatabase:
    def __init__(self, *, check_url: str = AUTHORITATIVE_URL):
        self.check_url = check_url
        self.transaction_active = False
        self.rolled_back = False
        self.fetches = []

    @asynccontextmanager
    async def transaction(self):
        self.transaction_active = True
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        finally:
            self.transaction_active = False

    async def fetch_one(self, query, values):
        self.assert_transactional()
        self.fetches.append((" ".join(query.split()), dict(values)))
        return {
            "calibration_id": CALIBRATION_ID,
            "account_id": ACCOUNT_ID,
            "platform": "xiaohongshu",
            "check_url": self.check_url,
            "staged_result": None,
            "calibration_status": "queued",
            "account_platform": "xiaohongshu",
            "account_execution_revision": EXECUTION_REVISION,
        }

    def assert_transactional(self):
        if not self.transaction_active:
            raise AssertionError("authoritative calibration read must be transactional")


class CalibrationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_tampered_queue_check_url_is_rejected_without_browser_access(self):
        db = ClaimDatabase()
        affected = AsyncMock(
            side_effect=AssertionError("tampered message must not claim the row")
        )
        pool = SimpleNamespace(get_account_context=AsyncMock())

        with (
            patch.object(account_calibrator, "database", db),
            patch.object(account_calibrator, "execute_affected_rows", affected),
            patch.object(account_calibrator, "record_event", AsyncMock()) as event,
            patch.object(account_calibrator, "structured_log") as log,
        ):
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    "platform": "xiaohongshu",
                    "check_url": "https://attacker.example/steal",
                },
            )

        self.assertTrue(db.rolled_back)
        self.assertEqual(len(db.fetches), 1)
        self.assertIn("FOR UPDATE", db.fetches[0][0])
        affected.assert_not_awaited()
        event.assert_not_awaited()
        pool.get_account_context.assert_not_awaited()
        self.assertEqual(
            log.call_args.args[:2],
            ("warning", "account_calibration_claim_rejected"),
        )

    async def test_legacy_message_without_platform_or_url_uses_database_binding(self):
        db = ClaimDatabase()

        async def affected(query, values, *, db):
            db.assert_transactional()
            self.assertIn("status = 'queued'", query)
            self.assertEqual(values["calibration_id"], CALIBRATION_ID)
            return 1

        with patch.object(account_calibrator, "database", db), patch.object(
            account_calibrator, "execute_affected_rows", affected
        ):
            claim = await account_calibrator.claim_calibration_message(
                CALIBRATION_ID,
                ACCOUNT_ID,
            )

        self.assertEqual(claim["check_url"], AUTHORITATIVE_URL)
        self.assertEqual(claim["platform"], "xiaohongshu")
        self.assertEqual(claim["execution_revision"], EXECUTION_REVISION)
        self.assertFalse(db.rolled_back)

    async def test_explicit_tampered_queue_platform_is_rejected(self):
        db = ClaimDatabase()
        affected = AsyncMock(
            side_effect=AssertionError("tampered platform must not claim the row")
        )

        with patch.object(account_calibrator, "database", db), patch.object(
            account_calibrator, "execute_affected_rows", affected
        ):
            claim = await account_calibrator.claim_calibration_message(
                CALIBRATION_ID,
                ACCOUNT_ID,
                "bilibili",
            )

        self.assertIsNone(claim)
        self.assertTrue(db.rolled_back)
        affected.assert_not_awaited()

    async def test_invalid_authoritative_url_fails_before_browser_context(self):
        db = ClaimDatabase(check_url="https://attacker.example/steal")
        affected_queries = []

        async def affected(query, values, *, db):
            del values
            affected_queries.append(" ".join(query.split()))
            return 1

        pool = SimpleNamespace(get_account_context=AsyncMock())
        with (
            patch.object(account_calibrator, "database", db),
            patch.object(account_calibrator, "execute_affected_rows", affected),
            patch.object(account_calibrator, "record_event", AsyncMock()),
            patch.object(
                account_calibrator,
                "emit_calibration_terminal_observability",
                AsyncMock(),
            ),
            patch.object(account_calibrator, "structured_log"),
        ):
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    "platform": "xiaohongshu",
                },
            )

        pool.get_account_context.assert_not_awaited()
        self.assertEqual(len(affected_queries), 2)
        self.assertIn("SET status = 'running'", affected_queries[0])
        self.assertIn("SET status = 'failed'", affected_queries[1])

    def test_four_platform_manifest_check_urls_are_valid_browser_entries(self):
        for platform in ("bilibili", "weibo", "xiaohongshu", "douyin"):
            with self.subTest(platform=platform):
                cfg = account_calibrator.get_platform(platform)
                check_url = cfg["account_check_url"]
                login_url = cfg["login_url"]
                self.assertEqual(
                    account_calibrator.validated_calibration_browser_entry_url(
                        platform, check_url
                    ),
                    check_url,
                )
                self.assertEqual(
                    account_calibrator.validated_calibration_browser_navigation_url(
                        platform, login_url
                    ),
                    login_url,
                )

    def test_calibration_browser_entry_rejects_untrusted_or_unlisted_urls(self):
        for value in (
            "http://www.xiaohongshu.com/explore",
            "https://attacker.example/steal",
            "https://www.xiaohongshu.com:444/explore",
            "https://user:pass@www.xiaohongshu.com/explore",
            "https://www.xiaohongshu.com/explore?next=https://attacker.example",
            "https://www.xiaohongshu.com/explore;unexpected",
            "https://www.xiaohongshu.com/unlisted",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "account_calibration_navigation_target_not_allowed",
                ):
                    account_calibrator.validated_calibration_browser_entry_url(
                        "xiaohongshu", value
                    )

    async def test_navigation_guard_aborts_cross_platform_redirect(self):
        main_frame = object()

        class Page:
            def __init__(self):
                self.main_frame = main_frame
                self.handler = None

            async def route(self, pattern, handler):
                self.assert_pattern(pattern)
                self.handler = handler

            @staticmethod
            def assert_pattern(pattern):
                if pattern != "**/*":
                    raise AssertionError(pattern)

        page = Page()
        await account_calibrator.install_calibration_navigation_guard(
            page, "xiaohongshu"
        )
        request = SimpleNamespace(
            url="https://attacker.example/steal",
            frame=main_frame,
            is_navigation_request=lambda: True,
        )
        route = SimpleNamespace(
            request=request,
            abort=AsyncMock(),
            continue_=AsyncMock(),
        )

        await page.handler(route)

        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()

    async def test_oauth_api_calibration_never_enters_browser_navigation(self):
        oauth_envelope = {
            "credential_kind": "weibo_oauth",
            "access_token": "ascii-token",
            "uid": "12345678",
            "expires_at": "2099-01-01T00:00:00Z",
        }

        class Database:
            async def fetch_one(self, query, values):
                del query, values
                return {
                    "platform": "weibo",
                    "encrypted_credential": b"purpose-bound",
                    "execution_revision": EXECUTION_REVISION,
                }

        class Vault:
            def decrypt_strict(self, blob, *, aad):
                del blob, aad
                return oauth_envelope

            def decrypt(self, *args, **kwargs):
                raise AssertionError("OAuth must use strict purpose binding")

        claim = {
            "calibration_id": CALIBRATION_ID,
            "account_id": ACCOUNT_ID,
            "platform": "weibo",
            "check_url": account_calibrator.WEIBO_OAUTH_IDENTITY_URL,
            "staged_result": None,
            "execution_revision": EXECUTION_REVISION,
        }
        oauth_handler = AsyncMock()
        pool = SimpleNamespace(get_account_context=AsyncMock())
        with (
            patch.object(
                account_calibrator,
                "claim_calibration_message",
                AsyncMock(return_value=claim),
            ),
            patch.object(account_calibrator, "database", Database()),
            patch.object(account_calibrator, "cookie_vault", Vault()),
            patch.object(
                account_calibrator,
                "handle_weibo_oauth_calibration",
                oauth_handler,
            ),
            patch.object(account_calibrator, "record_event", AsyncMock()),
            patch.object(
                account_calibrator,
                "validated_calibration_browser_entry_url",
                side_effect=AssertionError("OAuth must not use browser URL validation"),
            ),
        ):
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    "platform": "weibo",
                    "check_url": account_calibrator.WEIBO_OAUTH_IDENTITY_URL,
                    # A forged queue discriminator cannot force browser routing.
                    "calibration_kind": "browser_session",
                },
            )

        pool.get_account_context.assert_not_awaited()
        oauth_handler.assert_awaited_once()
        self.assertFalse(oauth_handler.await_args.kwargs["capability_calibration"])


class CalibrationRevisionBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaced_credential_is_rejected_before_cookie_decryption(self):
        class Database:
            async def fetch_one(self, query, values):
                self.query = " ".join(query.split())
                self.values = dict(values)
                return {
                    "encrypted_credential": b"new-credential",
                    "execution_revision": EXECUTION_REVISION + 1,
                }

        database = Database()
        vault = SimpleNamespace(decrypt=AsyncMock())
        inject = AsyncMock()
        with (
            patch.object(account_calibrator, "database", database),
            patch.object(account_calibrator, "cookie_vault", vault),
            patch.object(account_calibrator, "inject_account_cookies", inject),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "account_calibration_execution_revision_mismatch",
            ):
                await account_calibrator.inject_calibration_cookies(
                    SimpleNamespace(),
                    ACCOUNT_ID,
                    "xiaohongshu",
                    expected_execution_revision=EXECUTION_REVISION,
                )

        self.assertIn("execution_revision", database.query)
        self.assertEqual(database.values["id"], ACCOUNT_ID)
        vault.decrypt.assert_not_called()
        inject.assert_not_awaited()

    async def test_page_risk_revision_mismatch_writes_no_status_or_evidence(self):
        @asynccontextmanager
        async def transaction():
            yield

        database = SimpleNamespace(
            transaction=transaction,
            execute=AsyncMock(),
        )
        redis = SimpleNamespace(xadd=AsyncMock())
        revision_cas = AsyncMock(return_value=0)
        page = SimpleNamespace(url="https://www.xiaohongshu.com/login")

        with (
            patch.object(safety, "database", database),
            patch.object(safety, "redis", redis),
            patch.object(safety, "execute_affected_rows", revision_cas),
        ):
            with self.assertRaises(safety.AccountExecutionRevisionMismatch):
                await safety.detect_page_risk(
                    page,
                    ACCOUNT_ID,
                    "xiaohongshu",
                    expected_execution_revision=EXECUTION_REVISION,
                )

        revision_cas.assert_awaited_once()
        self.assertEqual(
            revision_cas.await_args.args[1]["execution_revision"],
            EXECUTION_REVISION,
        )
        database.execute.assert_not_awaited()
        redis.xadd.assert_not_awaited()

    async def test_missing_cookie_revision_mismatch_writes_no_risk_evidence(self):
        @asynccontextmanager
        async def transaction():
            yield

        database = SimpleNamespace(
            transaction=transaction,
            execute=AsyncMock(),
        )
        revision_cas = AsyncMock(return_value=0)
        domain_event = AsyncMock()

        with (
            patch.object(account_calibrator, "database", database),
            patch.object(
                account_calibrator, "execute_affected_rows", revision_cas
            ),
            patch.object(account_calibrator, "record_event", domain_event),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "account_calibration_execution_revision_mismatch",
            ):
                await account_calibrator.mark_account_login_required(
                    ACCOUNT_ID,
                    "missing cookies",
                    expected_execution_revision=EXECUTION_REVISION,
                )

        revision_cas.assert_awaited_once()
        self.assertEqual(
            revision_cas.await_args.args[1]["execution_revision"],
            EXECUTION_REVISION,
        )
        database.execute.assert_not_awaited()
        domain_event.assert_not_awaited()

    async def test_concurrent_replacement_cannot_settle_old_browser_identity(self):
        class SettlementDatabase:
            def __init__(self):
                self.lock_reads = []

            @asynccontextmanager
            async def transaction(self):
                yield

            async def fetch_one(self, query, values):
                self.lock_reads.append((" ".join(query.split()), dict(values)))
                return {
                    "calibration_status": "running",
                    "account_status": "warming",
                    # Simulate credential replacement after the old browser
                    # identity was verified but before settlement acquired the
                    # account lock.
                    "execution_revision": EXECUTION_REVISION + 1,
                }

        database = SettlementDatabase()
        page = SimpleNamespace(
            url=AUTHORITATIVE_URL,
            goto=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            screenshot=AsyncMock(),
            close=AsyncMock(),
        )
        context = SimpleNamespace(
            new_page=AsyncMock(return_value=page),
            cookies=AsyncMock(return_value=[{"name": "web_session"}]),
        )
        pool = SimpleNamespace(
            get_account_context=AsyncMock(return_value=context)
        )
        claim = {
            "calibration_id": CALIBRATION_ID,
            "account_id": ACCOUNT_ID,
            "platform": "xiaohongshu",
            "check_url": AUTHORITATIVE_URL,
            "staged_result": None,
            "execution_revision": EXECUTION_REVISION,
        }
        writes = []

        async def affected(query, values, *, db):
            self.assertIs(db, database)
            writes.append((" ".join(query.split()), dict(values)))
            if "SET status = 'failed'" in query:
                return 1
            raise AssertionError("stale calibration attempted a success write")

        inject = AsyncMock()
        risk = AsyncMock()
        with (
            patch.object(
                account_calibrator,
                "claim_calibration_message",
                AsyncMock(return_value=claim),
            ),
            patch.object(account_calibrator, "database", database),
            patch.object(account_calibrator, "execute_affected_rows", affected),
            patch.object(account_calibrator, "record_event", AsyncMock()),
            patch.object(
                account_calibrator,
                "install_calibration_navigation_guard",
                AsyncMock(),
            ),
            patch.object(
                account_calibrator, "inject_calibration_cookies", inject
            ),
            patch.object(account_calibrator, "detect_page_risk", risk),
            patch.object(
                account_calibrator,
                "verify_platform_identity",
                AsyncMock(return_value={"verified": True, "user_id": "old"}),
            ),
            patch.object(
                account_calibrator,
                "safe_title",
                AsyncMock(return_value="profile"),
            ),
            patch.object(
                account_calibrator,
                "emit_calibration_terminal_observability",
                AsyncMock(),
            ),
            patch.object(account_calibrator, "structured_log"),
        ):
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    # Queue revisions are compatibility-only data and must not
                    # override the authoritative revision from the claim.
                    "execution_revision": "999",
                },
            )

        inject.assert_awaited_once_with(
            context,
            ACCOUNT_ID,
            "xiaohongshu",
            expected_execution_revision=EXECUTION_REVISION,
        )
        risk.assert_awaited_once_with(
            page,
            ACCOUNT_ID,
            "xiaohongshu",
            expected_execution_revision=EXECUTION_REVISION,
        )
        self.assertEqual(len(database.lock_reads), 1)
        self.assertIn("FOR UPDATE", database.lock_reads[0][0])
        self.assertEqual(len(writes), 1)
        self.assertIn("SET status = 'failed'", writes[0][0])
        self.assertIn(
            "account_calibration_execution_revision_mismatch",
            writes[0][1]["error"],
        )
        self.assertNotIn("UPDATE accounts", writes[0][0])


class BilibiliCalibrationIdentityTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_verified_nav_persists_only_public_identity_metadata(self):
        response = SimpleNamespace(
            ok=True,
            status=200,
            json=AsyncMock(
                return_value={
                    "code": 0,
                    "data": {
                        "isLogin": True,
                        "mid": 111,
                        "uname": " 真实昵称 ",
                        "level_info": {"current_level": 6},
                        "official": {"title": "官方认证", "desc": "not persisted"},
                        "vip": {"label": {"text": "年度大会员"}},
                        "face": "https://example.invalid/avatar.jpg",
                        "credential_fingerprint": "must-not-persist",
                    },
                }
            ),
        )
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=[{"name": "DedeUserID", "value": "111"}]
            ),
            request=SimpleNamespace(get=AsyncMock(return_value=response)),
        )

        identity = await account_calibrator.verify_bilibili_identity(context)

        self.assertEqual(
            identity,
            {
                "verified": True,
                "method": "bilibili_nav",
                "mid": "111",
                "uid": "111",
                "nickname": "真实昵称",
                "level": 6,
                "title": "官方认证",
            },
        )
        self.assertNotIn("must-not-persist", repr(identity))
        self.assertNotIn("avatar", repr(identity))

    async def test_member_label_is_used_only_when_official_title_is_empty(self):
        response = SimpleNamespace(
            ok=True,
            status=200,
            json=AsyncMock(
                return_value={
                    "code": 0,
                    "data": {
                        "isLogin": True,
                        "mid": 111,
                        "official": {"title": ""},
                        "vip": {"label": {"text": "大会员"}},
                    },
                }
            ),
        )
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=[{"name": "DedeUserID", "value": "111"}]
            ),
            request=SimpleNamespace(get=AsyncMock(return_value=response)),
        )

        identity = await account_calibrator.verify_bilibili_identity(context)

        self.assertEqual(identity["title"], "大会员")

    async def test_authenticated_mid_must_match_declared_cookie_subject(self):
        response = SimpleNamespace(
            ok=True,
            status=200,
            json=AsyncMock(
                return_value={
                    "code": 0,
                    "data": {"isLogin": True, "mid": 222},
                }
            ),
        )
        context = SimpleNamespace(
            cookies=AsyncMock(
                return_value=[
                    {"name": "SESSDATA", "value": "session-b"},
                    {"name": "DedeUserID", "value": "111"},
                ]
            ),
            request=SimpleNamespace(
                get=AsyncMock(return_value=response)
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "bilibili_authenticated_identity_mismatch",
        ):
            await account_calibrator.verify_bilibili_identity(context)

        context.cookies.assert_awaited_once_with(
            "https://api.bilibili.com/"
        )


class FakeIdentityResponse:
    def __init__(self, *, ok=True, status=200, payload=None):
        self.ok = ok
        self.status = status
        self.payload = payload

    async def json(self):
        return self.payload


def identity_context(*, response=None, error=None):
    request = SimpleNamespace(
        get=AsyncMock(
            side_effect=error if error is not None else None,
            return_value=response,
        )
    )
    return SimpleNamespace(request=request)


class XiaohongshuIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_exception_is_unverified_and_cannot_promote_ready(self):
        identity = await account_calibrator.verify_xiaohongshu_identity(
            identity_context(error=RuntimeError("offline"))
        )

        self.assertFalse(identity["verified"])
        self.assertEqual(identity["note"], "identity_api_unavailable")
        self.assertEqual(
            account_calibrator.calibrated_account_status("warming", identity),
            "warming",
        )

    async def test_http_403_is_unverified_and_cannot_promote_ready(self):
        identity = await account_calibrator.verify_xiaohongshu_identity(
            identity_context(response=FakeIdentityResponse(ok=False, status=403))
        )

        self.assertFalse(identity["verified"])
        self.assertEqual(identity["note"], "identity_api_http_403")
        self.assertEqual(
            account_calibrator.calibrated_account_status("warming", identity),
            "warming",
        )

    async def test_unknown_or_userless_payload_is_unverified(self):
        payloads = (
            None,
            [],
            {},
            {"success": True, "data": []},
            {"success": True, "data": {}},
            {"success": False, "data": {"user_id": "123"}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                identity = await account_calibrator.verify_xiaohongshu_identity(
                    identity_context(
                        response=FakeIdentityResponse(payload=payload)
                    )
                )
                self.assertFalse(identity["verified"])
                self.assertEqual(
                    account_calibrator.calibrated_account_status(
                        "warming", identity
                    ),
                    "warming",
                )

    async def test_explicit_guest_is_rejected(self):
        with self.assertRaisesRegex(
            ValueError,
            "Xiaohongshu credential is not authenticated",
        ):
            await account_calibrator.verify_xiaohongshu_identity(
                identity_context(
                    response=FakeIdentityResponse(
                        payload={"success": True, "data": {"guest": True}}
                    )
                )
            )

    async def test_authenticated_user_id_is_verified(self):
        identity = await account_calibrator.verify_xiaohongshu_identity(
            identity_context(
                response=FakeIdentityResponse(
                    payload={
                        "success": True,
                        "data": {"guest": False, "user_id": " 123456 "},
                    }
                )
            )
        )

        self.assertEqual(
            identity,
            {
                "verified": True,
                "method": "xiaohongshu_me",
                "user_id": "123456",
            },
        )
        self.assertEqual(
            account_calibrator.calibrated_account_status("warming", identity),
            "ready",
        )

    async def test_authenticated_response_can_persist_explicit_public_labels(self):
        identity = await account_calibrator.verify_xiaohongshu_identity(
            identity_context(
                response=FakeIdentityResponse(
                    payload={
                        "success": True,
                        "data": {
                            "guest": False,
                            "user_id": "123456",
                            "nickname": "小红书昵称",
                            "official_title": "品牌认证",
                            "cookie": "must-not-persist",
                        },
                    }
                )
            )
        )

        self.assertEqual(
            identity,
            {
                "verified": True,
                "method": "xiaohongshu_me",
                "user_id": "123456",
                "nickname": "小红书昵称",
                "title": "品牌认证",
            },
        )
        self.assertNotIn("must-not-persist", repr(identity))


if __name__ == "__main__":
    unittest.main(verbosity=2)
