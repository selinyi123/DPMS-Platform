import asyncio
import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.action_plan import (  # noqa: E402
    ACTION_ORDER,
    BILIBILI_API_EXECUTION_PATH,
    BILIBILI_API_PREFLIGHT_CONTRACT_VERSION,
    compute_action_plan_hash,
    compute_bilibili_api_config_hash,
    compute_rule_hash,
    compute_target_hash,
    semantic_requirement_status,
)
from app.services import real_run_readiness  # noqa: E402
from app.services.bilibili_preflight_evidence import (  # noqa: E402
    API_PREFLIGHT_KIND,
    hash_preflight_observation,
)
from app.services.lottery_rules import parse_lottery_rule as parse_rule_fixture  # noqa: E402


REQUIRED_ACTIONS = ["followed", "liked", "commented", "reposted"]
SCREENSHOT_PATH = "/profiles/shadow-runs/shadow-task.png"
LOTTERY_ID = 11
ACCOUNT_ID = 7
RULE_SNAPSHOT_ID = 101
EXECUTION_REVISION = 7
DYNAMIC_ID = "1220306071196794898"
CANONICAL_URL = f"https://www.bilibili.com/opus/{DYNAMIC_ID}"
FOLLOW_TARGET = "@ASUS华硕官方UP"
DEFAULT_RULE_TEXT = (
    "#新品发布# 发布新视频，感谢字幕组翻译。"
    "抽奖：关注@ASUS华硕官方UP并转评赞本条动态；"
    "一等奖送键盘，二等奖送鼠标。"
)


def complete_observation(screenshot_path=SCREENSHOT_PATH):
    return {
        "account_id": 7,
        "lottery_id": 11,
        "platform": "bilibili",
        "qualified": True,
        "side_effects": False,
        "required_phases": REQUIRED_ACTIONS,
        "visible_phases": {
            "followed": "follow-selector",
            "liked": "like-selector",
            "commented": {"input": "comment-input", "submit": "comment-submit"},
            "reposted": "repost-selector",
        },
        "screenshot_path": str(screenshot_path),
    }


def empty_content_requirements(*, follow_target=FOLLOW_TARGET):
    return {
        "follow_targets": [follow_target] if follow_target else [],
        "commented": {"topic_tags": [], "mentions": []},
        "reposted": {"topic_tags": [], "mentions": []},
    }


def action_payloads_for(required_actions, content_requirements, *, overrides=None):
    actions = list(required_actions)
    payloads = {}
    if "followed" in actions:
        payloads["followed"] = {
            "target_handle": content_requirements["follow_targets"][0],
        }
    if "liked" in actions:
        payloads["liked"] = {}
    for action, fallback_text in (
        ("commented", "参与抽奖"),
        ("reposted", "转发参与"),
    ):
        if action not in actions:
            continue
        requirement = content_requirements[action]
        tokens = [*requirement["topic_tags"], *requirement["mentions"]]
        payloads[action] = {
            "text": " ".join([*tokens, fallback_text]),
            "topic_tags": list(requirement["topic_tags"]),
            "mentions": list(requirement["mentions"]),
        }
    for action, payload in (overrides or {}).items():
        payloads[action] = copy.deepcopy(payload)
    return payloads


def complete_action_plan(
    rule_text=DEFAULT_RULE_TEXT,
    *,
    required_actions=None,
    content_requirements=None,
    payload_overrides=None,
    unsupported_actions=None,
    represented_requirements=None,
    unresolved_requirements=None,
    capability_blockers=None,
    executable=True,
):
    parsed_rule = parse_rule_fixture(rule_text, "bilibili") if rule_text else {}
    actions = list(
        required_actions
        if required_actions is not None
        else parsed_rule.get("required_actions") or REQUIRED_ACTIONS
    )
    requirements = copy.deepcopy(
        content_requirements
        if content_requirements is not None
        else parsed_rule.get("content_requirements")
        or empty_content_requirements()
    )
    unsupported = list(
        unsupported_actions
        if unsupported_actions is not None
        else parsed_rule.get("unsupported_actions") or []
    )
    payloads = action_payloads_for(
        actions,
        requirements,
        overrides=payload_overrides,
    )
    represented, unresolved, capability = semantic_requirement_status(
        unsupported,
        payloads,
        requirements,
    )
    plan = {
        "version": 2,
        "platform": "bilibili",
        "is_lottery": True,
        "required_actions": actions,
        "action_payloads": payloads,
        "content_requirements": requirements,
        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
        "rule_snapshot_id": RULE_SNAPSHOT_ID,
        "rule_hash": compute_rule_hash(rule_text) if rule_text else "0" * 64,
        "review_required": False,
        "executable": executable,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": unsupported,
        "represented_requirements": list(
            represented if represented_requirements is None else represented_requirements
        ),
        "unresolved_requirements": list(
            unresolved if unresolved_requirements is None else unresolved_requirements
        ),
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": list(
            capability if capability_blockers is None else capability_blockers
        ),
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def complete_api_observation(
    *,
    required_actions=REQUIRED_ACTIONS,
    execution_revision=EXECUTION_REVISION,
    follow_target=FOLLOW_TARGET,
):
    actions = list(required_actions)
    return {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": BILIBILI_API_EXECUTION_PATH,
        "preflight_contract_version": BILIBILI_API_PREFLIGHT_CONTRACT_VERSION,
        "execution_revision": execution_revision,
        "config_hash": compute_bilibili_api_config_hash(execution_revision),
        "side_effects": False,
        "account_authenticated": True,
        "api_preflight_complete": True,
        "requested_dynamic_id": DYNAMIC_ID,
        "observed_dynamic_id": DYNAMIC_ID,
        "target_type": 2,
        "target_uid": 987654321,
        "author_handle": follow_target,
        "follow_target_handle": follow_target if "followed" in actions else "",
        "target_identity": {
            "verified": True,
            "dynamic_id": DYNAMIC_ID,
            "author_uid": 987654321,
            "author_handle": follow_target,
        },
        "comment_rid_str": DYNAMIC_ID if "commented" in actions else "",
        "comment_type": 17 if "commented" in actions else None,
        "required_actions": actions,
        "api_actions": [
            {
                "followed": "follow",
                "liked": "like",
                "commented": "comment",
                "reposted": "repost",
            }[action]
            for action in actions
        ],
        "capability_checks": {action: True for action in actions},
    }


class FakeReadinessDatabase:
    def __init__(
        self,
        action_plan,
        shadow_observation,
        *,
        evidence=True,
        evidence_hash=None,
        account_ready=True,
        snapshot_ready=True,
    ):
        self.action_plan = action_plan
        self.shadow_observation = shadow_observation
        self.evidence = evidence
        self.account_ready = account_ready
        self.snapshot_ready = snapshot_ready
        actions = action_plan.get("required_actions")
        if not isinstance(actions, list) or not actions or any(
            action not in ACTION_ORDER for action in actions
        ):
            actions = REQUIRED_ACTIONS
        requirements = action_plan.get("content_requirements") or empty_content_requirements()
        follow_targets = requirements.get("follow_targets") or [FOLLOW_TARGET]
        self.probe_observation = complete_api_observation(
            required_actions=actions,
            follow_target=follow_targets[0],
        )
        self.probe_hash = hash_preflight_observation(self.probe_observation)
        canonical_shadow = (
            hash_preflight_observation(shadow_observation)
            if isinstance(shadow_observation, dict)
            else self.probe_hash
        )
        self.shadow_hash = canonical_shadow
        self.evidence_shadow_hash = evidence_hash or canonical_shadow
        self.db_now = datetime(2026, 7, 14, 12, 0, 0)

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "FROM lottery_rule_snapshots" in query:
            if not self.snapshot_ready:
                return None
            self._assert_scope(values)
            self._assert_equal(values.get("snapshot_id"), RULE_SNAPSHOT_ID)
            self._assert_equal(values.get("rule_hash"), self.action_plan["rule_hash"])
            return {
                "id": RULE_SNAPSHOT_ID,
                "platform": "bilibili",
                "rule_hash": self.action_plan["rule_hash"],
                "is_complete": 1,
                "attested_by": "operator-1",
                "attested_at": self.db_now - timedelta(hours=1),
            }
        if "FROM accounts" in query:
            self._assert_equal(values.get("account_id"), ACCOUNT_ID)
            if not self.account_ready:
                return None
            return {
                "id": ACCOUNT_ID,
                "platform": "bilibili",
                "status": "ready",
                "execution_revision": EXECUTION_REVISION,
                "credential_size": 64,
            }
        if "FROM execution_evidence_bindings" in query:
            self._assert_scope(values)
            self._assert_equal(values.get("account_id"), ACCOUNT_ID)
            self._assert_equal(values.get("rule_snapshot_id"), RULE_SNAPSHOT_ID)
            self._assert_equal(values.get("execution_path_id"), BILIBILI_API_EXECUTION_PATH)
            self._assert_equal(values.get("target_hash"), compute_target_hash(CANONICAL_URL))
            self._assert_equal(values.get("rule_hash"), self.action_plan["rule_hash"])
            self._assert_equal(values.get("action_plan_hash"), self.action_plan["plan_hash"])
            self._assert_equal(
                values.get("config_hash"),
                compute_bilibili_api_config_hash(EXECUTION_REVISION),
            )
            if not self.evidence:
                return None
            return {
                "id": "execution-evidence-1",
                "probe_id": "probe-1",
                "shadow_task_id": "shadow-task",
                "verified_at": self.db_now - timedelta(minutes=5),
                "expires_at": self.db_now + timedelta(hours=23),
                "evidence_probe_observation_kind": API_PREFLIGHT_KIND,
                "evidence_probe_observation_hash": self.probe_hash,
                "evidence_shadow_observation_kind": API_PREFLIGHT_KIND,
                "evidence_shadow_observation_hash": self.evidence_shadow_hash,
                "probe_observation": self.probe_observation,
                "probe_observation_kind": API_PREFLIGHT_KIND,
                "probe_observation_hash": self.probe_hash,
                "probe_finished_at": self.db_now - timedelta(minutes=10),
                "shadow_observation": self.shadow_observation,
                "shadow_observation_kind": API_PREFLIGHT_KIND,
                "shadow_observation_hash": self.shadow_hash,
                "shadow_finished_at": self.db_now - timedelta(minutes=8),
                "probe_lease_released_at": self.db_now - timedelta(minutes=9),
                "shadow_lease_released_at": self.db_now - timedelta(minutes=7),
            }
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": self.db_now}
        if "FROM account_active_risk_states active_risk" in query:
            self._assert_equal((values or {}).get("account_id"), ACCOUNT_ID)
            return None
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_all(self, query, values=None):
        if "FROM risk_events" in query:
            self._assert_equal((values or {}).get("account_id"), ACCOUNT_ID)
            return []
        raise AssertionError(f"Unexpected query: {query}")

    @staticmethod
    def _assert_equal(actual, expected):
        if actual != expected:
            raise AssertionError(f"scope mismatch: expected {expected!r}, got {actual!r}")

    def _assert_scope(self, values):
        self._assert_equal(values.get("lottery_id"), LOTTERY_ID)


class RiskDisplacementDatabase:
    """Emulate the one-row rolling active-risk state."""

    def __init__(self, now):
        self.now = now
        self.risk_queries = []
        self.rows = [
            {
                "id": 100 + index,
                "account_id": 4,
                "event_type": "action_window",
                "detail": json.dumps({"reason": "action_window"}),
                "created_at": now - timedelta(hours=5, minutes=index),
            }
            for index in range(50)
        ]
        self.rows.append(
            {
                "id": 1,
                "account_id": 4,
                "event_type": "login_required",
                "detail": json.dumps({"reason": "login_required"}),
                "created_at": now - timedelta(hours=20),
            }
        )

    async def fetch_all(self, query, values=None):
        if "FROM accounts" in query:
            return [{"id": 4, "platform": "bilibili"}]
        if "FROM account_active_risk_states active_risk" in query:
            self.risk_queries.append(query)
            return [self.rows[-1]]
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, values=None):
        if "SELECT UTC_TIMESTAMP() AS db_now" in query:
            return {"db_now": self.now}
        if "FROM account_active_risk_states active_risk" in query:
            self.risk_queries.append(query)
            return self.rows[-1]
        raise AssertionError(f"Unexpected query: {query}")


class AccountRiskDisplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_account_risk_cannot_be_displaced_by_fifty_expired_events(self):
        now = datetime(2026, 7, 14, 12, 0, 0)
        fake = RiskDisplacementDatabase(now)
        original_database = real_run_readiness.database
        real_run_readiness.database = fake
        try:
            risk = await real_run_readiness.recent_account_risk(4, now=now)
        finally:
            real_run_readiness.database = original_database

        self.assertTrue(risk["has_recent_risk"])
        self.assertEqual(risk["latest_event"]["id"], 1)
        self.assertNotIn("LIMIT 50", fake.risk_queries[0])

    async def test_single_account_risk_lock_is_one_materialized_row(self):
        now = datetime(2026, 7, 14, 12, 0, 0)
        database = mock.Mock()
        database.fetch_one = mock.AsyncMock(return_value=None)

        with mock.patch.object(real_run_readiness, "database", database):
            risk = await real_run_readiness.recent_account_risk(
                4,
                now=now,
                for_update=True,
            )

        self.assertFalse(risk["has_recent_risk"])
        query, values = database.fetch_one.await_args.args
        self.assertIn("FROM account_active_risk_states", query)
        self.assertIn("LIMIT 1", query)
        self.assertTrue(query.rstrip().endswith("FOR UPDATE"))
        self.assertEqual(values["account_id"], 4)

    async def test_batched_risk_cannot_be_displaced_by_fifty_expired_events(self):
        now = datetime(2026, 7, 14, 12, 0, 0)
        fake = RiskDisplacementDatabase(now)
        original_database = real_run_readiness.database
        real_run_readiness.database = fake
        try:
            summaries = await real_run_readiness.real_run_account_risk_summaries(
                ["bilibili"]
            )
        finally:
            real_run_readiness.database = original_database

        summary = summaries["bilibili"]
        self.assertEqual(summary["ready_accounts"], 1)
        self.assertEqual(summary["runnable_accounts"], 0)
        self.assertEqual(summary["latest_recent_risk"]["latest_event"]["id"], 1)
        self.assertNotIn("risk_rank <= 50", fake.risk_queries[0])

    async def test_risk_summary_account_overflow_is_bounded_and_fail_closed(
        self,
    ):
        now = datetime(2026, 7, 14, 12, 0, 0)
        account_rows = [
            {"id": index + 1, "platform": "bilibili"}
            for index in range(
                real_run_readiness
                .REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
                + 1
            )
        ]
        database = mock.Mock()
        database.fetch_one = mock.AsyncMock(return_value={"db_now": now})
        database.fetch_all = mock.AsyncMock(return_value=account_rows)

        with mock.patch.object(real_run_readiness, "database", database):
            summary = (
                await real_run_readiness.real_run_account_risk_summaries(
                    ["bilibili"]
                )
            )["bilibili"]

        self.assertEqual(summary["runnable_accounts"], 0)
        self.assertTrue(summary["query_budget_exhausted"])
        self.assertEqual(
            summary["latest_recent_risk"]["latest_event"]["event_type"],
            "risk_summary_account_budget_exhausted",
        )
        self.assertEqual(database.fetch_all.await_count, 1)
        query, values = database.fetch_all.await_args.args
        self.assertIn("LIMIT :risk_summary_account_limit", query)
        self.assertEqual(
            values["risk_summary_account_limit"],
            real_run_readiness
            .REAL_RUN_RISK_SUMMARY_ACCOUNT_LIMIT_PER_PLATFORM
            + 1,
        )

    async def test_risk_summary_reads_one_materialized_row_per_account(
        self,
    ):
        now = datetime(2026, 7, 14, 12, 0, 0)
        risk_row = {
            "id": 1,
            "account_id": 4,
            "event_type": "login_required",
            "detail": json.dumps({"reason": "login_required"}),
            "created_at": now - timedelta(hours=20),
        }

        async def fetch_all(query, _values=None):
            if "FROM accounts" in query:
                return [{"id": 4, "platform": "bilibili"}]
            if "FROM account_active_risk_states active_risk" in query:
                return [risk_row]
            raise AssertionError(f"unexpected query: {query}")

        database = mock.Mock()
        database.fetch_one = mock.AsyncMock(return_value={"db_now": now})
        database.fetch_all = mock.AsyncMock(side_effect=fetch_all)

        with mock.patch.object(real_run_readiness, "database", database):
            summary = (
                await real_run_readiness.real_run_account_risk_summaries(
                    ["bilibili"]
                )
            )["bilibili"]

        self.assertEqual(summary["ready_accounts"], 1)
        self.assertEqual(summary["runnable_accounts"], 0)
        self.assertEqual(
            summary["latest_recent_risk"]["latest_event"]["event_type"],
            "login_required",
        )
        risk_call = database.fetch_all.await_args_list[1]
        query = risk_call.args[0]
        self.assertIn("FROM account_active_risk_states", query)
        self.assertNotIn("JSON_EXTRACT", query)
        self.assertNotIn("ROW_NUMBER() OVER", query)

    async def test_risk_summary_timeout_is_observable_and_fail_closed(self):
        now = datetime(2026, 7, 14, 12, 0, 0)

        async def slow_fetch_all(_query, _values=None):
            await asyncio.sleep(0.05)
            return []

        database = mock.Mock()
        database.fetch_one = mock.AsyncMock(return_value={"db_now": now})
        database.fetch_all = mock.AsyncMock(side_effect=slow_fetch_all)
        with mock.patch.object(
            real_run_readiness,
            "database",
            database,
        ), mock.patch.object(
            real_run_readiness,
            "REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS",
            0.001,
        ), mock.patch.object(
            real_run_readiness,
            "structured_log",
        ) as structured_log:
            summary = (
                await real_run_readiness.real_run_account_risk_summaries(
                    ["bilibili"]
                )
            )["bilibili"]

        self.assertEqual(summary["runnable_accounts"], 0)
        self.assertTrue(summary["query_budget_exhausted"])
        self.assertEqual(
            summary["latest_recent_risk"]["latest_event"]["event_type"],
            "risk_summary_query_timeout",
        )
        self.assertTrue(
            any(
                call.args[1] == "real_run_risk_summary_query_timeout"
                for call in structured_log.call_args_list
            )
        )

    async def test_risk_summary_timeout_isolated_from_sibling_platform(self):
        now = datetime(2026, 7, 14, 12, 0, 0)

        async def fetch_all(query, values=None):
            if "FROM accounts" in query:
                platform = str(
                    (values or {}).get("risk_summary_platform") or ""
                )
                if platform == "bilibili":
                    await asyncio.sleep(0.05)
                    return []
                return [{"id": 5, "platform": "weibo"}]
            if "FROM account_active_risk_states active_risk" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

        database = mock.Mock()
        database.fetch_one = mock.AsyncMock(return_value={"db_now": now})
        database.fetch_all = mock.AsyncMock(side_effect=fetch_all)
        with mock.patch.object(
            real_run_readiness,
            "database",
            database,
        ), mock.patch.object(
            real_run_readiness,
            "REAL_RUN_RISK_SUMMARY_TIMEOUT_SECONDS",
            0.01,
        ):
            summaries = (
                await real_run_readiness.real_run_account_risk_summaries(
                    ["bilibili", "weibo"]
                )
            )

        self.assertEqual(summaries["bilibili"]["runnable_accounts"], 0)
        self.assertEqual(
            summaries["bilibili"]["latest_recent_risk"]["latest_event"][
                "event_type"
            ],
            "risk_summary_query_timeout",
        )
        self.assertEqual(summaries["weibo"]["runnable_accounts"], 1)


class QualifiedShadowObservationTests(unittest.TestCase):
    def test_complete_observation_is_qualified(self):
        self.assertTrue(
            real_run_readiness.qualified_shadow_observation(complete_observation(), REQUIRED_ACTIONS)
        )

    def test_all_null_observation_is_not_qualified(self):
        payload = complete_observation()
        payload["visible_phases"] = {phase: None for phase in REQUIRED_ACTIONS}
        self.assertFalse(real_run_readiness.qualified_shadow_observation(payload, REQUIRED_ACTIONS))

    def test_comment_requires_input_and_submit(self):
        payload = complete_observation()
        payload["visible_phases"]["commented"]["submit"] = None
        self.assertFalse(real_run_readiness.qualified_shadow_observation(payload, REQUIRED_ACTIONS))

    def test_observation_must_match_current_required_actions(self):
        payload = complete_observation()
        self.assertFalse(
            real_run_readiness.qualified_shadow_observation(
                payload,
                ["followed", "liked", "commented"],
            )
        )


class PhaseConfiguredTests(unittest.TestCase):
    def test_structured_click_phase_requires_success_readback(self):
        self.assertFalse(
            real_run_readiness.phase_configured(
                "bilibili",
                {"liked": {"click": ["button.like"]}},
                "liked",
            )
        )
        self.assertTrue(
            real_run_readiness.phase_configured(
                "bilibili",
                {"liked": {"click": ["button.like"], "done": ["button.liked"]}},
                "liked",
            )
        )

    def test_structured_comment_phase_requires_success_readback(self):
        config = {"commented": {"input": ["textarea"], "submit": ["button.submit"]}}
        self.assertFalse(real_run_readiness.phase_configured("bilibili", config, "commented"))
        config["commented"]["done"] = ["article.own-comment"]
        self.assertTrue(real_run_readiness.phase_configured("bilibili", config, "commented"))

    def test_weibo_selectors_are_observation_only(self):
        self.assertTrue(
            real_run_readiness.phase_configured(
                "weibo",
                {"liked": {"click": ["button.like"]}},
                "liked",
            )
        )
        self.assertTrue(
            real_run_readiness.phase_configured(
                "weibo",
                {
                    "commented": {
                        "input": ["textarea"],
                        "submit": ["button.submit"],
                    }
                },
                "commented",
            )
        )


class ShadowScreenshotIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.allowed_root = self.base / "shadow-runs"
        self.allowed_root.mkdir()
        self.screenshot = self.allowed_root / "shadow-task.png"
        self.screenshot.write_bytes(b"shadow screenshot evidence")
        self.digest = hashlib.sha256(self.screenshot.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp_dir.cleanup()

    def matches(self, path, digest=None, *, integrity_cache=None, hash_budget=None):
        return real_run_readiness.shadow_screenshot_integrity_matches(
            path,
            self.digest if digest is None else digest,
            allowed_root=self.allowed_root,
            integrity_cache=integrity_cache,
            hash_budget=hash_budget,
        )

    def test_matching_file_inside_allowed_root_is_valid(self):
        self.assertTrue(self.matches(self.screenshot))

    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "Linux fd accounting")
    def test_repeated_secure_reads_do_not_leak_file_descriptors(self):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(100):
            self.assertTrue(self.matches(self.screenshot))
        after = len(os.listdir("/proc/self/fd"))
        self.assertEqual(after, before)

    def test_runtime_without_secure_open_primitives_fails_closed(self):
        resolved_root = self.allowed_root.resolve(strict=True)
        resolved_candidate = self.screenshot.resolve(strict=True)
        with mock.patch.object(real_run_readiness.os, "name", "nt"):
            with self.assertRaisesRegex(OSError, "secure_evidence_open_unsupported"):
                real_run_readiness._open_evidence_file_beneath_root(resolved_root, resolved_candidate)

    def test_hash_mismatch_is_invalid(self):
        self.assertFalse(self.matches(self.screenshot, "0" * 64))

    def test_missing_file_is_invalid(self):
        self.assertFalse(self.matches(self.allowed_root / "missing.png"))

    def test_path_outside_allowed_root_is_invalid(self):
        outside = self.base / "outside.png"
        outside.write_bytes(self.screenshot.read_bytes())

        self.assertFalse(self.matches(self.allowed_root / ".." / outside.name))

    def test_relative_path_is_invalid(self):
        self.assertFalse(self.matches(Path("shadow-task.png")))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "POSIX FIFO safety")
    def test_fifo_evidence_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.allowed_root / "shadow-task.fifo"
        os.mkfifo(fifo)
        self.assertFalse(self.matches(fifo))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX openat safety")
    def test_root_component_symlink_swap_is_rejected(self):
        outside = self.base / "outside-root"
        outside.mkdir()
        displaced = self.base / "shadow-runs-displaced"
        original_open = os.open
        swapped = False

        def swap_before_root_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and path == self.allowed_root.name and kwargs.get("dir_fd") is not None:
                self.allowed_root.rename(displaced)
                self.allowed_root.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(real_run_readiness.os, "open", swap_before_root_open):
                self.assertFalse(self.matches(self.screenshot))
            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if self.allowed_root.is_symlink():
                self.allowed_root.unlink()
            if displaced.exists():
                displaced.rename(self.allowed_root)

    def test_oversized_screenshot_is_rejected_before_hashing(self):
        original_limit = real_run_readiness.MAX_SHADOW_SCREENSHOT_BYTES
        real_run_readiness.MAX_SHADOW_SCREENSHOT_BYTES = 1
        try:
            self.assertFalse(self.matches(self.screenshot))
        finally:
            real_run_readiness.MAX_SHADOW_SCREENSHOT_BYTES = original_limit

    def test_request_hash_budget_fails_closed_before_excess_io(self):
        budget = {"remaining": self.screenshot.stat().st_size - 1}
        self.assertFalse(self.matches(self.screenshot, hash_budget=budget))
        self.assertEqual(budget["exhausted"], 1)
        self.assertEqual(budget["required_bytes"], self.screenshot.stat().st_size)

        exact_budget = {"remaining": self.screenshot.stat().st_size}
        self.assertTrue(self.matches(self.screenshot, hash_budget=exact_budget))
        self.assertEqual(exact_budget["remaining"], 0)
        self.assertNotIn("exhausted", exact_budget)

    def test_request_cache_reuses_hash_only_while_file_identity_is_unchanged(self):
        original_sha256 = real_run_readiness.hashlib.sha256
        calls = 0

        def counting_sha256(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_sha256(*args, **kwargs)

        cache = {}
        real_run_readiness.hashlib.sha256 = counting_sha256
        try:
            self.assertTrue(self.matches(self.screenshot, integrity_cache=cache))
            self.assertTrue(self.matches(self.screenshot, integrity_cache=cache))
            self.assertEqual(calls, 1)

            self.screenshot.write_bytes(b"tampered shadow screenshot evidence")
            self.assertFalse(self.matches(self.screenshot, integrity_cache=cache))
            self.assertEqual(calls, 2)
        finally:
            real_run_readiness.hashlib.sha256 = original_sha256


class RealRunShadowEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.shadow_root = Path(self.temp_dir.name) / "shadow-runs"
        self.shadow_root.mkdir()
        self.screenshot_path = self.shadow_root / "shadow-task.png"
        self.screenshot_path.write_bytes(b"shadow screenshot evidence")
        self.screenshot_hash = hashlib.sha256(self.screenshot_path.read_bytes()).hexdigest()
        self.original_shadow_root = real_run_readiness.SHADOW_SCREENSHOT_ROOT
        real_run_readiness.SHADOW_SCREENSHOT_ROOT = self.shadow_root

    def tearDown(self):
        real_run_readiness.SHADOW_SCREENSHOT_ROOT = self.original_shadow_root
        self.temp_dir.cleanup()

    def observation(self):
        return complete_api_observation()

    def evaluate(
        self,
        observation,
        *,
        evidence=True,
        rule_text=DEFAULT_RULE_TEXT,
        action_plan=None,
        evidence_hash=None,
        account_id=ACCOUNT_ID,
        account_ready=True,
        snapshot_ready=True,
    ):
        saved_plan = (
            complete_action_plan(rule_text)
            if action_plan is None
            else copy.deepcopy(action_plan)
        )
        original_database = real_run_readiness.database
        real_run_readiness.database = FakeReadinessDatabase(
            saved_plan,
            observation,
            evidence=evidence,
            evidence_hash=evidence_hash,
            account_ready=account_ready,
            snapshot_ready=snapshot_ready,
        )
        lottery = {
            "id": LOTTERY_ID,
            "platform": "bilibili",
            "raw_url": CANONICAL_URL,
            "canonical_url": CANONICAL_URL,
            "rule_text": rule_text,
            "rule_hash": saved_plan.get("rule_hash"),
            "authoritative_rule_snapshot_id": saved_plan.get("rule_snapshot_id"),
            "action_plan_hash": saved_plan.get("plan_hash"),
            "action_plan": json.dumps(saved_plan, ensure_ascii=False),
        }
        try:
            return asyncio.run(
                real_run_readiness.validate_real_run_evidence(
                    lottery,
                    account_id=account_id,
                )
            )
        finally:
            real_run_readiness.database = original_database

    def test_complete_account_scoped_probe_and_shadow_evidence_is_ready(self):
        result = self.evaluate(self.observation())

        self.assertTrue(result["allowed"])
        self.assertTrue(result["probe_ready"])
        self.assertTrue(result["shadow_ready"])
        self.assertTrue(result["action_plan_ready"])
        self.assertTrue(result["rule_snapshot_ready"])
        self.assertTrue(result["execution_evidence_bound"])
        self.assertEqual(EXECUTION_REVISION, result["execution_revision"])
        self.assertEqual([], result["blockers"])

    def test_implicit_author_follow_target_is_bound_before_semantic_validation(self):
        rule_text = (
            "抽奖要求：关注 + 评论 + 转发，同时评论请记得 @TargetUser，"
            "否则视为无效参与。"
        )
        parsed_rule = parse_rule_fixture(rule_text, "bilibili")
        source_requirements = copy.deepcopy(parsed_rule["content_requirements"])
        bound_requirements = copy.deepcopy(source_requirements)
        bound_requirements["follow_targets"] = ["@TargetUser"]
        saved_plan = complete_action_plan(
            rule_text,
            required_actions=parsed_rule["required_actions"],
            content_requirements=bound_requirements,
            unsupported_actions=parsed_rule["unsupported_actions"],
        )
        saved_plan["source_content_requirements"] = source_requirements
        saved_plan["plan_hash"] = compute_action_plan_hash(saved_plan)
        observation = complete_api_observation(
            required_actions=parsed_rule["required_actions"],
            follow_target="@TargetUser",
        )

        result = self.evaluate(
            observation,
            rule_text=rule_text,
            action_plan=saved_plan,
        )

        self.assertTrue(result["action_plan_ready"])
        self.assertNotIn("lottery_rule_requirements_unresolved", result["blockers"])
        self.assertNotIn("action_plan_requirement_binding_mismatch", result["blockers"])

    def test_batched_exact_evidence_matches_non_batched_authority(self):
        saved_plan = complete_action_plan()
        observation = self.observation()
        probe_observation = complete_api_observation()
        probe_hash = hash_preflight_observation(probe_observation)
        shadow_hash = hash_preflight_observation(observation)
        db_now = datetime(2026, 7, 14, 12, 0, 0)
        evidence = {
            "id": "execution-evidence-1",
            "lottery_id": LOTTERY_ID,
            "account_id": ACCOUNT_ID,
            "rule_snapshot_id": RULE_SNAPSHOT_ID,
            "execution_path_id": BILIBILI_API_EXECUTION_PATH,
            "target_hash": compute_target_hash(CANONICAL_URL),
            "rule_hash": saved_plan["rule_hash"],
            "action_plan_hash": saved_plan["plan_hash"],
            "config_hash": compute_bilibili_api_config_hash(
                EXECUTION_REVISION
            ),
            "probe_id": "probe-1",
            "shadow_task_id": "shadow-task",
            "verified_at": db_now - timedelta(minutes=5),
            "expires_at": db_now + timedelta(hours=23),
            "evidence_probe_observation_kind": API_PREFLIGHT_KIND,
            "evidence_probe_observation_hash": probe_hash,
            "evidence_shadow_observation_kind": API_PREFLIGHT_KIND,
            "evidence_shadow_observation_hash": shadow_hash,
            "probe_observation": probe_observation,
            "probe_observation_kind": API_PREFLIGHT_KIND,
            "probe_observation_hash": probe_hash,
            "probe_finished_at": db_now - timedelta(minutes=10),
            "shadow_observation": observation,
            "shadow_observation_kind": API_PREFLIGHT_KIND,
            "shadow_observation_hash": shadow_hash,
            "shadow_finished_at": db_now - timedelta(minutes=8),
        }
        batch = real_run_readiness.RealRunEvidenceBatch(
            account_id=None,
            account_ids=frozenset({ACCOUNT_ID}),
            account_scoped_readiness=True,
            accounts={
                ACCOUNT_ID: {
                    "id": ACCOUNT_ID,
                    "platform": "bilibili",
                    "status": "ready",
                    "execution_revision": EXECUTION_REVISION,
                    "credential_size": 64,
                }
            },
            account_risks={
                ACCOUNT_ID: real_run_readiness.account_risk_payload(None)
            },
            rule_snapshots={
                (LOTTERY_ID, RULE_SNAPSHOT_ID): {
                    "id": RULE_SNAPSHOT_ID,
                    "lottery_id": LOTTERY_ID,
                    "platform": "bilibili",
                    "rule_hash": saved_plan["rule_hash"],
                    "rule_text": DEFAULT_RULE_TEXT,
                    "is_complete": 1,
                    "attested_by": "operator-1",
                    "attested_at": db_now - timedelta(hours=1),
                }
            },
            bilibili_execution_evidence={
                (LOTTERY_ID, ACCOUNT_ID): [evidence]
            },
        )
        lottery = {
            "id": LOTTERY_ID,
            "platform": "bilibili",
            "raw_url": CANONICAL_URL,
            "canonical_url": CANONICAL_URL,
            "rule_text": DEFAULT_RULE_TEXT,
            "rule_hash": saved_plan["rule_hash"],
            "authoritative_rule_snapshot_id": RULE_SNAPSHOT_ID,
            "action_plan_hash": saved_plan["plan_hash"],
            "action_plan": json.dumps(saved_plan, ensure_ascii=False),
        }

        class NoDatabase:
            async def fetch_one(self, *_args, **_kwargs):
                raise AssertionError("batched validation performed a DB read")

            async def fetch_all(self, *_args, **_kwargs):
                raise AssertionError("batched validation performed a DB read")

        original_database = real_run_readiness.database
        real_run_readiness.database = NoDatabase()
        try:
            result = asyncio.run(
                real_run_readiness.validate_real_run_evidence(
                    lottery,
                    account_id=ACCOUNT_ID,
                    evidence_batch=batch,
                )
            )
        finally:
            real_run_readiness.database = original_database

        self.assertTrue(result["allowed"])
        self.assertTrue(result["execution_evidence_bound"])
        self.assertEqual(result["blockers"], [])

    def test_succeeded_task_without_observation_is_not_ready(self):
        result = self.evaluate(None)
        self.assertFalse(result["shadow_ready"])
        self.assertIn("exact_execution_evidence_required", result["blockers"])

    def test_succeeded_task_without_exact_evidence_binding_is_not_ready(self):
        result = self.evaluate(self.observation(), evidence=False)
        self.assertFalse(result["shadow_ready"])
        self.assertIn("exact_execution_evidence_required", result["blockers"])

    def test_tampered_shadow_observation_hash_is_not_ready(self):
        result = self.evaluate(self.observation(), evidence_hash="0" * 64)
        self.assertFalse(result["shadow_ready"])
        self.assertIn("exact_execution_evidence_required", result["blockers"])

    def test_exact_evidence_requires_an_explicit_account_scope(self):
        result = self.evaluate(
            self.observation(),
            account_id=None,
        )
        self.assertFalse(result["shadow_ready"])
        self.assertIn("execution_account_scope_required", result["blockers"])

    def test_empty_rule_text_blocks_real_run(self):
        result = self.evaluate(
            self.observation(),
            rule_text="",
            action_plan=complete_action_plan(""),
        )
        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_rule_text_required", result["blockers"])

    def test_current_rule_review_requirement_cannot_be_overridden_by_saved_plan(self):
        rule_text = "#required-topic#"
        saved_plan = complete_action_plan(
            rule_text,
            required_actions=REQUIRED_ACTIONS,
            content_requirements=empty_content_requirements(),
            unsupported_actions=[],
        )
        current_rule = {
            "is_lottery": True,
            "required_actions": REQUIRED_ACTIONS,
            "review_required": True,
            "unsupported_actions": ["topic_tag"],
            "ambiguity_patterns": [],
            "content_requirements": {
                "follow_targets": [FOLLOW_TARGET],
                "commented": {
                    "topic_tags": [rule_text],
                    "mentions": [],
                },
                "reposted": {"topic_tags": [], "mentions": []},
            },
        }
        with mock.patch.object(
            real_run_readiness,
            "parse_lottery_rule",
            return_value=current_rule,
        ):
            result = self.evaluate(
                self.observation(),
                rule_text=rule_text,
                action_plan=saved_plan,
            )

        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_rule_requirements_unresolved", result["blockers"])

    def test_exact_asus_rule_cannot_be_overridden_by_saved_ready_plan(self):
        rule_text = (
            "带话题 #ASUS翻转夏日#并@ASUS华硕官方UP 晒出你家‘踩稿官’的视频/照片+翻译，"
            "赢ROG键盘，这波不亏！关注@ASUS华硕官方UP +转评赞本条动态"
        )
        parsed_rule = parse_rule_fixture(rule_text, "bilibili")
        saved_plan = complete_action_plan(
            rule_text,
            required_actions=REQUIRED_ACTIONS,
            content_requirements=parsed_rule["content_requirements"],
            unsupported_actions=parsed_rule["unsupported_actions"],
            payload_overrides={
                "commented": {
                    "text": (
                        "#ASUS翻转夏日# @ASUS华硕官方UP "
                        "我家踩稿官：Cat means 猫"
                    ),
                    "topic_tags": ["#ASUS翻转夏日#"],
                    "mentions": [FOLLOW_TARGET],
                    "media_refs": ["evidence://cat.jpg"],
                    "translation": "Cat means 猫",
                },
            },
        )

        result = self.evaluate(
            self.observation(),
            rule_text=rule_text,
            action_plan=saved_plan,
        )

        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("bilibili_media_submission_unsupported", result["blockers"])

    def test_stale_action_plan_is_not_authoritatively_ready(self):
        rule_text = "supported lottery rule"
        requirements = empty_content_requirements()
        current_rule = {
            "is_lottery": True,
            "required_actions": ["followed", "liked"],
            "review_required": False,
            "unsupported_actions": [],
            "ambiguity_patterns": [],
            "content_requirements": requirements,
        }
        saved_plan = complete_action_plan(
            rule_text,
            required_actions=["followed"],
            content_requirements=requirements,
            unsupported_actions=[],
        )

        with mock.patch.object(real_run_readiness, "parse_lottery_rule", return_value=current_rule):
            result = self.evaluate(
                self.observation(),
                rule_text=rule_text,
                action_plan=saved_plan,
            )

        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_action_plan_stale", result["blockers"])

    def test_review_flag_must_be_explicitly_false(self):
        saved_plan = complete_action_plan()
        saved_plan.pop("review_required")
        saved_plan["plan_hash"] = compute_action_plan_hash(saved_plan)
        result = self.evaluate(self.observation(), action_plan=saved_plan)

        self.assertFalse(result["allowed"])
        self.assertFalse(result["action_plan_ready"])
        self.assertIn("lottery_rule_review_required", result["blockers"])

    def test_false_review_flag_requires_explicit_operator_attestation(self):
        for field, value in (
            ("reviewed_by", None),
            ("rule_complete_confirmed", False),
        ):
            with self.subTest(field=field):
                saved_plan = complete_action_plan()
                saved_plan[field] = value
                saved_plan["plan_hash"] = compute_action_plan_hash(saved_plan)
                result = self.evaluate(self.observation(), action_plan=saved_plan)

                self.assertFalse(result["allowed"])
                self.assertFalse(result["action_plan_ready"])
                self.assertIn(
                    "action_plan_review_attestation_invalid",
                    result["blockers"],
                )

    def test_required_actions_must_be_a_unique_supported_string_list(self):
        invalid_actions = (
            "followed",
            ["followed", "followed"],
            ["followed", "unsupported"],
            ["followed", None],
        )

        for required_actions in invalid_actions:
            with self.subTest(required_actions=required_actions):
                saved_plan = complete_action_plan()
                saved_plan["required_actions"] = required_actions
                saved_plan["plan_hash"] = compute_action_plan_hash(saved_plan)
                result = self.evaluate(
                    self.observation(),
                    action_plan=saved_plan,
                )

            self.assertFalse(result["allowed"])
            self.assertFalse(result["action_plan_ready"])
            self.assertIn("action_plan_required_actions_invalid", result["blockers"])

    def test_request_hash_budget_exhaustion_is_logged_once(self):
        screenshot_path = str(self.screenshot_path)
        batch = real_run_readiness.RealRunEvidenceBatch(account_id=None)
        batch.shadows[LOTTERY_ID] = {
            "task_id": "shadow-task",
            "account_id": ACCOUNT_ID,
            "screenshot_path": screenshot_path,
        }
        observation = complete_observation(screenshot_path)
        observation["platform"] = "generic"
        batch.observations["shadow-task"] = {"payload": json.dumps(observation)}
        batch.evidence_files[("shadow-task", str(ACCOUNT_ID), LOTTERY_ID)] = {
            "file_path": screenshot_path,
            "sha256": self.screenshot_hash,
        }
        lottery = {
            "id": LOTTERY_ID,
            "platform": "generic",
            "raw_url": "https://example.com/lottery/11",
            "rule_text": DEFAULT_RULE_TEXT,
            "action_plan": json.dumps(
                {"required_actions": REQUIRED_ACTIONS, "review_required": False}
            ),
        }

        def exhaust_budget(*_args, hash_budget=None, **_kwargs):
            hash_budget["remaining"] = 0
            hash_budget["exhausted"] = 1
            hash_budget["required_bytes"] = 1024
            return False

        with (
            mock.patch.object(
                real_run_readiness,
                "shadow_screenshot_integrity_matches",
                side_effect=exhaust_budget,
            ),
            mock.patch.object(real_run_readiness, "structured_log") as log,
        ):
            asyncio.run(
                real_run_readiness.validate_real_run_evidence(
                    lottery,
                    evidence_batch=batch,
                )
            )
            asyncio.run(
                real_run_readiness.validate_real_run_evidence(
                    lottery,
                    evidence_batch=batch,
                )
            )

        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args.args[:2], ("warning", "real_run_evidence_hash_budget_exhausted"))

    def test_browser_shadow_cannot_replace_bilibili_api_path_probe(self):
        browser_observation = complete_observation(str(self.screenshot_path))
        result = self.evaluate(
            browser_observation,
        )

        self.assertFalse(result["shadow_ready"])
        self.assertFalse(result["probe_ready"])
        self.assertFalse(result["allowed"])
        self.assertIn("exact_execution_evidence_required", result["blockers"])


if __name__ == "__main__":
    unittest.main()
