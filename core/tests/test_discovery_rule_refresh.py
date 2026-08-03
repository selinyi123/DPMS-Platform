import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import discovery
from app.services.discovery import prepare_discovery_rule_refresh


class DiscoveryRuleRefreshTests(unittest.TestCase):
    def test_changed_rule_invalidates_reviewed_plan(self):
        candidate_plan = {
            "required_actions": ["followed", "liked", "commented"],
            "review_required": False,
            "source": "bilibili_parser",
        }

        rule_update, plan_update = prepare_discovery_rule_refresh(
            "关注并转评赞",
            {"required_actions": ["followed"], "review_required": False},
            "带话题 #ASUS翻转夏日# 并关注、转评赞",
            candidate_plan,
        )

        self.assertEqual("带话题 #ASUS翻转夏日# 并关注、转评赞", rule_update)
        self.assertEqual(["followed", "liked", "commented"], plan_update["required_actions"])
        self.assertTrue(plan_update["review_required"])
        self.assertFalse(plan_update["executable"])
        self.assertEqual("discovery_rule_changed", plan_update["source"])
        self.assertFalse(candidate_plan["review_required"])
        self.assertEqual("bilibili_parser", candidate_plan["source"])

    def test_changed_rule_without_candidate_plan_still_requires_review(self):
        rule_update, plan_update = prepare_discovery_rule_refresh(
            "旧规则",
            {"review_required": False},
            "新规则",
            None,
        )

        self.assertEqual("新规则", rule_update)
        self.assertEqual(
            {
                "review_required": True,
                "executable": False,
                "source": "discovery_rule_changed",
            },
            plan_update,
        )

    def test_unchanged_rule_preserves_existing_reviewed_plan(self):
        rule_update, plan_update = prepare_discovery_rule_refresh(
            "关注并转评赞",
            json.dumps({"required_actions": ["followed"], "review_required": False}),
            "关注并转评赞",
            {"required_actions": ["liked"], "review_required": True},
        )

        self.assertIsNone(rule_update)
        self.assertIsNone(plan_update)

    def test_unchanged_rule_can_fill_missing_action_plan(self):
        incoming = {"required_actions": ["followed"], "review_required": True}

        rule_update, plan_update = prepare_discovery_rule_refresh(
            "关注并转评赞",
            "{}",
            "关注并转评赞",
            incoming,
        )

        self.assertIsNone(rule_update)
        self.assertEqual(
            {
                "required_actions": ["followed"],
                "review_required": True,
                "executable": False,
                "source": "discovery_unattested",
            },
            plan_update,
        )

    def test_empty_legacy_rule_can_be_populated_once(self):
        incoming = {"required_actions": ["followed"], "review_required": False}

        rule_update, plan_update = prepare_discovery_rule_refresh(
            "   ",
            {"required_actions": ["liked"], "review_required": False},
            "关注并转评赞",
            incoming,
        )

        self.assertEqual("关注并转评赞", rule_update)
        self.assertEqual(
            {
                "required_actions": ["followed"],
                "review_required": True,
                "executable": False,
                "source": "discovery_unattested",
            },
            plan_update,
        )

    def test_blank_candidate_does_not_clear_existing_state(self):
        self.assertEqual(
            (None, None),
            prepare_discovery_rule_refresh(
                "关注并转评赞",
                {"review_required": False},
                "   ",
                {"review_required": True},
            ),
        )


class _TransactionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeDatabase:
    def __init__(self, existing):
        self.existing = existing
        self.fetch_query = ""
        self.fetch_queries = []
        self.executions = []

    def transaction(self):
        return _TransactionContext()

    async def fetch_one(self, query, values):
        self.fetch_query = str(query)
        self.fetch_queries.append(self.fetch_query)
        return self.existing

    async def execute(self, query, values):
        self.executions.append((str(query), values))
        return 1


class _InsertFailureDatabase(_FakeDatabase):
    def __init__(self, fallback_existing=None):
        super().__init__(existing=None)
        self.fallback_existing = fallback_existing
        self.fetch_count = 0

    async def fetch_one(self, query, values):
        self.fetch_count += 1
        self.fetch_query = str(query)
        return None if self.fetch_count == 1 else self.fallback_existing

    async def execute(self, query, values):
        self.executions.append((str(query), values))
        raise RuntimeError("database insert failed")


class DiscoveryRuleRefreshPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiration_never_overwrites_a_claimed_or_locked_lottery(self):
        affected_rows = AsyncMock(return_value=3)
        fake_database = object()

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "execute_affected_rows", affected_rows),
        ):
            count = await discovery.expire_old_lotteries()

        self.assertEqual(count, 3)
        query = affected_rows.await_args.args[0]
        self.assertIs(affected_rows.await_args.kwargs["db"], fake_database)
        self.assertIn("status = 'pending'", query)
        self.assertIn("execution_lock IS NULL", query)
        self.assertNotIn("'claimed'", query)

    async def test_existing_row_is_locked_and_changed_rule_is_persisted_unreviewed(self):
        fake_database = _FakeDatabase(
            {
                "id": 42,
                "rule_text": "关注并转评赞",
                "action_plan": json.dumps({"review_required": False}),
                "status": "pending",
                "execution_lock": None,
            }
        )

        with patch.object(discovery, "database", fake_database):
            inserted = await discovery.insert_lottery_if_new(
                {"platform": "bilibili", "source_type": "up", "source_value": "123"},
                "https://t.bilibili.com/123",
                "canonical://bilibili/dynamic/123",
                90,
                {
                    "rule_text": "带话题 #ASUS翻转夏日# 并关注、转评赞",
                    "action_plan": {
                        "required_actions": ["followed", "liked", "commented", "reposted"],
                        "review_required": False,
                    },
                },
            )

        self.assertFalse(inserted)
        locked_lookup = next(
            query for query in fake_database.fetch_queries if "FROM lotteries" in query
        )
        self.assertIn("FOR UPDATE", locked_lookup)
        self.assertIn("url_hash = SHA2", locked_lookup)
        self.assertEqual(1, len(fake_database.executions))
        _, values = fake_database.executions[0]
        self.assertEqual("带话题 #ASUS翻转夏日# 并关注、转评赞", values["rule_text"])
        persisted_plan = json.loads(values["action_plan"])
        self.assertTrue(persisted_plan["review_required"])
        self.assertEqual("discovery_rule_changed", persisted_plan["source"])

    async def test_active_execution_defers_rule_and_plan_refresh(self):
        fake_database = _FakeDatabase(
            {
                "id": 43,
                "rule_text": "旧规则",
                "action_plan": json.dumps({"review_required": False}),
                "status": "running",
                "execution_lock": "task-active",
            }
        )

        with patch.object(discovery, "database", fake_database):
            inserted = await discovery.insert_lottery_if_new(
                {"platform": "bilibili", "source_type": "up", "source_value": "123"},
                "https://t.bilibili.com/123",
                "canonical://bilibili/dynamic/123",
                90,
                {
                    "rule_text": "新规则",
                    "action_plan": {"required_actions": ["liked"], "review_required": True},
                },
            )

        self.assertFalse(inserted)
        self.assertEqual(1, len(fake_database.executions))
        _, values = fake_database.executions[0]
        self.assertIsNone(values["rule_text"])
        self.assertIsNone(values["action_plan"])

    async def test_immutable_intent_defers_automatic_rule_and_plan_refresh(self):
        fake_database = _FakeDatabase(
            {
                "id": 44,
                "rule_text": "旧规则",
                "action_plan": json.dumps(
                    {
                        "required_actions": ["liked", "commented"],
                        "review_required": False,
                    }
                ),
                "status": "pending",
                "execution_lock": None,
                # This covers both a partially confirmed current intent and a
                # settled zero-effect generation. Discovery may observe a new
                # draft, but only an operator may supersede frozen authority.
                "has_execution_intent": 1,
            }
        )

        with (
            patch.object(discovery, "database", fake_database),
            patch.object(discovery, "structured_log") as log,
        ):
            inserted = await discovery.insert_lottery_if_new(
                {
                    "platform": "bilibili",
                    "source_type": "up",
                    "source_value": "123",
                },
                "https://t.bilibili.com/123",
                "canonical://bilibili/dynamic/123",
                90,
                {
                    "rule_text": "新规则",
                    "action_plan": {
                        "required_actions": ["followed"],
                        "review_required": True,
                    },
                },
            )

        self.assertFalse(inserted)
        locked_lookup = next(
            query
            for query in fake_database.fetch_queries
            if "FROM lotteries AS l" in query
        )
        self.assertIn("FROM lottery_execution_intents", locked_lookup)
        self.assertEqual(1, len(fake_database.executions))
        _, values = fake_database.executions[0]
        self.assertIsNone(values["rule_text"])
        self.assertIsNone(values["action_plan"])
        self.assertEqual(0, values["rule_changed"])
        self.assertEqual(0, values["action_plan_changed"])
        log.assert_any_call(
            "warning",
            "discovery_rule_refresh_deferred_frozen_intent",
            lottery_id=44,
            canonical_url="canonical://bilibili/dynamic/123",
        )

    async def test_insert_failure_is_not_silently_reported_as_duplicate(self):
        fake_database = _InsertFailureDatabase()

        with patch.object(discovery, "database", fake_database):
            with self.assertRaisesRegex(RuntimeError, "database insert failed"):
                await discovery.insert_lottery_if_new(
                    {"platform": "bilibili", "source_type": "up", "source_value": "123"},
                    "https://t.bilibili.com/123",
                    "canonical://bilibili/dynamic/123",
                    90,
                )

    async def test_long_tracked_source_uses_bounded_stable_locator(self):
        fake_database = _FakeDatabase(existing=None)
        source = {
            "id": 42,
            "platform": "bilibili",
            "source_type": "url_list",
            "source_value": "https://example.test/" + ("x" * 100),
        }

        with patch.object(discovery, "database", fake_database):
            inserted = await discovery.insert_lottery_if_new(
                source,
                "https://t.bilibili.com/123",
                "canonical://bilibili/dynamic/123",
                90,
            )

        self.assertTrue(inserted)
        insert_query, values = fake_database.executions[0]
        self.assertIn("INSERT INTO lotteries", insert_query)
        self.assertEqual("tracked_source:42", values["source_id"])
        self.assertLessEqual(len(values["source_id"]), 64)

    async def test_concurrent_unique_race_is_reported_as_existing(self):
        fake_database = _InsertFailureDatabase(fallback_existing={"id": 44})

        with patch.object(discovery, "database", fake_database):
            inserted = await discovery.insert_lottery_if_new(
                {"platform": "bilibili", "source_type": "up", "source_value": "123"},
                "https://t.bilibili.com/123",
                "canonical://bilibili/dynamic/123",
                90,
            )

        self.assertFalse(inserted)
        self.assertEqual(fake_database.fetch_count, 2)


if __name__ == "__main__":
    unittest.main()
