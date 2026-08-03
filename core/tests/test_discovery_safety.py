import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import discovery


class _TransactionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _AtomicSourceDatabase:
    def __init__(self):
        self.rows = set()
        self.row_counts = {}
        self.insert_queries = []
        self._lock = asyncio.Lock()

    def transaction(self):
        return _TransactionContext()

    async def execute(self, query, values=None):
        sql = str(query)
        if "INSERT INTO tracked_sources" not in sql:
            raise AssertionError(f"unexpected execute: {sql}")
        self.insert_queries.append((sql, dict(values or {})))
        uid = str(values["uid"])
        # Yield before the atomic section so concurrent callers really race in
        # this offline model. The UPSERT itself remains indivisible.
        await asyncio.sleep(0)
        async with self._lock:
            inserted = uid not in self.rows
            self.rows.add(uid)
        self.row_counts[asyncio.current_task()] = 1 if inserted else 0
        return len(self.rows)

    async def fetch_one(self, query, values=None):
        sql = str(query)
        if "ROW_COUNT()" in sql:
            return {"affected": self.row_counts.get(asyncio.current_task(), 0)}
        if "SELECT id FROM tracked_sources" in sql:
            uid = str((values or {}).get("uid"))
            return {"id": 1} if uid in self.rows else None
        raise AssertionError(f"unexpected fetch_one: {sql}")


class _DuplicateFallbackDatabase(_AtomicSourceDatabase):
    async def execute(self, query, values=None):
        uid = str(values["uid"])
        if uid == "1001":
            self.rows.add(uid)
            self.insert_queries.append((str(query), dict(values)))
            raise RuntimeError("duplicate key from racing writer")
        return await super().execute(query, values)


class _BoundedScanDatabase:
    def __init__(self, sources):
        self.sources = sources
        self.fetch_all_query = ""
        self.fetch_all_values = None
        self.fetch_all_calls = []
        self.executions = []

    async def fetch_one(self, query, values=None):
        if "COUNT(*) AS total" not in str(query):
            raise AssertionError(f"unexpected fetch_one: {query}")
        return {"total": len(self.sources)}

    async def fetch_all(self, query, values=None):
        self.fetch_all_query = str(query)
        self.fetch_all_values = dict(values or {})
        self.fetch_all_calls.append(
            (self.fetch_all_query, self.fetch_all_values)
        )
        # Deliberately ignore LIMIT to prove the Python-side hard bound too.
        platform = str(self.fetch_all_values.get("platform") or "")
        allowed_types = {
            str(value)
            for key, value in self.fetch_all_values.items()
            if key.startswith("source_type_")
        }
        rows = [
            source
            for source in self.sources
            if str(source.get("platform") or "") == platform
            and (
                not allowed_types
                or str(source.get("source_type") or "") in allowed_types
            )
            and discovery.should_scan(source)
        ]
        return sorted(
            rows,
            key=lambda source: (
                source.get("last_scan_at") is not None,
                source.get("last_scan_at") or datetime.min,
                source["id"],
            ),
        )

    async def execute(self, query, values=None):
        query_text = str(query)
        bound = dict(values or {})
        self.executions.append((query_text, bound))
        advanced_ids = {
            int(value)
            for key, value in bound.items()
            if key.startswith("invalid_id_")
        }
        if "last_scan_at = NOW()" in query_text:
            if "id" in bound:
                advanced_ids.add(int(bound["id"]))
            now = datetime.now()
            for source in self.sources:
                if int(source["id"]) in advanced_ids:
                    source["last_scan_at"] = now
        return 1


def _source(source_id, *, platform="bilibili", value=None):
    default_values = {
        "bilibili": str(9000 + source_id),
        "weibo": f"https://m.weibo.cn/status/AbCdEf{source_id}",
        "xiaohongshu": (
            "https://www.xiaohongshu.com/explore/"
            f"{source_id:024x}"
        ),
        "douyin": f"https://www.douyin.com/video/{source_id:019d}",
    }
    return {
        "id": source_id,
        "platform": platform,
        "source_type": "up" if platform == "bilibili" else "url_list",
        "source_value": str(
            value if value is not None else default_values[platform]
        ),
        "last_scan_at": None,
        "scan_interval_minutes": 30,
    }


def _candidate(*uids):
    refs = " ".join(f"【UP{uid}】、【{uid}】" for uid in uids)
    return {"rule_text": f"抽奖 {refs}", "raw_url": "https://t.bilibili.com/123"}


def _keyword_source(source_id, value):
    source = _source(source_id)
    source["source_type"] = "keyword"
    source["source_value"] = value
    return source


def _keyword_candidate(candidate_id):
    return SimpleNamespace(
        url=f"https://t.bilibili.com/{candidate_id}",
        title=f"lottery-{candidate_id}",
        rule_text="抽奖：关注并转发",
        published_at=None,
        action_plan={"is_lottery": True},
    )


class _RecordWithoutGet:
    """Minimal stand-in for a databases.Record row."""

    def __init__(self, values):
        self._values = dict(values)

    def keys(self):
        return self._values.keys()

    def __getitem__(self, key):
        return self._values[key]


class DiscoverySingleflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        discovery._discovery_run_task = None

    async def asyncTearDown(self):
        task = discovery._discovery_run_task
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        discovery._discovery_run_task = None

    async def test_concurrent_callers_share_one_scan_and_receive_copies(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_run_once(**_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {
                "sources": 3,
                "scanned": 2,
                "by_platform": {"weibo": {"scanned": 1}},
            }

        with patch.object(discovery, "_run_discovery_once", side_effect=fake_run_once):
            first = asyncio.create_task(discovery.run_discovery())
            await started.wait()
            second = asyncio.create_task(discovery.run_discovery())
            await asyncio.sleep(0)
            self.assertEqual(1, calls)
            release.set()
            first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(first_result, second_result)
        self.assertIsNot(first_result, second_result)
        self.assertIsNot(
            first_result["by_platform"]["weibo"],
            second_result["by_platform"]["weibo"],
        )
        first_result["by_platform"]["weibo"]["scanned"] = 99
        self.assertEqual(1, second_result["by_platform"]["weibo"]["scanned"])
        self.assertEqual(1, calls)

    async def test_cancelled_waiter_does_not_cancel_shared_scan(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_run_once(**_kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"sources": 1}

        with patch.object(discovery, "_run_discovery_once", side_effect=fake_run_once):
            cancelled_waiter = asyncio.create_task(discovery.run_discovery())
            await started.wait()
            cancelled_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_waiter

            surviving_waiter = asyncio.create_task(discovery.run_discovery())
            await asyncio.sleep(0)
            self.assertEqual(1, calls)
            release.set()
            self.assertEqual({"sources": 1}, await surviving_waiter)


class DiscoveryBoundedScanTests(unittest.IsolatedAsyncioTestCase):
    def test_should_scan_treats_null_interval_as_safe_default(self):
        self.assertTrue(
            discovery.should_scan(
                {
                    "last_scan_at": datetime.now() - timedelta(minutes=31),
                    "scan_interval_minutes": None,
                }
            )
        )

    async def test_scan_is_oldest_first_and_hard_bounded(self):
        source_rows = [_source(index, platform="weibo") for index in range(1, 151)]
        fake_database = _BoundedScanDatabase(source_rows)
        fetch_candidates = AsyncMock(return_value=[])

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
        ):
            stats = await discovery._run_discovery_once()

        self.assertEqual(150, stats["sources"])
        self.assertEqual(discovery.DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT, stats["scanned"])
        self.assertEqual(discovery.DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT, fetch_candidates.await_count)
        self.assertEqual(4, len(fake_database.fetch_all_calls))
        self.assertEqual(
            discovery.DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT,
            fake_database.fetch_all_values["source_limit"],
        )
        self.assertEqual("xiaohongshu", fake_database.fetch_all_values["platform"])
        self.assertIn("TIMESTAMPADD", fake_database.fetch_all_query)
        self.assertIn("platform = :platform", fake_database.fetch_all_query)
        self.assertIn("source_type = :source_type_0", fake_database.fetch_all_query)
        self.assertIn(
            "ORDER BY (last_scan_at IS NULL) DESC, last_scan_at ASC, id ASC",
            fake_database.fetch_all_query,
        )
        self.assertIn("LIMIT :source_limit", fake_database.fetch_all_query)

    async def test_legacy_unsupported_source_is_dormant_and_does_not_take_slot(self):
        unsupported = _source(1, platform="weibo")
        unsupported["source_type"] = "up"
        supported = _source(2, platform="weibo")
        fake_database = _BoundedScanDatabase([unsupported, supported])
        fetch_candidates = AsyncMock(return_value=[])

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
        ):
            stats = await discovery._run_discovery_once()

        self.assertEqual(1, stats["by_platform"]["weibo"]["scheduled"])
        self.assertEqual(1, stats["by_platform"]["weibo"]["scanned"])
        self.assertEqual(1, fetch_candidates.await_count)
        self.assertEqual(2, fetch_candidates.await_args.args[0]["id"])
        weibo_query = next(
            (query, values)
            for query, values in fake_database.fetch_all_calls
            if values["platform"] == "weibo"
        )
        self.assertIn("source_type = :source_type_0", weibo_query[0])
        self.assertEqual("url_list", weibo_query[1]["source_type_0"])

    async def test_invalid_oldest_window_advances_so_valid_row_is_not_starved(self):
        oversized_keyword = "x" * (
            discovery.BILIBILI_KEYWORD_QUERY_MAX_CHARS + 1
        )
        invalid_rows = [
            _keyword_source(source_id, oversized_keyword)
            for source_id in range(
                1, discovery.DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT + 1
            )
        ]
        valid = _source(discovery.DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT + 1)
        fake_database = _BoundedScanDatabase([*invalid_rows, valid])
        fetch_candidates = AsyncMock(return_value=[])

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
            patch.object(discovery, "structured_log"),
        ):
            first = await discovery._run_discovery_once()
            second = await discovery._run_discovery_once()

        self.assertEqual(0, first["scanned"])
        self.assertEqual(
            discovery.DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT,
            first["by_platform"]["bilibili"]["failed"],
        )
        self.assertEqual(1, second["scanned"])
        self.assertEqual(1, fetch_candidates.await_count)
        self.assertEqual(valid["id"], fetch_candidates.await_args.args[0]["id"])
        invalid_updates = [
            values
            for query, values in fake_database.executions
            if "invalid_id_0" in values
            and "UPDATE tracked_sources" in query
        ]
        self.assertEqual(1, len(invalid_updates))
        self.assertEqual(
            discovery.DISCOVERY_PLATFORM_SOURCE_QUERY_LIMIT,
            len(
                [
                    key
                    for key in invalid_updates[0]
                    if key.startswith("invalid_id_")
                ]
            ),
        )

    def test_round_robin_prevents_one_platform_from_starving_the_others(self):
        batches = {
            "bilibili": [_source(index) for index in range(1, 101)],
            "weibo": [_source(201, platform="weibo")],
            "douyin": [_source(301, platform="douyin")],
            "xiaohongshu": [_source(401, platform="xiaohongshu")],
        }

        selected = discovery.fair_round_robin_sources(
            batches,
            limit=discovery.DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT,
        )

        self.assertEqual(discovery.DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT, len(selected))
        self.assertEqual(
            {"bilibili", "weibo", "douyin", "xiaohongshu"},
            {source["platform"] for source in selected},
        )
        self.assertEqual(
            [1, 2, 3],
            [
                source["id"]
                for source in selected
                if source["platform"] == "bilibili"
            ][:3],
        )

    async def test_slow_failing_bilibili_does_not_block_other_platform(self):
        source_rows = [
            _source(1),
            _source(2, platform="weibo"),
        ]
        fake_database = _BoundedScanDatabase(source_rows)
        bilibili_started = asyncio.Event()
        release_bilibili = asyncio.Event()
        weibo_completed = asyncio.Event()

        async def fetch(source, **_kwargs):
            if source["platform"] == "bilibili":
                bilibili_started.set()
                await release_bilibili.wait()
                raise RuntimeError("bilibili unavailable")
            weibo_completed.set()
            return []

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", side_effect=fetch),
        ):
            task = asyncio.create_task(discovery._run_discovery_once())
            await bilibili_started.wait()
            await asyncio.wait_for(weibo_completed.wait(), timeout=1)
            self.assertFalse(task.done())
            release_bilibili.set()
            stats = await task

        self.assertEqual(1, stats["by_platform"]["weibo"]["scanned"])
        self.assertEqual(1, stats["by_platform"]["bilibili"]["failed"])

    async def test_session_factory_failure_is_isolated_to_its_platform(self):
        source_rows = [
            _source(1),
            _source(2, platform="weibo"),
        ]
        fake_database = _BoundedScanDatabase(source_rows)

        def fail_session_factory(_module):
            raise RuntimeError("broken platform session")

        original_get_platform_module = discovery.get_platform_module
        bilibili_module = replace(
            original_get_platform_module("bilibili"),
            discovery_session_factory=fail_session_factory,
        )

        def get_platform_module(platform):
            if platform == "bilibili":
                return bilibili_module
            return original_get_platform_module(platform)

        fetch_candidates = AsyncMock(return_value=[])

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(
                discovery,
                "get_platform_module",
                side_effect=get_platform_module,
            ),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
        ):
            stats = await discovery._run_discovery_once()

        self.assertEqual(1, stats["by_platform"]["bilibili"]["failed"])
        self.assertEqual(1, stats["by_platform"]["weibo"]["scanned"])
        self.assertEqual(1, fetch_candidates.await_count)

    async def test_malformed_candidate_does_not_abort_later_source(self):
        fake_database = _BoundedScanDatabase([])
        fetch_candidates = AsyncMock(side_effect=[[{}], []])
        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
        ):
            stats = await discovery._scan_platform_sources(
                "bilibili",
                [_source(1), _source(2)],
                discovery.get_platform_module("bilibili"),
            )

        self.assertEqual(2, stats["scanned"])
        self.assertEqual(1, stats["failed"])
        self.assertEqual(2, fetch_candidates.await_count)
        self.assertEqual(2, len(fake_database.executions))

    async def test_expansion_failure_does_not_abort_remaining_sources(self):
        source_rows = [_source(1), _source(2)]
        fake_database = _BoundedScanDatabase(source_rows)
        fetch_candidates = AsyncMock(return_value=[])
        expand_sources = AsyncMock(side_effect=[RuntimeError("upsert failed"), 0])

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "fetch_candidates_for_source", fetch_candidates),
            patch.object(discovery, "expand_bilibili_collection_sources", expand_sources),
        ):
            stats = await discovery._run_discovery_once()

        self.assertEqual(2, stats["scanned"])
        self.assertEqual(1, stats["failed"])
        self.assertEqual(2, fetch_candidates.await_count)
        self.assertEqual(2, expand_sources.await_count)
        self.assertEqual(2, len(fake_database.executions))


class BilibiliKeywordSearchSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_module_receives_plain_source_snapshot(self):
        source = _RecordWithoutGet(_keyword_source(7, "抽奖"))
        fetch = AsyncMock(return_value=[])
        session = SimpleNamespace(
            platform_module=SimpleNamespace(platform_id="bilibili"),
            fetch_candidates=fetch,
        )

        with patch.object(
            discovery,
            "get_platform_module",
            return_value=SimpleNamespace(),
        ):
            result = await discovery.fetch_candidates_for_source(
                source,
                discovery_session=session,
            )

        self.assertEqual(result, [])
        forwarded = fetch.await_args.args[0]
        self.assertIs(type(forwarded), dict)
        self.assertEqual(forwarded["source_value"], "抽奖")

    def test_split_keywords_deduplicates_rejects_oversized_and_limits_queries(self):
        oversized = "长" * (discovery.BILIBILI_KEYWORD_QUERY_MAX_CHARS + 1)
        source_value = ",".join(
            ["Giveaway", "giveaway", oversized]
            + [f"keyword-{index}" for index in range(20)]
        )

        keywords = discovery.split_keywords(source_value)

        self.assertEqual(discovery.BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT, len(keywords))
        self.assertEqual("Giveaway", keywords[0])
        self.assertNotIn("giveaway", keywords)
        self.assertNotIn(oversized, keywords)
        self.assertEqual("keyword-6", keywords[-1])

    async def test_oversized_only_source_does_not_load_cookie_or_search(self):
        oversized = "长" * (discovery.BILIBILI_KEYWORD_QUERY_MAX_CHARS + 1)
        load_cookie = AsyncMock(return_value="SESSDATA=secret")
        search = AsyncMock(return_value=[])

        with (
            patch.object(discovery, "try_load_bilibili_discovery_cookie_header", load_cookie),
            patch.object(discovery, "fetch_bilibili_keyword_search", search),
        ):
            candidates = await discovery.fetch_keyword_dynamics(oversized)

        self.assertEqual([], candidates)
        load_cookie.assert_not_awaited()
        search.assert_not_awaited()

    async def test_per_source_search_calls_and_provider_arguments_are_bounded(self):
        search = AsyncMock(return_value=[])
        source_value = ",".join(f"keyword-{index}" for index in range(50))

        with (
            patch.object(
                discovery,
                "try_load_bilibili_discovery_cookie_header",
                AsyncMock(return_value=None),
            ),
            patch.object(discovery, "fetch_bilibili_keyword_search", search),
        ):
            candidates = await discovery.fetch_keyword_dynamics(source_value)

        self.assertEqual([], candidates)
        self.assertEqual(discovery.BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT, search.await_count)
        for call in search.await_args_list:
            self.assertEqual(
                discovery.BILIBILI_KEYWORD_SEARCH_PAGES_PER_CALL,
                call.kwargs["pages"],
            )
            self.assertEqual(
                discovery.BILIBILI_KEYWORD_SEARCH_RESULT_LIMIT,
                call.kwargs["limit"],
            )

    async def test_failed_search_attempt_consumes_run_budget(self):
        budget = discovery.KeywordSearchCallBudget(1)
        search = AsyncMock(side_effect=RuntimeError("provider rejected request"))
        load_cookie = AsyncMock(return_value=None)

        with (
            patch.object(discovery, "try_load_bilibili_discovery_cookie_header", load_cookie),
            patch.object(discovery, "fetch_bilibili_keyword_search", search),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider rejected request"):
                await discovery.fetch_keyword_dynamics("one,two", search_budget=budget)
            candidates = await discovery.fetch_keyword_dynamics("three", search_budget=budget)

        self.assertEqual([], candidates)
        self.assertEqual(0, budget.remaining)
        self.assertEqual(1, search.await_count)
        self.assertEqual(1, load_cookie.await_count)

    async def test_candidates_and_items_inspected_per_search_are_bounded(self):
        calls = 0

        async def oversized_provider_result(keyword, **kwargs):
            nonlocal calls
            calls += 1
            base = calls * 1000
            # Deliberately violate the provider helper's declared result limit
            # to prove the caller keeps its own defensive bound.
            return [_keyword_candidate(base + index) for index in range(100)]

        with (
            patch.object(
                discovery,
                "try_load_bilibili_discovery_cookie_header",
                AsyncMock(return_value=None),
            ),
            patch.object(
                discovery,
                "fetch_bilibili_keyword_search",
                side_effect=oversized_provider_result,
            ),
        ):
            candidates = await discovery.fetch_keyword_dynamics("one,two,three,four")

        self.assertEqual(discovery.BILIBILI_KEYWORD_SOURCE_CANDIDATE_LIMIT, len(candidates))
        self.assertEqual(3, calls)
        self.assertEqual(len(candidates), len({item["raw_url"] for item in candidates}))

    async def test_search_call_budget_is_global_across_sources(self):
        # Persisted keyword source validation permits at most the same eight
        # queries that one provider call path can consume.
        source_value = ",".join(
            f"keyword-{index}"
            for index in range(discovery.BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT)
        )
        keyword_sources = [
            _keyword_source(source_id, source_value)
            for source_id in range(1, discovery.DISCOVERY_ACTIVE_SOURCE_SCAN_LIMIT + 1)
        ]
        # A non-keyword source after the rows that exhaust the search budget
        # must still run; only deferred keyword rows stay due for the next run.
        non_keyword_source = _source(999, platform="weibo")
        fully_funded_sources = (
            discovery.BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET
            // discovery.BILIBILI_KEYWORD_SOURCE_QUERY_LIMIT
        )
        source_rows = (
            keyword_sources[:fully_funded_sources]
            + [non_keyword_source]
            + keyword_sources[fully_funded_sources:-1]
        )
        fake_database = _BoundedScanDatabase(source_rows)
        search = AsyncMock(return_value=[])
        load_cookie = AsyncMock(return_value=None)

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "expire_old_lotteries", AsyncMock(return_value=0)),
            patch.object(discovery, "try_load_bilibili_discovery_cookie_header", load_cookie),
            patch.object(discovery, "fetch_bilibili_keyword_search", search),
            patch.object(
                discovery,
                "expand_bilibili_collection_sources",
                AsyncMock(return_value=0),
            ),
        ):
            stats = await discovery._run_discovery_once()

        self.assertEqual(fully_funded_sources + 1, stats["scanned"])
        self.assertEqual(discovery.BILIBILI_KEYWORD_SEARCH_CALL_RUN_BUDGET, search.await_count)
        self.assertEqual(fully_funded_sources, load_cookie.await_count)
        self.assertEqual(fully_funded_sources + 1, len(fake_database.executions))
        self.assertEqual(999, fake_database.executions[-1][1]["id"])


class BilibiliExpansionSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_is_global_across_sources_and_new_rows_are_inactive(self):
        fake_database = _AtomicSourceDatabase()
        budget = discovery.ExpansionBudget(3)

        with patch.object(discovery, "database", fake_database):
            first = await discovery.expand_bilibili_collection_sources(
                _source(1),
                [_candidate("1001", "1002")],
                budget=budget,
            )
            second = await discovery.expand_bilibili_collection_sources(
                _source(2),
                [_candidate("2001", "2002")],
                budget=budget,
            )

        self.assertEqual((2, 1), (first, second))
        self.assertEqual(0, budget.remaining)
        self.assertEqual({"1001", "1002", "2001"}, fake_database.rows)
        self.assertEqual(3, len(fake_database.insert_queries))
        for sql, _ in fake_database.insert_queries:
            self.assertIn("VALUES ('bilibili', 'up', :uid, :scan_interval_minutes, 0)", sql)
            self.assertIn("ON DUPLICATE KEY UPDATE", sql)

    async def test_atomic_upsert_is_idempotent_under_concurrency(self):
        fake_database = _AtomicSourceDatabase()

        with patch.object(discovery, "database", fake_database):
            results = await asyncio.gather(
                discovery.upsert_discovered_bilibili_source("1234", 30),
                discovery.upsert_discovered_bilibili_source("1234", 30),
            )

        self.assertEqual([False, True], sorted(results))
        self.assertEqual({"1234"}, fake_database.rows)
        self.assertEqual(2, len(fake_database.insert_queries))

    async def test_proven_duplicate_fallback_does_not_stop_remaining_refs(self):
        fake_database = _DuplicateFallbackDatabase()

        with patch.object(discovery, "database", fake_database):
            inserted = await discovery.expand_bilibili_collection_sources(
                _source(1),
                [_candidate("1001", "1002")],
            )

        self.assertEqual(1, inserted)
        self.assertEqual({"1001", "1002"}, fake_database.rows)
        self.assertEqual(2, len(fake_database.insert_queries))


if __name__ == "__main__":
    unittest.main()
