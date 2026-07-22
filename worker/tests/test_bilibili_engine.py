"""Offline tests for the Bilibili direct-API engine (worker/app/bilibili).

No network and no real account: the client is driven through
``httpx.MockTransport`` so request construction (cookie/csrf injection, wbi
params, POST bodies, follow failover) is asserted directly, and the executor
runs against a fake client with stubbed sleep/rand.

Run:  python -m unittest worker.tests.test_bilibili_engine   (from worker/)
  or: python worker/tests/test_bilibili_engine.py
"""

import asyncio
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> import app.*

import httpx

from app.bilibili import wbi
from app.bilibili.client import (
    BilibiliApiActionOutcomeUnknown,
    BilibiliApiClient,
    parse_cookie,
)
from app.bilibili.config import BiliEngineConfig
from app.bilibili.errors import CodeResult, Outcome, classify
from app.bilibili.executor import BilibiliApiExecutor
from app.bilibili.parser import DynamicCard, looks_like_lottery, parse_feed
from app.bilibili.runtime import BilibiliRuntimeError, parse_detail_card

COOKIE = "DedeUserID=12345; bili_jct=abc123csrf; SESSDATA=deadbeef"


class WbiTests(unittest.TestCase):
    def test_mixin_key_canonical_vector(self):
        # Canonical public test vector for the wbi mixin-key derivation.
        self.assertEqual(
            wbi.get_mixin_key(
                "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
            ),
            "ea1db124af3c7062474693fa704f4ff8",
        )

    def test_sign_is_deterministic_and_order_independent(self):
        a = wbi.sign({"foo": "bar", "baz": 1}, "i" * 32, "s" * 32, wts=1700000000)
        b = wbi.sign({"baz": 1, "foo": "bar"}, "i" * 32, "s" * 32, wts=1700000000)
        self.assertEqual(a["w_rid"], b["w_rid"])
        self.assertEqual(a["wts"], 1700000000)
        self.assertEqual(len(a["w_rid"]), 32)

    def test_key_from_url(self):
        self.assertEqual(
            wbi.key_from_url("https://i0.hdslb.com/bfs/wbi/abc123.png"), "abc123"
        )


class CookieAndErrorTests(unittest.TestCase):
    def test_parse_cookie(self):
        jar = parse_cookie(COOKIE)
        self.assertEqual(jar["DedeUserID"], "12345")
        self.assertEqual(jar["bili_jct"], "abc123csrf")

    def test_classification(self):
        self.assertIs(classify("follow", 0).outcome, Outcome.OK)
        self.assertIs(classify("follow", 22014).outcome, Outcome.OK)      # already followed
        self.assertIs(classify("follow", 22009).outcome, Outcome.LIMIT)   # follow cap
        self.assertIs(classify("follow", 22015).outcome, Outcome.RISK)    # 账号异常
        self.assertIs(classify("like", 65006).outcome, Outcome.OK)        # already liked
        self.assertIs(classify("like", 1000001).outcome, Outcome.RETRY)   # frequent
        self.assertIs(classify("comment", 12051).outcome, Outcome.SKIP)   # duplicate
        self.assertIs(classify("comment", 12015).outcome, Outcome.CAPTCHA)
        self.assertIs(classify("comment", -101).outcome, Outcome.AUTH)
        self.assertIs(classify("like", -352).outcome, Outcome.RISK)       # generic risk
        self.assertIs(classify("repost", 999999).outcome, Outcome.FATAL)  # unknown


class ParserTests(unittest.TestCase):
    def _feed(self):
        return {
            "data": {
                "items": [
                    {
                        "id_str": "999888777666555444",  # > 2^53; must stay a string
                        "type": "DYNAMIC_TYPE_FORWARD",
                        "basic": {"comment_id_str": "111222333"},
                        "modules": {
                            "module_author": {"mid": 42, "name": "抽奖姬", "pub_ts": 1700000000},
                            "module_stat": {"like": {"status": False}},
                            "module_dynamic": {
                                "desc": {
                                    "rich_text_nodes": [
                                        {"type": "RICH_TEXT_NODE_TYPE_TEXT", "text": "转发抽奖 ", "orig_text": "转发抽奖 "},
                                        {"type": "RICH_TEXT_NODE_TYPE_AT", "text": "@friend", "orig_text": "@friend", "rid": "777"},
                                    ]
                                },
                                "additional": {"reserve": {"rid": 555, "title": "新视频预约"}},
                            },
                        },
                        "orig": {
                            "id_str": "555444333222111000",
                            "type": "DYNAMIC_TYPE_DRAW",
                            "basic": {"comment_id_str": "444555666"},
                            "modules": {
                                "module_author": {"mid": 7, "name": "原Up", "pub_ts": 1699990000},
                                "module_dynamic": {
                                    "desc": {
                                        "rich_text_nodes": [
                                            {"type": "RICH_TEXT_NODE_TYPE_LOTTERY", "text": "互动抽奖", "orig_text": "互动抽奖"}
                                        ]
                                    }
                                },
                            },
                        },
                    }
                ]
            }
        }

    def test_parse_forward_with_origin_official_lottery(self):
        cards = parse_feed(self._feed())
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c.dynamic_id, "999888777666555444")
        self.assertIsInstance(c.dynamic_id, str)
        self.assertEqual(c.type, 1)            # forward
        self.assertEqual(c.chat_type, 17)      # comment type for forward
        self.assertEqual(c.uid, 42)
        self.assertEqual(c.rid_str, "111222333")
        self.assertEqual(c.reserve_id, 555)
        self.assertEqual(len(c.ctrl), 1)       # one @ node
        self.assertEqual(c.ctrl[0]["data"], "777")
        self.assertIsNotNone(c.origin)
        self.assertTrue(c.origin.has_official_lottery)
        self.assertEqual(c.origin.uid, 7)
        self.assertTrue(looks_like_lottery(c))

    def test_detail_response_must_match_requested_dynamic(self):
        payload = self._feed()
        payload["data"]["item"] = payload["data"].pop("items")[0]
        with self.assertRaisesRegex(
            BilibiliRuntimeError,
            "bilibili_dynamic_detail_target_mismatch",
        ):
            parse_detail_card(payload, "111111111111111111")

    def test_detail_response_missing_dynamic_id_is_not_filled_from_request(self):
        payload = self._feed()
        item = payload["data"].pop("items")[0]
        item.pop("id_str")
        payload["data"]["item"] = item

        with self.assertRaisesRegex(
            BilibiliRuntimeError,
            "bilibili_dynamic_detail_unparseable",
        ):
            parse_detail_card(payload, "999888777666555444")


def _resp(payload, request):
    return httpx.Response(200, json=payload, request=request)


class ClientMockTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []  # (method, path, params, body)

    def _handler(self, default_code=0):
        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            params = dict(request.url.params)
            body = {}
            if request.content:
                body = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
            self.calls.append((request.method, path, params, body))

            if path.endswith("/x/web-interface/nav"):
                return _resp(
                    {"code": 0, "data": {"isLogin": True, "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                    }}},
                    request,
                )
            if "feed/space" in path:
                return _resp({"code": 0, "data": {"items": [], "has_more": 0}}, request)
            if path.endswith("/x/relation/modify"):
                return _resp({"code": default_code}, request)
            if "SetUserFollow" in path:
                return _resp({"code": 0}, request)
            if "reply/add" in path:
                return _resp({"code": 12015, "data": {"url": "https://captcha/img.jpg"}}, request)
            return _resp({"code": 0}, request)

        return handle

    async def test_follow_posts_relation_modify_with_csrf(self):
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler())) as c:
            res = await c.follow(42)
        self.assertIs(res.outcome, Outcome.OK)
        method, path, _, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/x/relation/modify"))
        self.assertEqual(body["fid"], "42")
        self.assertEqual(body["act"], "1")
        self.assertEqual(body["csrf"], "abc123csrf")  # injected from bili_jct

    async def test_follow_unknown_business_code_stops_without_route_failover(self):
        # An unrecognised response cannot prove that the first mutation failed;
        # sending another route could duplicate a successful follow.
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler(default_code=88888))) as c:
            with self.assertRaises(BilibiliApiActionOutcomeUnknown) as caught:
                await c.follow(42)
        self.assertEqual(caught.exception.action, "follow")
        paths = [p for _, p, _, _ in self.calls]
        self.assertTrue(any(p.endswith("/x/relation/modify") for p in paths))
        self.assertFalse(any("SetUserFollow" in p for p in paths))

    async def test_space_dynamics_is_wbi_signed_and_caches_keys(self):
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler())) as c:
            await c.get_space_dynamics(7)
            await c.get_space_dynamics(7, offset="abc")
        nav_calls = [1 for _, p, _, _ in self.calls if p.endswith("/x/web-interface/nav")]
        self.assertEqual(sum(nav_calls), 1)  # keys fetched once, then cached
        feed_params = [params for _, p, params, _ in self.calls if "feed/space" in p]
        self.assertEqual(len(feed_params), 2)
        for params in feed_params:
            self.assertIn("w_rid", params)
            self.assertIn("wts", params)

    async def test_check_login_reads_nav(self):
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler())) as c:
            self.assertTrue(await c.check_login())

    async def test_client_close_failure_does_not_replace_completed_body_outcome(self):
        class FailingCloseClient:
            async def aclose(self):
                raise RuntimeError("local pool close failed")

        client = BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler()))
        await client._client.aclose()
        client._client = FailingCloseClient()

        with patch("app.bilibili.client.structured_log") as log:
            await client.__aexit__(None, None, None)

        log.assert_called_once()
        self.assertEqual(log.call_args.args[1], "bilibili_http_client_close_failed")

    async def test_comment_captcha_surfaces_url(self):
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler())) as c:
            res = await c.comment("111222333", 17, "hi")
        self.assertIs(res.outcome, Outcome.CAPTCHA)
        self.assertIn("captcha/img.jpg", res.message)

    async def test_get_retries_transport_failure(self):
        attempts = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("temporary read failure", request=request)
            return _resp({"code": 0, "data": {"isLogin": True}}, request)

        config = BiliEngineConfig(max_http_retries=2, http_retry_wait=0)
        async with BilibiliApiClient(
            COOKIE, config=config, transport=httpx.MockTransport(handle)
        ) as client:
            self.assertTrue(await client.check_login())
        self.assertEqual(attempts, 3)

    async def test_post_transport_failure_is_unknown_and_not_retried(self):
        attempts = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("response lost", request=request)

        config = BiliEngineConfig(max_http_retries=3, http_retry_wait=0)
        async with BilibiliApiClient(
            COOKIE, config=config, transport=httpx.MockTransport(handle)
        ) as client:
            with self.assertRaises(BilibiliApiActionOutcomeUnknown) as caught:
                await client.like("dyn1")
        self.assertEqual(caught.exception.action, "like")
        self.assertEqual(attempts, 1)

    async def test_post_5xx_is_unknown_and_not_retried(self):
        attempts = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={"code": -1}, request=request)

        config = BiliEngineConfig(max_http_retries=3, http_retry_wait=0)
        async with BilibiliApiClient(
            COOKIE, config=config, transport=httpx.MockTransport(handle)
        ) as client:
            with self.assertRaises(BilibiliApiActionOutcomeUnknown):
                await client.like("dyn1")
        self.assertEqual(attempts, 1)

    async def test_cancelled_post_is_quarantined_as_unknown_outcome(self):
        async with BilibiliApiClient(COOKIE) as client:
            async def cancelled_request(*_args, **_kwargs):
                raise asyncio.CancelledError()

            client._client.request = cancelled_request
            with self.assertRaises(BilibiliApiActionOutcomeUnknown) as caught:
                await client.like("dyn1")

        self.assertEqual(caught.exception.action, "like")
        self.assertEqual(caught.exception.reason, "cancelled")

    async def test_post_business_retry_result_is_still_classified(self):
        attempts = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return _resp({"code": 1000001}, request)

        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(handle)) as client:
            result = await client.like("dyn1")
        self.assertIs(result.outcome, Outcome.RETRY)
        self.assertEqual(attempts, 1)


class _FakeClient:
    """Records action calls and returns queued CodeResults."""

    def __init__(self, results):
        self.uid = 100
        self._results = {k: list(v) for k, v in results.items()}
        self.calls = []
        self.args = {}

    async def _pop(self, name, *a):
        self.calls.append(name)
        self.args.setdefault(name, []).append(a)
        q = self._results.get(name) or []
        return q.pop(0) if q else classify(name, 0)

    async def follow(self, uid):
        return await self._pop("follow", uid)

    async def like(self, dyid):
        return await self._pop("like", dyid)

    async def repost(self, dyid, content="转发动态"):
        return await self._pop("repost", dyid, content)

    async def comment(self, oid, chat_type, message, code=""):
        return await self._pop("comment", oid, chat_type, message)


def _card(rid="rid1"):
    return DynamicCard(uid=42, type=2, rid_str=rid, chat_type=11, dynamic_id="dyn1")


EXACT_ACTION_PAYLOADS = {
    "followed": {},
    "liked": {},
    "commented": {"text": "#ASUS翻转夏日# @ASUS华硕官方UP 精确评论"},
    "reposted": {"text": "#ASUS翻转夏日# 精确转发"},
}


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def _exec(self, fake, before_action=None, after_action=None):
        # no real waiting; deterministic jitter
        return BilibiliApiExecutor(
            fake,
            BiliEngineConfig(),
            sleep=self._nosleep,
            rand=lambda: 0.5,
            before_action=before_action,
            after_action=after_action,
        )

    async def _nosleep(self, _seconds):
        return None

    async def test_all_actions_succeed(self):
        fake = _FakeClient({})  # default OK for everything
        res = await self._exec(fake).participate(
            _card(), ["follow", "like", "repost", "comment"], EXACT_ACTION_PAYLOADS
        )
        self.assertTrue(res.success)
        self.assertEqual(set(res.performed), {"follow", "like", "repost", "comment"})
        self.assertEqual(
            fake.args["comment"][0][2],
            "#ASUS翻转夏日# @ASUS华硕官方UP 精确评论",
        )
        self.assertEqual(fake.args["repost"][0][1], "#ASUS翻转夏日# 精确转发")

    async def test_abort_on_risk_stops_remaining_phases(self):
        fake = _FakeClient({"follow": [classify("follow", 22015)]})  # RISK
        res = await self._exec(fake).participate(
            _card(), ["follow", "like", "comment"], EXACT_ACTION_PAYLOADS
        )
        self.assertTrue(res.aborted)
        self.assertFalse(res.success)
        self.assertEqual(fake.calls, ["follow"])  # like/comment never attempted

    async def test_every_terminal_non_ok_result_is_settled_then_stops_next_phase(self):
        terminal_outcomes = (
            Outcome.LIMIT,
            Outcome.SKIP,
            Outcome.CAPTCHA,
            Outcome.RISK,
            Outcome.AUTH,
            Outcome.FATAL,
        )
        for outcome in terminal_outcomes:
            with self.subTest(outcome=outcome.value):
                fake = _FakeClient(
                    {"follow": [CodeResult(123, outcome, f"{outcome.value} result")]}
                )
                settled = []

                async def after_action(action, result):
                    settled.append((action, result.outcome))

                res = await self._exec(fake, after_action=after_action).participate(
                    _card(), ["follow", "like"]
                )

                self.assertTrue(res.aborted)
                self.assertFalse(res.success)
                self.assertEqual(fake.calls, ["follow"])
                self.assertEqual(settled, [("follow", outcome)])

    async def test_retry_exhaustion_is_settled_then_stops_next_phase(self):
        retries = [
            CodeResult(1000001, Outcome.RETRY, "retry result")
            for _ in range(BiliEngineConfig().action_max_attempts)
        ]
        fake = _FakeClient({"follow": retries})
        settled = []

        async def after_action(action, result):
            settled.append((action, result.outcome))

        res = await self._exec(fake, after_action=after_action).participate(
            _card(), ["follow", "like"]
        )

        self.assertTrue(res.aborted)
        self.assertFalse(res.success)
        self.assertEqual(
            fake.calls,
            ["follow"] * BiliEngineConfig().action_max_attempts,
        )
        self.assertEqual(settled, [("follow", Outcome.RETRY)])

    async def test_action_cap_is_reported_as_an_abort_before_next_mutation(self):
        fake = _FakeClient({})
        executor = BilibiliApiExecutor(
            fake,
            BiliEngineConfig(max_actions_per_target=1),
            sleep=self._nosleep,
            rand=lambda: 0.5,
        )

        result = await executor.participate(_card(), ["follow", "like"])

        self.assertTrue(result.aborted)
        self.assertFalse(result.success)
        self.assertEqual("达到单目标动作上限", result.abort_reason)
        self.assertEqual(["follow"], fake.calls)

    async def test_action_cap_counts_retry_attempts_not_only_successes(self):
        fake = _FakeClient(
            {
                "follow": [
                    CodeResult(1000001, Outcome.RETRY, "retry result"),
                    CodeResult(1000001, Outcome.RETRY, "retry result"),
                    CodeResult(1000001, Outcome.RETRY, "retry result"),
                ]
            }
        )
        executor = BilibiliApiExecutor(
            fake,
            BiliEngineConfig(max_actions_per_target=2, action_max_attempts=4),
            sleep=self._nosleep,
            rand=lambda: 0.5,
        )

        result = await executor.participate(_card(), ["follow", "like"])

        self.assertTrue(result.aborted)
        self.assertFalse(result.success)
        self.assertEqual("达到单目标动作上限", result.abort_reason)
        self.assertEqual(["follow", "follow"], fake.calls)

    async def test_retry_then_success(self):
        fake = _FakeClient({"like": [classify("like", 1000001), classify("like", 0)]})  # RETRY then OK
        res = await self._exec(fake).participate(_card(), ["like"])
        self.assertTrue(res.success)
        self.assertEqual(fake.calls.count("like"), 2)

    async def test_before_action_is_checked_before_every_retry_attempt(self):
        fake = _FakeClient({"like": [classify("like", 1000001), classify("like", 0)]})
        checked = []

        async def before_action(action):
            checked.append(action)

        res = await self._exec(fake, before_action).participate(_card(), ["like"])
        self.assertTrue(res.success)
        self.assertEqual(checked, ["like", "like"])

    async def test_gate_closing_after_first_action_blocks_second_action(self):
        fake = _FakeClient({})
        checked = []

        async def before_action(action):
            checked.append(action)
            if len(checked) > 1:
                raise RuntimeError("real_run_gate_blocked:real_run_disabled")

        with self.assertRaisesRegex(RuntimeError, "real_run_disabled"):
            await self._exec(fake, before_action).participate(_card(), ["follow", "like"])
        self.assertEqual(checked, ["follow", "like"])
        self.assertEqual(fake.calls, ["follow"])

    async def test_confirmed_action_is_settled_before_next_gate_check(self):
        fake = _FakeClient({})
        settled = []

        async def before_action(action):
            if action == "like":
                raise RuntimeError("real_run_gate_blocked:real_run_disabled")

        async def after_action(action, result):
            settled.append((action, result.outcome))

        with self.assertRaisesRegex(RuntimeError, "real_run_disabled"):
            await self._exec(fake, before_action, after_action).participate(
                _card(), ["follow", "like"]
            )
        self.assertEqual(settled, [("follow", Outcome.OK)])
        self.assertEqual(fake.calls, ["follow"])

    async def test_comment_skipped_without_rid(self):
        fake = _FakeClient({})
        settled = []

        async def after_action(action, result):
            settled.append((action, result.outcome))

        res = await self._exec(fake, after_action=after_action).participate(
            _card(rid=""), ["comment", "repost"], EXACT_ACTION_PAYLOADS
        )
        self.assertIs(res.actions["comment"].outcome, Outcome.SKIP)
        self.assertTrue(res.aborted)
        self.assertEqual(settled, [("comment", Outcome.SKIP)])
        self.assertNotIn("comment", fake.calls)
        self.assertNotIn("repost", fake.calls)

    async def test_forward_acts_on_origin(self):
        # A 转发 (type==1) of a lottery: follow/repost/comment must hit the origin.
        fake = _FakeClient({})
        card = DynamicCard(
            uid=42, type=1, dynamic_id="fwd", rid_str="r0", chat_type=17,
            origin=DynamicCard(uid=7, type=2, dynamic_id="orig", rid_str="r1", chat_type=11),
        )
        res = await self._exec(fake).participate(
            card, ["follow", "repost"], EXACT_ACTION_PAYLOADS
        )
        self.assertTrue(res.success)
        self.assertEqual(res.dynamic_id, "orig")
        self.assertEqual(fake.args["follow"][0][0], 7)       # followed origin uid, not 42
        self.assertEqual(fake.args["repost"][0][0], "orig")  # reposted origin dynamic

    async def test_comment_or_repost_without_reviewed_text_is_blocked_before_actions(self):
        for action in ("comment", "repost"):
            with self.subTest(action=action):
                fake = _FakeClient({})
                with self.assertRaisesRegex(
                    RuntimeError, f"bilibili_{action}_exact_text_required"
                ):
                    await self._exec(fake).participate(_card(), [action], {})
                self.assertEqual(fake.calls, [])

    async def test_media_payload_is_not_silently_downgraded_to_text_only(self):
        fake = _FakeClient({})
        payloads = {
            **EXACT_ACTION_PAYLOADS,
            "commented": {
                "text": "精确评论",
                "media_refs": ["evidence:photo-1"],
            },
        }
        with self.assertRaisesRegex(RuntimeError, "media_unsupported"):
            await self._exec(fake).participate(_card(), ["comment"], payloads)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
