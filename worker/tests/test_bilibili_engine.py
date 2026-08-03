"""Offline tests for the Bilibili direct-API engine (worker/app/bilibili).

No network and no real account: the client is driven through
``httpx.MockTransport`` so request construction (cookie/csrf injection, wbi
params, POST bodies, follow failover) is asserted directly, and the executor
runs against a fake client with stubbed sleep/rand.

Run:  python -m unittest worker.tests.test_bilibili_engine   (from worker/)
  or: python worker/tests/test_bilibili_engine.py
"""

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # worker/ -> import app.*

import httpx

from app.bilibili import wbi
from app.bilibili.client import (
    BilibiliApiActionOutcomeUnknown,
    BilibiliApiClient,
    parse_cookie,
)
from app.bilibili.config import BiliEngineConfig
from app.bilibili.errors import Outcome, classify
from app.bilibili.executor import BilibiliApiExecutor
from app.bilibili.parser import DynamicCard, looks_like_lottery, parse_feed

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

    async def test_follow_failover_to_second_route_on_unknown_code(self):
        # relation/modify returns an unknown code (-> FATAL) so the client should
        # fail over to feed/SetUserFollow.
        async with BilibiliApiClient(COOKIE, transport=httpx.MockTransport(self._handler(default_code=88888))) as c:
            res = await c.follow(42)
        self.assertIs(res.outcome, Outcome.OK)
        paths = [p for _, p, _, _ in self.calls]
        self.assertTrue(any(p.endswith("/x/relation/modify") for p in paths))
        self.assertTrue(any("SetUserFollow" in p for p in paths))

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
        return await self._pop("repost", dyid)

    async def comment(self, oid, chat_type, message, code=""):
        return await self._pop("comment", oid, chat_type, message)


def _card(rid="rid1"):
    return DynamicCard(uid=42, type=2, rid_str=rid, chat_type=11, dynamic_id="dyn1")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def _exec(self, fake, before_action=None):
        # no real waiting; deterministic jitter
        return BilibiliApiExecutor(
            fake,
            BiliEngineConfig(),
            sleep=self._nosleep,
            rand=lambda: 0.5,
            before_action=before_action,
        )

    async def _nosleep(self, _seconds):
        return None

    async def test_all_actions_succeed(self):
        fake = _FakeClient({})  # default OK for everything
        res = await self._exec(fake).participate(_card(), ["follow", "like", "repost", "comment"])
        self.assertTrue(res.success)
        self.assertEqual(set(res.performed), {"follow", "like", "repost", "comment"})

    async def test_abort_on_risk_stops_remaining_phases(self):
        fake = _FakeClient({"follow": [classify("follow", 22015)]})  # RISK
        res = await self._exec(fake).participate(_card(), ["follow", "like", "comment"])
        self.assertTrue(res.aborted)
        self.assertFalse(res.success)
        self.assertEqual(fake.calls, ["follow"])  # like/comment never attempted

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

    async def test_comment_skipped_without_rid(self):
        fake = _FakeClient({})
        res = await self._exec(fake).participate(_card(rid=""), ["comment"])
        self.assertIs(res.actions["comment"].outcome, Outcome.SKIP)
        self.assertNotIn("comment", fake.calls)

    async def test_forward_acts_on_origin(self):
        # A 转发 (type==1) of a lottery: follow/repost/comment must hit the origin.
        fake = _FakeClient({})
        card = DynamicCard(
            uid=42, type=1, dynamic_id="fwd", rid_str="r0", chat_type=17,
            origin=DynamicCard(uid=7, type=2, dynamic_id="orig", rid_str="r1", chat_type=11),
        )
        res = await self._exec(fake).participate(card, ["follow", "repost"])
        self.assertTrue(res.success)
        self.assertEqual(res.dynamic_id, "orig")
        self.assertEqual(fake.args["follow"][0][0], 7)       # followed origin uid, not 42
        self.assertEqual(fake.args["repost"][0][0], "orig")  # reposted origin dynamic


if __name__ == "__main__":
    unittest.main(verbosity=2)
