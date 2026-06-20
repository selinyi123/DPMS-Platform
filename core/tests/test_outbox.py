import base64
import json
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services.outbox import (  # noqa: E402
    LOTTERY_TASK_FIELDS,
    OUTBOX_MAX_ATTEMPTS,
    build_lottery_task_message,
    should_retry,
    terminal_status,
)


class BuildLotteryTaskMessageTests(unittest.TestCase):
    def _msg(self, **overrides):
        params = dict(
            task_id="t-1",
            account_id=7,
            lottery_id=42,
            platform="bilibili",
            raw_url="https://www.bilibili.com/x",
            canonical_url="https://www.bilibili.com/x?clean",
            task_mode="dry_run",
            dry_run=True,
            platform_selectors={"follow": ".btn"},
            action_plan={"required_actions": ["followed"]},
        )
        params.update(overrides)
        return build_lottery_task_message(**params)

    def test_field_set_is_exactly_the_contract(self):
        msg = self._msg()
        self.assertEqual(set(msg.keys()), set(LOTTERY_TASK_FIELDS))

    def test_all_values_are_strings(self):
        msg = self._msg()
        for key, value in msg.items():
            self.assertIsInstance(value, str, f"{key} must be a str for Redis xadd")

    def test_ids_are_stringified(self):
        msg = self._msg(account_id=7, lottery_id=42)
        self.assertEqual(msg["account_id"], "7")
        self.assertEqual(msg["lottery_id"], "42")

    def test_dry_run_flag_matches_mode(self):
        self.assertEqual(self._msg(task_mode="dry_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="shadow_run", dry_run=True)["dry_run"], "1")
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["dry_run"], "0")

    def test_mode_is_preserved(self):
        self.assertEqual(self._msg(task_mode="real_run", dry_run=False)["mode"], "real_run")

    def test_selectors_and_plan_are_json_encoded(self):
        msg = self._msg(platform_selectors={"a": 1}, action_plan={"b": 2})
        self.assertEqual(json.loads(msg["selector_config"]), {"a": 1})
        self.assertEqual(json.loads(msg["action_plan"]), {"b": 2})

    def test_non_dict_selectors_and_plan_default_to_empty(self):
        msg = self._msg(platform_selectors=None, action_plan="not-a-dict")
        self.assertEqual(msg["selector_config"], "{}")
        self.assertEqual(msg["action_plan"], "{}")

    def test_none_urls_become_empty_strings(self):
        msg = self._msg(raw_url=None, canonical_url=None)
        self.assertEqual(msg["raw_url"], "")
        self.assertEqual(msg["canonical_url"], "")


class RetryPredicateTests(unittest.TestCase):
    def test_should_retry_below_cap(self):
        self.assertTrue(should_retry(0))
        self.assertTrue(should_retry(OUTBOX_MAX_ATTEMPTS - 1))

    def test_should_not_retry_at_or_above_cap(self):
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS))
        self.assertFalse(should_retry(OUTBOX_MAX_ATTEMPTS + 3))

    def test_terminal_status_keeps_pending_until_cap(self):
        self.assertEqual(terminal_status(1), "pending")
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS - 1), "pending")

    def test_terminal_status_failed_at_cap(self):
        self.assertEqual(terminal_status(OUTBOX_MAX_ATTEMPTS), "failed")


class RecoveryPayloadContractTests(unittest.TestCase):
    """The recovery daemon must produce the same field set as fresh dispatch."""

    def test_recovery_action_plan_parser(self):
        from app.services.recovery_daemon import _parse_action_plan

        self.assertEqual(_parse_action_plan(None), {})
        self.assertEqual(_parse_action_plan({"x": 1}), {"x": 1})
        self.assertEqual(_parse_action_plan('{"y": 2}'), {"y": 2})
        self.assertEqual(_parse_action_plan(""), {})
        self.assertEqual(_parse_action_plan("not json"), {})
        self.assertEqual(_parse_action_plan(b'{"z": 3}'), {"z": 3})


if __name__ == "__main__":
    unittest.main()
