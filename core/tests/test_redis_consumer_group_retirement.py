from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from scripts import retire_redis_consumer_group as retirement


def retirement_documents(
    *,
    now: datetime,
    group: str = "abandoned-audit",
    break_glass_oversized_inventory: bool = False,
):
    intent_id = str(uuid.uuid4())
    intent = {
        "version": 1,
        "intent_id": intent_id,
        "stream": "lottery_tasks:bilibili",
        "group": group,
        "actor": "operator@example.invalid",
        "ticket": "OPS-2042",
        "reason": "Retire an abandoned audit group after drain verification.",
        "created_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        # Operators should normally use a full day. The hard parser minimum is
        # one hour, so ordinary low-traffic idleness cannot authorize removal.
        "inactive_for_seconds": 86_400,
        "break_glass_oversized_inventory": (
            break_glass_oversized_inventory
        ),
    }
    allowlist = {
        "version": 1,
        "allowed": [
            {
                "intent_id": intent_id,
                "stream": intent["stream"],
                "group": group,
                "actor": intent["actor"],
                "ticket": intent["ticket"],
                "reason": intent["reason"],
                "created_at": intent["created_at"],
                "expires_at": intent["expires_at"],
                "inactive_for_seconds": intent[
                    "inactive_for_seconds"
                ],
                "break_glass_oversized_inventory": (
                    break_glass_oversized_inventory
                ),
            }
        ],
    }
    return intent, allowlist


class FakeRedis:
    def __init__(self, *, groups, consumers=(), eval_result=("destroyed", "1")):
        self.groups = groups
        self.consumers = consumers
        self.eval_result = eval_result
        self.eval_calls = []
        self.consumer_calls = 0

    async def xinfo_groups(self, _stream):
        return self.groups

    async def xinfo_consumers(self, _stream, _group):
        self.consumer_calls += 1
        return self.consumers

    async def eval(self, *args):
        self.eval_calls.append(args)
        return self.eval_result


class RedisConsumerGroupRetirementTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_operation_error_does_not_leak_admin_url(self):
        secret_url = "redis://admin:super-secret@example.invalid/0"
        error = retirement._safe_operation_error(
            RuntimeError(f"connection failed: {secret_url}")
        )

        self.assertEqual(
            error,
            "retirement_operation_failed:RuntimeError",
        )
        self.assertNotIn(secret_url, error)

    def test_intent_requires_independent_exact_allowlist_pair(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        self.assertEqual(validated["stream"], intent["stream"])
        self.assertEqual(validated["group"], intent["group"])
        self.assertFalse(validated["known_group"])

        allowlist["allowed"][0]["group"] = "another-group"
        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_pair_not_exactly_approved",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

        intent, allowlist = retirement_documents(now=now)
        intent["inactive_for_seconds"] = 3_600
        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_pair_not_exactly_approved",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    def test_expired_intent_is_rejected(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        intent["created_at"] = (now - timedelta(hours=2)).isoformat()
        intent["expires_at"] = (now - timedelta(hours=1)).isoformat()

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_intent_not_active",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    def test_current_topology_group_cannot_be_retired(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(
            now=now,
            group="workers:bilibili",
        )
        # Topology authority wins before an operator-supplied approval
        # document is considered.
        allowlist["allowed"] = []

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_current_topology_group_forbidden",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    def test_last_removed_group_remains_on_governed_stream(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(
            now=now,
            group="workers:bilibili",
        )
        with patch.object(
            retirement,
            "consumer_group_specs_for_stream",
            return_value=(),
        ):
            validated = retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

        self.assertEqual(validated["stream"], "lottery_tasks:bilibili")
        self.assertEqual(validated["group"], "workers:bilibili")

    def test_low_traffic_idle_window_cannot_bypass_minimum(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        intent["inactive_for_seconds"] = 3_599

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_inactive_window_invalid",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    def test_discovery_topology_group_cannot_be_retired(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        intent["stream"] = "discovery_scan_requests:v1:bilibili"
        intent["group"] = "discovery-platform-runners:v1:bilibili"
        allowlist["allowed"] = []

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_current_topology_group_forbidden",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    async def test_preflight_rejects_pending_lag_or_active_consumer(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[
                {
                    "name": intent["group"],
                    "pending": 1,
                    "lag": 2,
                }
            ],
            consumers=[{"name": "consumer-1", "idle": 10}],
        )

        observed = await retirement.inspect_retirement_candidate(
            fake,
            validated,
        )

        self.assertFalse(observed["safe_to_retire"])
        self.assertEqual(observed["pending"], 1)
        self.assertEqual(observed["lag"], 2)
        self.assertEqual(observed["active_consumers"], 1)

    async def test_preflight_rejects_oversized_group_inventory(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[
                {
                    "name": f"orphan-{index}",
                    "pending": 0,
                    "lag": 0,
                }
                for index in range(
                    retirement.MAX_OBSERVED_CONSUMER_GROUPS + 1
                )
            ],
        )

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_group_inventory_too_large",
        ):
            await retirement.inspect_retirement_candidate(
                fake,
                validated,
            )

    async def test_break_glass_can_reduce_bounded_oversized_inventory(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(
            now=now,
            break_glass_oversized_inventory=True,
        )
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[
                {
                    "name": intent["group"],
                    "pending": 0,
                    "lag": 0,
                    "consumers": 0,
                },
                *[
                    {
                        "name": f"orphan-{index}",
                        "pending": 0,
                        "lag": 0,
                    }
                    for index in range(
                        retirement.MAX_OBSERVED_CONSUMER_GROUPS
                    )
                ],
            ],
        )

        observation = await retirement.inspect_retirement_candidate(
            fake,
            validated,
        )

        self.assertTrue(observation["safe_to_retire"])
        self.assertTrue(observation["inventory_oversized"])
        self.assertTrue(observation["break_glass_used"])

        intent["break_glass_oversized_inventory"] = False
        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_pair_not_exactly_approved",
        ):
            retirement.validate_retirement_intent(
                intent,
                allowlist,
                now=now,
            )

    async def test_preflight_skips_reported_oversized_consumer_inventory(
        self,
    ):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[
                {
                    "name": intent["group"],
                    "pending": 0,
                    "lag": 0,
                    "consumers": 257,
                }
            ],
        )

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_consumer_inventory_too_large",
        ):
            await retirement.inspect_retirement_candidate(
                fake,
                validated,
            )
        self.assertEqual(fake.consumer_calls, 0)

    async def test_atomic_retirement_uses_exact_key_group_and_threshold(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[],
            eval_result=("destroyed", "1"),
        )

        result = await retirement.retire_consumer_group_atomically(
            fake,
            validated,
        )

        self.assertEqual(result, {"status": "destroyed", "destroyed": True})
        args = fake.eval_calls[0]
        self.assertIn("XGROUP', 'DESTROY'", args[0])
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], intent["stream"])
        self.assertEqual(args[3], intent["group"])
        self.assertEqual(args[4], "86400000")
        self.assertEqual(args[6], "0")
        self.assertEqual(
            args[7],
            str(retirement.MAX_OBSERVED_CONSUMER_GROUPS),
        )
        self.assertEqual(
            args[8],
            str(retirement.MAX_BREAK_GLASS_CONSUMER_GROUPS),
        )

    async def test_atomic_retirement_refuses_changed_redis_state(self):
        now = datetime.now(timezone.utc)
        intent, allowlist = retirement_documents(now=now)
        validated = retirement.validate_retirement_intent(
            intent,
            allowlist,
            now=now,
        )
        fake = FakeRedis(
            groups=[],
            eval_result=("blocked_pending", "1"),
        )

        with self.assertRaisesRegex(
            retirement.RetirementRefused,
            "retirement_atomic_precondition_failed:blocked_pending:1",
        ):
            await retirement.retire_consumer_group_atomically(
                fake,
                validated,
            )

    async def test_retention_sweep_requires_db_terminal_and_group_confirmation(
        self,
    ):
        terminal_id = str(uuid.uuid4())
        nonterminal_id = str(uuid.uuid4())

        class SweepRedis:
            def __init__(self):
                self.eval_calls = []

            async def xrange(self, *_args, **_kwargs):
                return [
                    ("1-0", {"task_id": terminal_id}),
                    ("2-0", {"task_id": nonterminal_id}),
                ]

            async def eval(self, *args):
                self.eval_calls.append(args)
                return ("1", "1")

        class Database:
            async def fetch_one(self, _query, values):
                return {
                    "status": (
                        "succeeded"
                        if values["identifier"] == terminal_id
                        else "running"
                    )
                }

        redis = SweepRedis()
        result = await retirement.sweep_confirmed_stream_entries(
            redis,
            Database(),
            stream="lottery_tasks:bilibili",
            limit=10,
        )

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["nonterminal"], 1)
        self.assertEqual(result["confirmed"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(redis.eval_calls), 1)
        self.assertIn("XDEL", redis.eval_calls[0][0])
        self.assertEqual(result["next_cursor"], "2-0")
        self.assertTrue(result["scan_complete"])

    async def test_retention_sweep_cursor_advances_past_blocked_prefix(self):
        class SweepRedis:
            def __init__(self):
                self.range_calls = []

            async def xrange(self, stream, **kwargs):
                self.range_calls.append((stream, kwargs))
                return [
                    ("101-0", {"not": "a supported terminal envelope"}),
                    ("102-0", None),
                ]

            async def eval(self, *_args):
                raise AssertionError("blocked rows must not be deleted")

        class Database:
            async def fetch_one(self, *_args, **_kwargs):
                raise AssertionError("unsupported stream fields need no DB")

        redis = SweepRedis()
        result = await retirement.sweep_confirmed_stream_entries(
            redis,
            Database(),
            stream="lottery_tasks:bilibili",
            limit=2,
            cursor="100-0",
        )

        self.assertEqual(
            redis.range_calls[0][1]["min"],
            "(100-0",
        )
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["nonterminal"], 2)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["next_cursor"], "102-0")
        self.assertFalse(result["scan_complete"])

    async def test_login_sweep_requires_terminal_session_authority(self):
        session_id = str(uuid.uuid4())

        class Database:
            async def fetch_one(self, query, values):
                self.query = query
                self.values = values
                return {"status": "confirmed"}

        database = Database()
        authorized = await retirement._terminal_stream_entry_authorized(
            database,
            stream="login_requests",
            fields={"session_id": session_id},
        )

        self.assertTrue(authorized)
        self.assertIn("FROM login_sessions", database.query)
        self.assertEqual(database.values["identifier"], session_id)

    def test_audit_log_is_append_only_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "group-retirement.jsonl"
            retirement.append_audit_record(path, {"event": "attempt"})
            retirement.append_audit_record(path, {"event": "completed"})

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(
            [record["event"] for record in records],
            ["attempt", "completed"],
        )

    def test_audit_log_rejects_symlink_before_resolving_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target.jsonl"
            link = Path(temp_dir) / "audit.jsonl"
            target.write_text("", encoding="utf-8")
            link.symlink_to(target)
            with self.assertRaisesRegex(
                retirement.RetirementRefused,
                "retirement_audit_symlink_forbidden",
            ):
                retirement.append_audit_record(
                    link,
                    {"event": "attempt"},
                )


if __name__ == "__main__":
    unittest.main()
