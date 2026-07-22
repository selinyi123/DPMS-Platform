import base64
import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from fastapi import HTTPException  # noqa: E402

from app.action_plan import (  # noqa: E402
    WEIBO_ACTION_CAPABILITY_REQUIREMENTS,
    WEIBO_ACTION_ORDER,
    WEIBO_MANUAL_EXECUTION_BLOCKER,
    WEIBO_MANUAL_EXECUTION_PATH,
    WEIBO_OAUTH_EXECUTION_PATH,
    WEIBO_RIP_ACTIONS,
    ActionPlanV2Error,
    compute_action_plan_hash,
    default_execution_path_for_platform,
    validate_action_payload,
    validate_action_plan_v2,
    weibo_runtime_capability_requirements,
)
from app.adapter_config import platform_real_adapter_kind  # noqa: E402
from app.api.accounts import (  # noqa: E402
    account_credential_kind,
    list_accounts,
    queue_account_calibration,
)
from app.api.lotteries import (  # noqa: E402
    account_execution_path_for_dispatch,
    pick_account,
    trusted_weibo_rip,
)
from app.api.metrics import weibo_oauth_capability_summary  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.lottery_rules import parse_lottery_rule  # noqa: E402
from app.services.real_run_readiness import (  # noqa: E402
    validate_weibo_oauth_capability_attestation,
    validate_weibo_oauth_contract,
)
from app.utils.weibo_oauth_credential import (  # noqa: E402
    WeiboOAuthCredentialError,
    parse_weibo_oauth_credential,
)
from app.utils.crypto import (  # noqa: E402
    CREDENTIAL_AAD,
    WEIBO_RIP_HMAC_CONTEXT,
    cookie_vault,
    weibo_rip_hmac,
)


def complete_weibo_plan(
    *,
    path: str = WEIBO_OAUTH_EXECUTION_PATH,
    friend_mode: str = "exact",
    friend_handles: tuple[str, ...] = ("@好友甲", "@好友乙", "@好友丙"),
) -> dict:
    source_mentions = ["@官方客服"]
    bound_mentions = [*source_mentions, "@品牌官方", *friend_handles]
    comment_text = " ".join(["参与微博抽奖", *bound_mentions])
    actions = list(WEIBO_ACTION_ORDER)
    plan = {
        "version": 2,
        "platform": "weibo",
        "is_lottery": True,
        "required_actions": actions,
        "action_payloads": {
            "followed": {"target_handle": "@品牌官方"},
            "liked": {},
            "commented": {
                "text": comment_text,
                "topic_tags": [],
                "mentions": bound_mentions,
            },
            "favorited": {},
            "reposted": {},
        },
        "source_content_requirements": {
            "follow_targets": ["@品牌官方"],
            "commented": {"topic_tags": [], "mentions": source_mentions},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "content_requirements": {
            "follow_targets": ["@品牌官方"],
            "commented": {"topic_tags": [], "mentions": bound_mentions},
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": {
            "commented": {"mode": friend_mode, "count": 3}
        },
        "runtime_capability_requirements": (
            weibo_runtime_capability_requirements(actions)
            if path == WEIBO_OAUTH_EXECUTION_PATH
            else {}
        ),
        "execution_path_id": path,
        "rule_snapshot_id": 501,
        "rule_hash": "a" * 64,
        "review_required": False,
        "executable": path == WEIBO_OAUTH_EXECUTION_PATH,
        "confidence": 1.0,
        "source": "operator_complete_attestation",
        "reviewed_by": "admin-1",
        "rule_complete_confirmed": True,
        "unsupported_actions": ["mention_account", "mention_friends"],
        "represented_requirements": ["mention_account", "mention_friends"],
        "unresolved_requirements": [],
        "ambiguity_patterns": [],
        "payload_validation_errors": [],
        "capability_blockers": (
            []
            if path == WEIBO_OAUTH_EXECUTION_PATH
            else [WEIBO_MANUAL_EXECUTION_BLOCKER]
        ),
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def capability_result(
    *,
    now: datetime,
    calibration_id: str = "calibration-1",
    account_id: int = 7,
    execution_revision: int = 3,
    app_review_status: str = "approved",
    client_type: str = "weibo",
    denied: tuple[str, ...] = (),
) -> dict:
    actions = {}
    for action in WEIBO_ACTION_ORDER:
        requirement = WEIBO_ACTION_CAPABILITY_REQUIREMENTS[action]
        actions[action] = {
            "endpoint": requirement["endpoint"],
            "permission": requirement["permission"],
            "granted": action not in denied,
        }
    attested_at = now - timedelta(minutes=10)
    verified_at = now - timedelta(minutes=5)
    return {
        "identity": {
            "verified": True,
            "method": "weibo_account_get_uid",
            "uid": "1234567890",
        },
        "oauth_capabilities": {
            "contract_version": 1,
            "calibration_id": calibration_id,
            "account_id": account_id,
            "execution_revision": execution_revision,
            "credential_kind": "weibo_oauth",
            "identity_verified": True,
            "app_review_status": app_review_status,
            "client_type": client_type,
            "verified_at": verified_at.isoformat().replace("+00:00", "Z"),
            "evidence_source": "operator_attested_app_capabilities",
            "attested_by": "admin-1",
            "attested_at": attested_at.isoformat().replace("+00:00", "Z"),
            "actions": actions,
        },
    }


class WeiboActionPlanTests(unittest.TestCase):
    def test_oauth_is_default_and_selectors_never_become_write_path(self):
        self.assertEqual(
            default_execution_path_for_platform("weibo"),
            WEIBO_OAUTH_EXECUTION_PATH,
        )
        self.assertEqual(
            platform_real_adapter_kind(
                {"weibo": {action: ["selector"] for action in WEIBO_ACTION_ORDER}},
                "weibo",
            ),
            "oauth",
        )
        self.assertEqual(
            WEIBO_RIP_ACTIONS,
            frozenset({"followed", "commented", "reposted"}),
        )

    def test_exact_friend_count_excludes_source_and_follow_handles(self):
        validated = validate_action_plan_v2(complete_weibo_plan())

        self.assertEqual(
            validated.friend_mention_requirements,
            {"commented": {"mode": "exact", "count": 3}},
        )
        self.assertEqual(
            validated.source_content_requirements["commented"]["mentions"],
            ["@官方客服"],
        )

    def test_exact_friend_count_rejects_too_few_or_too_many(self):
        for handles in (("@好友甲", "@好友乙"), ("@甲", "@乙", "@丙", "@丁")):
            with self.subTest(handles=handles):
                with self.assertRaisesRegex(
                    ActionPlanV2Error,
                    "action_plan_friend_mention_count_mismatch",
                ):
                    validate_action_plan_v2(
                        complete_weibo_plan(friend_handles=handles)
                    )

    def test_minimum_friend_count_allows_more_handles(self):
        plan = complete_weibo_plan(
            friend_mode="minimum",
            friend_handles=("@甲", "@乙", "@丙", "@丁"),
        )

        self.assertEqual(
            validate_action_plan_v2(plan).friend_mention_requirements[
                "commented"
            ]["mode"],
            "minimum",
        )

    def test_normalized_duplicate_friend_handles_fail_closed(self):
        plan = complete_weibo_plan(
            friend_handles=("@Alice", "@alice", "@好友三"),
        )

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "action_plan_friend_mention_requirement_binding_mismatch",
        ):
            validate_action_plan_v2(plan)

    def test_whole_plan_unique_handle_limit_is_fail_closed(self):
        plan = complete_weibo_plan()
        repost_mentions = [f"@r{index:02d}" for index in range(28)]
        plan["action_payloads"]["reposted"] = {
            "text": " ".join(repost_mentions),
            "mentions": repost_mentions,
        }
        for field in ("source_content_requirements", "content_requirements"):
            plan[field]["reposted"]["mentions"] = repost_mentions
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "weibo_preflight_unique_handle_limit_exceeded",
        ):
            validate_action_plan_v2(plan)

    def test_whole_plan_limit_merges_nfkc_casefold_handle_identities(self):
        plan = complete_weibo_plan()
        repost_mentions = [f"@{chr(ord('a') + index)}" for index in range(26)]
        repost_mentions.extend(["@ALICE", "@\uff21\uff2c\uff29\uff23\uff25"])
        plan["action_payloads"]["reposted"] = {
            "text": " ".join(repost_mentions),
            "mentions": repost_mentions,
        }
        for field in ("source_content_requirements", "content_requirements"):
            plan[field]["reposted"]["mentions"] = repost_mentions
        plan["plan_hash"] = compute_action_plan_hash(plan)

        self.assertEqual(
            validate_action_plan_v2(plan).required_actions,
            WEIBO_ACTION_ORDER,
        )

    def test_friend_mentions_require_real_ascii_at_handles(self):
        for invalid_handle in ("好友甲", "foo", "＃话题", "＠Alice"):
            with self.subTest(invalid_handle=invalid_handle):
                plan = complete_weibo_plan(
                    friend_handles=(invalid_handle, "@好友乙", "@好友丙"),
                )
                with self.assertRaisesRegex(
                    ActionPlanV2Error,
                    "action_payload_mentions_invalid",
                ):
                    validate_action_plan_v2(plan)

    def test_source_and_follow_handles_are_excluded_by_normalized_identity(self):
        plan = complete_weibo_plan()
        bound_mentions = ["@brand", "@team", "@好友甲", "@好友乙", "@好友丙"]
        plan["action_payloads"]["followed"]["target_handle"] = "@ＴＥＡＭ"
        plan["action_payloads"]["commented"] = {
            "text": "参与微博抽奖 " + " ".join(bound_mentions),
            "topic_tags": [],
            "mentions": bound_mentions,
        }
        plan["source_content_requirements"]["follow_targets"] = ["@TEAM"]
        plan["source_content_requirements"]["commented"]["mentions"] = [
            "@Brand"
        ]
        plan["content_requirements"]["follow_targets"] = ["@ＴＥＡＭ"]
        plan["content_requirements"]["commented"]["mentions"] = bound_mentions
        plan["plan_hash"] = compute_action_plan_hash(plan)

        validated = validate_action_plan_v2(plan)

        self.assertEqual(
            validated.friend_mention_requirements["commented"]["count"],
            3,
        )

    def test_constrained_plan_requires_hashed_source_requirements(self):
        plan = complete_weibo_plan()
        plan.pop("source_content_requirements")
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "action_plan_friend_mention_requirement_binding_mismatch",
        ):
            validate_action_plan_v2(plan)

    def test_runtime_capability_contract_is_exact(self):
        plan = complete_weibo_plan()
        plan["runtime_capability_requirements"]["actions"]["liked"][
            "permission"
        ] = "standard"
        plan["plan_hash"] = compute_action_plan_hash(plan)

        with self.assertRaisesRegex(
            ActionPlanV2Error, "weibo_oauth_capability_contract_mismatch"
        ):
            validate_action_plan_v2(plan)

    def test_manual_fallback_is_non_executable_and_plain_repost_is_valid(self):
        manual = complete_weibo_plan(path=WEIBO_MANUAL_EXECUTION_PATH)
        validated = validate_action_plan_v2(manual, require_executable=False)
        self.assertEqual(validated.payload_for("reposted"), {})

        manual["executable"] = True
        manual["plan_hash"] = compute_action_plan_hash(manual)
        with self.assertRaisesRegex(
            ActionPlanV2Error, "weibo_manual_plan_must_be_non_executable"
        ):
            validate_action_plan_v2(manual, require_executable=False)

    def test_comment_and_repost_enforce_140_utf16_units_without_truncation(self):
        for action in ("commented", "reposted"):
            with self.subTest(action=action, boundary="140-bmp"):
                payload = validate_action_payload(
                    action,
                    {"text": "中" * 140},
                    platform="weibo",
                )
                self.assertEqual(payload["text"], "中" * 140)
            with self.subTest(action=action, boundary="141-bmp"):
                with self.assertRaisesRegex(
                    ActionPlanV2Error,
                    f"weibo_{action}_text_too_long",
                ):
                    validate_action_payload(
                        action,
                        {"text": "中" * 141},
                        platform="weibo",
                    )

    def test_non_bmp_emoji_counts_as_two_utf16_units(self):
        self.assertEqual(
            validate_action_payload(
                "commented",
                {"text": ("中" * 138) + "😀"},
                platform="weibo",
            )["text"],
            ("中" * 138) + "😀",
        )
        with self.assertRaisesRegex(
            ActionPlanV2Error, "weibo_commented_text_too_long"
        ):
            validate_action_payload(
                "commented",
                {"text": ("中" * 139) + "😀"},
                platform="weibo",
            )


    def test_lone_surrogates_return_stable_field_errors_before_plan_hashing(self):
        with self.assertRaises(ActionPlanV2Error) as text_error:
            validate_action_payload(
                "commented",
                {"text": "\ud800"},
                platform="weibo",
            )
        self.assertEqual(
            text_error.exception.code,
            "action_payload_commented_text_invalid",
        )

        with self.assertRaises(ActionPlanV2Error) as mention_error:
            validate_action_payload(
                "commented",
                {"text": "valid", "mentions": ["@\ud800"]},
                platform="weibo",
            )
        self.assertEqual(
            mention_error.exception.code,
            "action_payload_mentions_invalid",
        )

        plan = complete_weibo_plan()
        plan["action_payloads"]["commented"]["text"] = "\ud800"
        with self.assertRaises(ActionPlanV2Error) as plan_error:
            validate_action_plan_v2(plan)
        self.assertEqual(
            plan_error.exception.code,
            "action_payload_commented_text_invalid",
        )

    def test_required_mention_is_not_satisfied_by_longer_handle_prefix(self):
        with self.assertRaisesRegex(
            ActionPlanV2Error,
            "action_payload_required_token_missing",
        ):
            validate_action_payload(
                "commented",
                {"text": "hello @alice2", "mentions": ["@alice"]},
                platform="weibo",
            )


class WeiboRuleTests(unittest.TestCase):
    def test_plain_friend_count_is_minimum_and_comment_scoped(self):
        plan = parse_lottery_rule(
            "微博抽奖：关注@品牌官方，转评赞本条微博，评论并@3位好友",
            "weibo",
        )

        self.assertEqual(
            plan["friend_mention_requirements"],
            {"commented": {"mode": "minimum", "count": 3}},
        )

    def test_only_explicit_exact_wording_creates_exact_constraint(self):
        plan = parse_lottery_rule(
            "微博抽奖：评论恰好@三位好友并转发点赞",
            "weibo",
        )

        self.assertEqual(
            plan["friend_mention_requirements"],
            {"commented": {"mode": "exact", "count": 3}},
        )

    def test_repost_friend_instruction_stays_repost_scoped(self):
        plan = parse_lottery_rule(
            "微博抽奖：转发并@3位好友，点赞收藏",
            "weibo",
        )

        self.assertEqual(
            plan["friend_mention_requirements"],
            {"reposted": {"mode": "minimum", "count": 3}},
        )
        self.assertIn("favorited", plan["required_actions"])


class WeiboOAuthEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 22, 6, 0, tzinfo=timezone.utc)

    def validate(self, result, **overrides):
        values = {
            "required_actions": ("commented", "reposted"),
            "account_id": 7,
            "execution_revision": 3,
            "calibration_fresh": True,
            "expected_calibration_id": "calibration-1",
            "expected_uid": "1234567890",
            "now": self.now,
        }
        values.update(overrides)
        return validate_weibo_oauth_capability_attestation(result, **values)

    def test_accepts_fresh_admin_attested_required_grants(self):
        status = self.validate(capability_result(now=self.now))

        self.assertTrue(status["ready"])
        self.assertEqual(status["blockers"], [])
        self.assertFalse(status["evidence"]["secret_material_exposed"])

    def test_test_only_and_denied_action_remain_blocked(self):
        status = self.validate(
            capability_result(
                now=self.now,
                app_review_status="test_only",
                denied=("reposted",),
            )
        )

        self.assertFalse(status["ready"])
        self.assertIn("weibo_oauth_app_review_required", status["blockers"])
        self.assertIn(
            "weibo_oauth_action_capability_denied", status["blockers"]
        )
        self.assertEqual(status["denied_actions"], ["reposted"])

    def test_rejects_calibration_uid_revision_and_provenance_mismatch(self):
        result = capability_result(now=self.now)
        result["oauth_capabilities"]["evidence_source"] = (
            "official_oauth_authorization"
        )

        status = self.validate(
            result,
            execution_revision=4,
            expected_calibration_id="other-calibration",
            expected_uid="999",
        )

        self.assertFalse(status["ready"])
        self.assertIn(
            "weibo_oauth_capability_contract_mismatch", status["blockers"]
        )
        self.assertIn(
            "weibo_oauth_execution_revision_mismatch", status["blockers"]
        )
        self.assertIn(
            "weibo_oauth_identity_verification_required", status["blockers"]
        )

    def test_rejects_extra_secret_field_even_on_unrequested_action(self):
        result = capability_result(now=self.now)
        result["oauth_capabilities"]["actions"]["liked"]["access_token"] = (
            "must-not-survive"
        )

        status = self.validate(result)

        self.assertFalse(status["ready"])
        self.assertIn(
            "weibo_oauth_capability_contract_mismatch", status["blockers"]
        )
        self.assertNotIn("must-not-survive", repr(status["evidence"]))

    def test_partial_grant_does_not_mark_platform_fully_ready(self):
        result = capability_result(
            now=self.now,
            denied=("followed", "commented", "favorited", "reposted"),
        )
        summary = weibo_oauth_capability_summary(
            [
                {
                    "id": 7,
                    "execution_revision": 3,
                    "result": result,
                    "calibration_fresh": True,
                }
            ]
        )

        self.assertEqual(summary["any_action_accounts"], 1)
        self.assertEqual(summary["full_action_accounts"], 0)
        self.assertEqual(summary["action_accounts"]["liked"], 1)


class WeiboCredentialAndNetworkTests(unittest.TestCase):
    def test_oauth_credential_contains_only_secret_identity_and_expiry(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        value = {
            "credential_kind": "weibo_oauth",
            "access_token": "safe-placeholder-token",
            "uid": "1234567890",
            "expires_at": future.isoformat().replace("+00:00", "Z"),
        }

        parsed = parse_weibo_oauth_credential(json.dumps(value))

        self.assertEqual(set(parsed), set(value))

    def test_privilege_claims_are_forbidden_in_credential(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        value = {
            "credential_kind": "weibo_oauth",
            "access_token": "safe-placeholder-token",
            "uid": "1234567890",
            "expires_at": future.isoformat().replace("+00:00", "Z"),
            "app_review_status": "approved",
        }

        with self.assertRaisesRegex(
            WeiboOAuthCredentialError, "weibo_oauth_credential_invalid"
        ):
            parse_weibo_oauth_credential(json.dumps(value))

    def test_oauth_credential_rejects_duplicate_keys_and_unicode_digits(self):
        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")
        duplicate_uid = (
            '{"credential_kind":"weibo_oauth",'
            '"access_token":"safe-placeholder-token",'
            '"uid":"123","uid":"456",'
            f'"expires_at":{json.dumps(future)}}}'
        )
        with self.assertRaisesRegex(
            WeiboOAuthCredentialError, "weibo_oauth_credential_invalid"
        ):
            parse_weibo_oauth_credential(duplicate_uid)

        unicode_uid = {
            "credential_kind": "weibo_oauth",
            "access_token": "safe-placeholder-token",
            "uid": "１２３",
            "expires_at": future,
        }
        with self.assertRaisesRegex(
            WeiboOAuthCredentialError, "weibo_oauth_uid_invalid"
        ):
            parse_weibo_oauth_credential(json.dumps(unicode_uid))

    def test_oauth_credential_requires_execution_time_reserve(self):
        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        base = {
            "credential_kind": "weibo_oauth",
            "access_token": "safe-placeholder-token",
            "uid": "1234567890",
        }
        expiring = dict(
            base,
            expires_at=(now + timedelta(seconds=899)).isoformat(),
        )
        with self.assertRaisesRegex(
            WeiboOAuthCredentialError, "weibo_oauth_credential_expiring_soon"
        ):
            parse_weibo_oauth_credential(json.dumps(expiring), now=now)

        sufficient = dict(
            base,
            expires_at=(now + timedelta(seconds=900)).isoformat(),
        )
        self.assertEqual(
            parse_weibo_oauth_credential(json.dumps(sufficient), now=now)["uid"],
            base["uid"],
        )

    def test_rip_binding_uses_purpose_derived_hmac(self):
        canonical_ip = "8.8.8.8"
        master = base64.b64decode(settings.encryption_key, validate=True)
        derived = hmac.new(
            master,
            WEIBO_RIP_HMAC_CONTEXT,
            hashlib.sha256,
        ).digest()
        expected = hmac.new(
            derived,
            canonical_ip.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(weibo_rip_hmac(canonical_ip), expected)
        self.assertEqual(weibo_rip_hmac(""), "")
        self.assertNotEqual(
            weibo_rip_hmac(canonical_ip),
            hashlib.sha256(canonical_ip.encode("ascii")).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "weibo_rip_hmac_input_invalid"):
            weibo_rip_hmac("127.0.0.1")

    def test_rip_uses_overwritten_real_ip_and_ignores_spoofed_xff(self):
        request = SimpleNamespace(
            headers={
                "x-real-ip": "8.8.8.8",
                "x-forwarded-for": "1.2.3.4, 8.8.8.8",
            },
            client=SimpleNamespace(host="172.18.0.2"),
        )

        self.assertEqual(trusted_weibo_rip(request), "8.8.8.8")

    def test_rip_fails_closed_without_public_trusted_ingress_value(self):
        request = SimpleNamespace(
            headers={"x-forwarded-for": "8.8.8.8"},
            client=SimpleNamespace(host="127.0.0.1"),
        )

        with self.assertRaises(HTTPException) as caught:
            trusted_weibo_rip(request)
        self.assertEqual(
            caught.exception.detail, {"code": "weibo_public_rip_required"}
        )

    def test_phase_migration_includes_distinct_favorite_enum(self):
        migration = (
            Path(__file__).parents[1]
            / "migrations"
            / "0012_task_phase_favorited.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("'commented','favorited','reposted'", migration)

    def test_privacy_migration_redacts_legacy_plaintext_rip(self):
        migration = (
            Path(__file__).parents[1]
            / "migrations"
            / "0013_redact_legacy_plaintext_weibo_rip.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("JSON_REMOVE(payload, '$.weibo_rip')", migration)
        self.assertIn("legacy_plaintext_weibo_rip_redacted", migration)
        self.assertIn("UPDATE failed_task_messages", migration)
        task_update = migration.split("UPDATE outbox_events", 1)[0]
        self.assertNotIn("o.status IN ('pending', 'sending')", task_update)

    def test_privacy_migration_settles_queued_tasks_without_orphaning_locks(self):
        migration = (
            Path(__file__).parents[1]
            / "migrations"
            / "0013_redact_legacy_plaintext_weibo_rip.sql"
        ).read_text(encoding="utf-8")
        task_updates = migration.split("UPDATE task_runs tr")
        self.assertEqual(len(task_updates), 2)
        queued_update = task_updates[1].split("UPDATE outbox_events", 1)[0]

        self.assertIn("LEFT JOIN lotteries l", queued_update)
        self.assertIn("l.execution_lock = tr.task_id", queued_update)
        self.assertIn("l.status = 'claimed'", queued_update)
        self.assertIn("LEFT JOIN account_operation_leases lease", queued_update)
        self.assertIn("lease.task_id = tr.task_id", queued_update)
        self.assertIn("lease.owner_id = tr.task_id", queued_update)
        self.assertIn("tr.worker_id = NULL", queued_update)
        self.assertIn("tr.stream_message_id = NULL", queued_update)
        self.assertIn("tr.lease_expires_at = NULL", queued_update)
        self.assertIn("tr.reconciliation_required = 0", queued_update)
        self.assertIn("l.status = 'pending'", queued_update)
        self.assertIn("l.execution_lock = NULL", queued_update)
        self.assertIn("lease.released_at = COALESCE", queued_update)
        self.assertIn("WHERE tr.status = 'queued'", queued_update)
        self.assertNotIn("tr.reconciliation_required = 1", queued_update)
        self.assertNotIn("WHERE tr.status = 'running'", migration)
        self.assertNotIn(
            "legacy_plaintext_weibo_rip_running_reconciliation_required",
            migration,
        )

    def test_account_credential_kind_distinguishes_oauth_and_cookie(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        oauth = cookie_vault.encrypt(
            json.dumps(
                {
                    "credential_kind": "weibo_oauth",
                    "access_token": "safe-placeholder-token",
                    "uid": "1234567890",
                    "expires_at": future.isoformat().replace("+00:00", "Z"),
                }
            ),
            aad=CREDENTIAL_AAD,
        )
        browser = cookie_vault.encrypt(
            json.dumps([{"name": "SUB", "value": "legacy-session"}]),
            aad=CREDENTIAL_AAD,
        )

        self.assertEqual(account_credential_kind("weibo", oauth), "weibo_oauth")
        self.assertEqual(
            account_credential_kind("weibo", browser),
            "browser_session",
        )

    def test_expired_oauth_keeps_kind_while_freshness_gate_rejects_it(self):
        expired = cookie_vault.encrypt(
            json.dumps(
                {
                    "credential_kind": "weibo_oauth",
                    "access_token": "safe-placeholder-token",
                    "uid": "1234567890",
                    "expires_at": "2020-01-01T00:00:00Z",
                }
            ),
            aad=CREDENTIAL_AAD,
        )

        self.assertEqual(
            account_credential_kind("weibo", expired),
            "weibo_oauth",
        )


class WeiboAccountSelectionTests(unittest.IsolatedAsyncioTestCase):
    def oauth_envelope(self) -> bytes:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        return cookie_vault.encrypt(
            json.dumps(
                {
                    "credential_kind": "weibo_oauth",
                    "access_token": "safe-placeholder-token",
                    "uid": "1234567890",
                    "expires_at": future.isoformat().replace("+00:00", "Z"),
                }
            ),
            aad=CREDENTIAL_AAD,
        )

    def browser_envelope(self) -> bytes:
        return cookie_vault.encrypt(
            json.dumps([{"name": "SUB", "value": "legacy-session"}]),
            aad=CREDENTIAL_AAD,
        )

    def candidate(self, account_id: int, credential: bytes, result=None) -> dict:
        return {
            "id": account_id,
            "platform": "weibo",
            "status": "ready",
            "encrypted_credential": credential,
            "execution_revision": 3,
            "latest_calibration_id": f"calibration-{account_id}",
            "latest_calibration_status": "succeeded",
            "latest_calibration_result": result or {},
            "latest_calibration_fresh": True,
        }

    async def test_mixed_pool_selects_credential_matching_execution_path(self):
        now = datetime.now(timezone.utc)
        oauth = self.candidate(
            2,
            self.oauth_envelope(),
            capability_result(
                now=now,
                calibration_id="calibration-2",
                account_id=2,
                execution_revision=3,
            ),
        )
        browser = self.candidate(1, self.browser_envelope())
        fetch_all = AsyncMock(side_effect=[[browser, oauth], [oauth, browser]])
        no_risk = AsyncMock(return_value={"has_recent_risk": False})
        with patch(
            "app.api.lotteries.load_strategy_account_recommendations",
            new=AsyncMock(return_value={"weibo": []}),
        ), patch(
            "app.api.lotteries.database.fetch_all",
            new=fetch_all,
        ), patch(
            "app.api.lotteries.recent_account_risk",
            new=no_risk,
        ):
            oauth_selected = await pick_account(
                None,
                "weibo",
                execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
                required_actions=("commented", "reposted"),
            )
            browser_selected = await pick_account(
                None,
                "weibo",
                execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
            )

        self.assertEqual(oauth_selected["id"], 2)
        self.assertEqual(browser_selected["id"], 1)

    async def test_all_recent_risk_candidates_return_none_without_fallback(self):
        candidates = [
            self.candidate(1, self.browser_envelope()),
            self.candidate(2, self.browser_envelope()),
        ]
        recent_risk = AsyncMock(return_value={"has_recent_risk": True})
        with patch(
            "app.api.lotteries.load_strategy_account_recommendations",
            new=AsyncMock(return_value={"weibo": []}),
        ), patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(return_value=candidates),
        ), patch(
            "app.api.lotteries.recent_account_risk",
            new=recent_risk,
        ):
            selected = await pick_account(
                None,
                "weibo",
                execution_path_id=WEIBO_MANUAL_EXECUTION_PATH,
            )

        self.assertIsNone(selected)
        self.assertEqual(recent_risk.await_count, 2)

    async def test_oauth_dry_run_needs_valid_kind_but_not_write_attestation(self):
        oauth_without_capability = self.candidate(
            3,
            self.oauth_envelope(),
            {"identity": {"verified": True, "uid": "1234567890"}},
        )
        with patch(
            "app.api.lotteries.load_strategy_account_recommendations",
            new=AsyncMock(return_value={"weibo": []}),
        ), patch(
            "app.api.lotteries.database.fetch_all",
            new=AsyncMock(
                side_effect=[
                    [oauth_without_capability],
                    [oauth_without_capability],
                ]
            ),
        ), patch(
            "app.api.lotteries.recent_account_risk",
            new=AsyncMock(return_value={"has_recent_risk": False}),
        ):
            dry_selected = await pick_account(
                None,
                "weibo",
                execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
                required_actions=("commented",),
                require_weibo_capability=False,
            )
            real_selected = await pick_account(
                None,
                "weibo",
                execution_path_id=WEIBO_OAUTH_EXECUTION_PATH,
                required_actions=("commented",),
                require_weibo_capability=True,
            )

        self.assertEqual(dry_selected["id"], 3)
        self.assertIsNone(real_selected)

    def test_oauth_plan_shadow_uses_browser_but_dry_and_real_use_oauth(self):
        for task_mode, expected in (
            ("shadow_run", WEIBO_MANUAL_EXECUTION_PATH),
            ("dry_run", WEIBO_OAUTH_EXECUTION_PATH),
            ("real_run", WEIBO_OAUTH_EXECUTION_PATH),
        ):
            with self.subTest(task_mode=task_mode):
                self.assertEqual(
                    account_execution_path_for_dispatch(
                        "weibo",
                        task_mode=task_mode,
                        stored_execution_path=WEIBO_OAUTH_EXECUTION_PATH,
                    ),
                    expected,
                )

    async def test_oauth_gate_uses_bound_dry_run_not_browser_selector_shadow(self):
        now = datetime.now(timezone.utc)
        account = {
            "id": 7,
            "status": "ready",
            "execution_revision": 3,
            "encrypted_credential": self.oauth_envelope(),
            "calibration_id": "calibration-1",
            "calibration_status": "succeeded",
            "calibration_result": capability_result(now=now),
            "calibration_fresh": True,
        }
        base_contract = {
            "blockers": [],
            "action_plan_ready": True,
            "selector_observation_complete": False,
        }
        manual_validator = AsyncMock(return_value=base_contract)
        with patch(
            "app.services.real_run_readiness.validate_manual_only_contract",
            new=manual_validator,
        ), patch(
            "app.services.real_run_readiness.database.fetch_one",
            new=AsyncMock(side_effect=[account, {"task_id": "dry-task-1"}]),
        ), patch(
            "app.services.real_run_readiness.recent_account_risk",
            new=AsyncMock(return_value={"has_recent_risk": False}),
        ):
            result = await validate_weibo_oauth_contract(
                {
                    "id": 81,
                    "canonical_url": "https://weibo.com/1234567890/AbCdEfGhI",
                    "action_plan": complete_weibo_plan(),
                },
                account_id=7,
            )

        self.assertTrue(result["allowed"])
        self.assertTrue(result["oauth_capability_ready"])
        self.assertFalse(result["shadow_ready"])
        self.assertTrue(result["execution_preflight_ready"])
        self.assertEqual(result["oauth_dry_run_task_id"], "dry-task-1")
        self.assertFalse(
            manual_validator.await_args.kwargs["manual_shadow_supported"]
        )


class WeiboAccountApiTests(unittest.IsolatedAsyncioTestCase):
    def oauth_envelope(self) -> bytes:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        return cookie_vault.encrypt(
            json.dumps(
                {
                    "credential_kind": "weibo_oauth",
                    "access_token": "safe-placeholder-token",
                    "uid": "1234567890",
                    "expires_at": future.isoformat().replace("+00:00", "Z"),
                }
            ),
            aad=CREDENTIAL_AAD,
        )

    async def test_list_accounts_exposes_kind_without_ciphertext(self):
        oauth = self.oauth_envelope()
        browser = cookie_vault.encrypt(
            json.dumps([{"name": "SUB", "value": "legacy-session"}]),
            aad=CREDENTIAL_AAD,
        )
        rows = [
            {
                "id": 1,
                "platform": "weibo",
                "encrypted_credential": oauth,
                "latest_risk_event": None,
                "latest_calibration": None,
                "current_task_run": None,
                "latest_task_run": None,
            },
            {
                "id": 2,
                "platform": "weibo",
                "encrypted_credential": browser,
                "latest_risk_event": None,
                "latest_calibration": None,
                "current_task_run": None,
                "latest_task_run": None,
            },
        ]
        with patch(
            "app.api.accounts.database.fetch_all",
            new=AsyncMock(return_value=rows),
        ):
            result = await list_accounts()

        self.assertEqual(
            [item["credential_kind"] for item in result],
            ["weibo_oauth", "browser_session"],
        )
        self.assertTrue(all("encrypted_credential" not in item for item in result))

    async def test_oauth_identity_calibration_kind_reaches_worker_queue(self):
        fetch = AsyncMock(return_value={"encrypted_credential": self.oauth_envelope()})
        execute = AsyncMock(return_value=1)
        xadd = AsyncMock(return_value="1-0")
        with patch("app.api.accounts.database.fetch_one", new=fetch), patch(
            "app.api.accounts.database.execute", new=execute
        ), patch(
            "app.api.accounts.redis._conn",
            new=SimpleNamespace(xadd=xadd),
        ):
            queued = await queue_account_calibration(7, "weibo")

        stream, fields = xadd.await_args.args
        self.assertEqual(stream, "account_calibration_requests")
        self.assertEqual(fields["calibration_kind"], "weibo_oauth_identity")
        self.assertNotIn("access_token", repr(fields))
        self.assertEqual(queued["calibration_kind"], "weibo_oauth_identity")


if __name__ == "__main__":
    unittest.main()
