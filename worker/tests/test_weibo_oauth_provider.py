"""Pure tests for the official Weibo OAuth capability and mutation provider."""

from __future__ import annotations

import copy
import base64
import hashlib
import hmac
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(WORKER_ROOT / "tools"))

from bilibili_dry_run_harness import (  # noqa: E402
    stub_httpx,
    stub_playwright,
    stub_worker_runtime_dependencies,
)


stub_playwright()
stub_httpx()
stub_worker_runtime_dependencies()

import httpx  # noqa: E402


if not hasattr(httpx, "HTTPStatusError"):
    class _HTTPStatusError(RuntimeError):
        def __init__(self, message, *, request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    httpx.HTTPStatusError = _HTTPStatusError

from app.account_calibrator import build_weibo_oauth_calibration_result  # noqa: E402
from app.action_plan import (  # noqa: E402
    ActionPlanV2Error,
    WEIBO_ACTION_ORDER,
    WEIBO_OAUTH_EXECUTION_PATH,
    compute_action_plan_hash,
    validate_action_plan_v2,
    weibo_runtime_capability_requirements,
)
from app.weibo.capabilities import (  # noqa: E402
    WeiboOAuthCapabilityError,
    build_weibo_oauth_capability_attestation,
    validate_weibo_oauth_capability_attestation,
)
from app.weibo.client import (  # noqa: E402
    ENDPOINTS,
    WeiboActionReceipt,
    WeiboApiActionOutcomeUnknown,
    WeiboApiClient,
    WeiboApiError,
    WeiboApiRejected,
    WeiboDuplicateOperation,
    WeiboOAuthIdentityClient,
    build_weibo_mutation_request,
    classify_weibo_api_rejection,
    status_identifier_from_canonical_uri,
    validate_weibo_text,
)
from app.weibo.executor import (  # noqa: E402
    WeiboExecutionOutcomeUnknown,
    WeiboOAuthExecutor,
)
from app.weibo.credentials import (  # noqa: E402
    WEIBO_RIP_AAD,
    WeiboOAuthCredentialError,
    decrypt_weibo_rip,
    parse_weibo_oauth_credential,
    validate_weibo_rip,
    weibo_rip_hmac,
)
from app import task_runner  # noqa: E402
from app import account_calibrator  # noqa: E402
from app import real_run_gate  # noqa: E402


NOW = datetime.now(timezone.utc).replace(microsecond=0)
ACCOUNT_ID = 17
EXECUTION_REVISION = 4
CALIBRATION_ID = str(uuid4())
STATUS_ID = "1234567890123"
FOLLOW_UID = "99887766"
TOKEN = "test-oauth-token-never-persist"
PUBLIC_RIP = "8.8.8.8"


def oauth_plan(actions=WEIBO_ACTION_ORDER, *, comment_text="#lottery# @friend_a @friend_b enter"):
    selected = list(actions)
    payloads = {
        "followed": {"target_handle": "@lottery_host"},
        "liked": {},
        "commented": {
            "text": comment_text,
            "topic_tags": ["#lottery#"],
            "mentions": ["@friend_a", "@friend_b"],
        },
        "favorited": {},
        "reposted": {},
    }
    plan = {
        "version": 2,
        "platform": "weibo",
        "rule_snapshot_id": 801,
        "rule_hash": "d" * 64,
        "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
        "required_actions": selected,
        "action_payloads": {
            action: copy.deepcopy(payloads[action]) for action in selected
        },
        "content_requirements": {
            "follow_targets": ["@lottery_host"] if "followed" in selected else [],
            "commented": {
                "topic_tags": ["#lottery#"] if "commented" in selected else [],
                "mentions": (
                    ["@friend_a", "@friend_b"] if "commented" in selected else []
                ),
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "source_content_requirements": {
            "follow_targets": ["@lottery_host"] if "followed" in selected else [],
            "commented": {
                "topic_tags": ["#lottery#"] if "commented" in selected else [],
                "mentions": [],
            },
            "reposted": {"topic_tags": [], "mentions": []},
        },
        "friend_mention_requirements": (
            {"commented": {"mode": "exact", "count": 2}}
            if "commented" in selected
            else {}
        ),
        "runtime_capability_requirements": weibo_runtime_capability_requirements(
            selected
        ),
        "executable": True,
        "review_required": False,
        "reviewed_by": "operator-1",
        "rule_complete_confirmed": True,
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def text_only_plan(action, text):
    plan = oauth_plan((action,))
    plan["action_payloads"][action] = {"text": text}
    plan["content_requirements"] = {
        "follow_targets": [],
        "commented": {"topic_tags": [], "mentions": []},
        "reposted": {"topic_tags": [], "mentions": []},
    }
    plan["source_content_requirements"] = copy.deepcopy(
        plan["content_requirements"]
    )
    plan["friend_mention_requirements"] = {}
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


def three_friend_plan():
    plan = oauth_plan(("followed", "commented"))
    mentions = ["@host", "@Alice", "@bob", "@carol"]
    plan["action_payloads"]["followed"] = {"target_handle": "@host"}
    plan["action_payloads"]["commented"] = {
        "text": " ".join(mentions),
        "topic_tags": [],
        "mentions": list(mentions),
    }
    plan["source_content_requirements"] = {
        "follow_targets": ["@host"],
        "commented": {"topic_tags": [], "mentions": ["@host"]},
        "reposted": {"topic_tags": [], "mentions": []},
    }
    plan["content_requirements"] = {
        "follow_targets": ["@host"],
        "commented": {"topic_tags": [], "mentions": list(mentions)},
        "reposted": {"topic_tags": [], "mentions": []},
    }
    plan["friend_mention_requirements"] = {
        "commented": {"mode": "exact", "count": 3}
    }
    plan["plan_hash"] = compute_action_plan_hash(plan)
    return plan


class WeiboActionPlanBoundaryTests(unittest.TestCase):
    def assert_plan_code(self, expected, plan):
        with self.assertRaises(ActionPlanV2Error) as caught:
            validate_action_plan_v2(plan)
        self.assertEqual(caught.exception.code, expected)

    def test_core_parity_140_utf16_units_for_comment_and_repost(self):
        validate_action_plan_v2(text_only_plan("commented", "汉" * 140))
        validate_action_plan_v2(text_only_plan("reposted", "😀" * 70))
        self.assert_plan_code(
            "weibo_commented_text_too_long",
            text_only_plan("commented", "汉" * 141),
        )
        self.assert_plan_code(
            "weibo_reposted_text_too_long",
            text_only_plan("reposted", "😀" * 70 + "x"),
        )
        invalid_surrogate = text_only_plan("commented", "valid")
        invalid_surrogate["action_payloads"]["commented"]["text"] = "\ud800"
        invalid_surrogate["plan_hash"] = "0" * 64
        self.assert_plan_code(
            "action_payload_commented_text_invalid",
            invalid_surrogate,
        )

    def test_source_host_plus_three_unique_friends_and_normalized_duplicates(self):
        plan = three_friend_plan()
        validate_action_plan_v2(plan)

        for duplicate in ("@alice", "@Ａlice"):
            tampered = copy.deepcopy(plan)
            tampered["action_payloads"]["commented"]["mentions"].append(duplicate)
            tampered["action_payloads"]["commented"]["text"] += f" {duplicate}"
            tampered["content_requirements"]["commented"]["mentions"].append(duplicate)
            tampered["plan_hash"] = compute_action_plan_hash(tampered)
            self.assert_plan_code(
                "action_plan_friend_mention_requirement_binding_mismatch",
                tampered,
            )

        invalid_at = copy.deepcopy(plan)
        invalid_at["action_payloads"]["commented"]["mentions"][-1] = "＠carol"
        invalid_at["content_requirements"]["commented"]["mentions"][-1] = "＠carol"
        invalid_at["action_payloads"]["commented"]["text"] += " ＠carol"
        invalid_at["plan_hash"] = compute_action_plan_hash(invalid_at)
        self.assert_plan_code("action_payload_mentions_invalid", invalid_at)

    def test_required_mention_is_not_satisfied_by_a_longer_handle_prefix(self):
        plan = oauth_plan(("commented",))
        plan["action_payloads"]["commented"]["text"] = (
            "#lottery# @friend_a2 @friend_b enter"
        )
        plan["plan_hash"] = compute_action_plan_hash(plan)
        self.assert_plan_code(
            "action_payload_required_token_missing",
            plan,
        )


def operator_attestation(*, actions=WEIBO_ACTION_ORDER):
    return {
        "version": 1,
        "attested_by": "admin-1",
        "attested_at": NOW.isoformat().replace("+00:00", "Z"),
        "app_review_status": "approved",
        "client_type": "weibo",
        "granted_actions": {
            action: action in actions for action in WEIBO_ACTION_ORDER
        },
    }


def attestation(*, actions=WEIBO_ACTION_ORDER, **overrides):
    value = build_weibo_oauth_capability_attestation(
        calibration_id=CALIBRATION_ID,
        account_id=ACCOUNT_ID,
        execution_revision=EXECUTION_REVISION,
        operator_attestation=operator_attestation(actions=actions),
        verified_at=NOW,
    )
    value.update(overrides)
    return value


class CapabilityAttestationTests(unittest.IsolatedAsyncioTestCase):
    def validate(self, value, *, actions=WEIBO_ACTION_ORDER):
        return validate_weibo_oauth_capability_attestation(
            value,
            calibration_id=CALIBRATION_ID,
            account_id=ACCOUNT_ID,
            execution_revision=EXECUTION_REVISION,
            runtime_capability_requirements=weibo_runtime_capability_requirements(
                actions
            ),
            now=NOW,
        )

    def assert_code(self, expected, value, *, actions=WEIBO_ACTION_ORDER):
        with self.assertRaises(WeiboOAuthCapabilityError) as caught:
            self.validate(value, actions=actions)
        self.assertEqual(caught.exception.code, expected)

    def test_exact_fresh_attestation_is_bound_and_contains_no_token(self):
        value = attestation()
        self.assertEqual(self.validate(value), value)
        self.assertEqual(value["calibration_id"], CALIBRATION_ID)
        self.assertEqual(set(value["actions"]), set(WEIBO_ACTION_ORDER))
        self.assertNotIn(TOKEN, repr(value))

    def test_stale_future_account_revision_and_grant_fail_closed(self):
        stale_time = (NOW - timedelta(hours=24, seconds=1)).isoformat()
        self.assert_code(
            "weibo_oauth_capability_evidence_stale",
            attestation(verified_at=stale_time, attested_at=stale_time),
        )
        self.assert_code(
            "weibo_oauth_capability_evidence_from_future",
            attestation(verified_at=(NOW + timedelta(seconds=1)).isoformat()),
        )
        self.assert_code(
            "weibo_oauth_account_binding_invalid",
            attestation(account_id=ACCOUNT_ID + 1),
        )
        self.assert_code(
            "weibo_oauth_execution_revision_mismatch",
            attestation(execution_revision=EXECUTION_REVISION + 1),
        )
        denied = attestation()
        denied["actions"]["commented"]["granted"] = False
        self.assert_code("weibo_oauth_action_not_granted:commented", denied)

        invalid_actor = operator_attestation()
        invalid_actor["attested_by"] = "\ud800"
        with self.assertRaises(WeiboOAuthCapabilityError) as caught:
            build_weibo_oauth_capability_attestation(
                calibration_id=CALIBRATION_ID,
                account_id=ACCOUNT_ID,
                execution_revision=EXECUTION_REVISION,
                operator_attestation=invalid_actor,
                verified_at=NOW,
            )
        self.assertEqual(
            caught.exception.code,
            "weibo_oauth_attestation_actor_invalid",
        )

    async def test_calibration_combines_official_identity_and_operator_attestation(self):
        class IdentityClient:
            async def check_identity(self):
                return "12345678"

        result = await build_weibo_oauth_calibration_result(
            IdentityClient(),
            calibration_id=CALIBRATION_ID,
            account_id=ACCOUNT_ID,
            execution_revision=EXECUTION_REVISION,
            operator_attestation=operator_attestation(),
            expected_uid="12345678",
        )
        self.assertEqual(result["identity"], {
            "verified": True,
            "method": "weibo_account_get_uid",
            "uid": "12345678",
        })
        self.assertNotIn("access_token", repr(result))


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.request = object()

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self):
        return None


def make_client(fake_http, *, actions=WEIBO_ACTION_ORDER):
    return WeiboApiClient(
        TOKEN,
        capability_attestation=attestation(actions=actions),
        calibration_id=CALIBRATION_ID,
        account_id=ACCOUNT_ID,
        execution_revision=EXECUTION_REVISION,
        runtime_capability_requirements=weibo_runtime_capability_requirements(actions),
        http_client=fake_http,
    )


class OfficialApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_client_does_not_accept_present_invalid_error_code(self):
        for invalid_code in (0, False, "", None, 21327.9):
            with self.subTest(error_code=invalid_code):
                fake = FakeHttpClient(
                    [FakeResponse({"uid": "12345678", "error_code": invalid_code})]
                )
                client = WeiboOAuthIdentityClient(TOKEN, http_client=fake)
                with self.assertRaises(WeiboApiRejected) as caught:
                    await client.check_identity()
                self.assertEqual(caught.exception.error_code, -1)
                self.assertFalse(caught.exception.confirmed_no_effect)
                self.assertEqual(len(fake.calls), 1)

        for status_code in (302, 400, 401, 403, 429):
            with self.subTest(status_code=status_code):
                fake = FakeHttpClient(
                    [FakeResponse({"uid": "12345678"}, status_code=status_code)]
                )
                client = WeiboOAuthIdentityClient(TOKEN, http_client=fake)
                with self.assertRaisesRegex(
                    WeiboApiError,
                    "weibo_oauth_identity_request_failed",
                ):
                    await client.check_identity()
                self.assertEqual(len(fake.calls), 1)

    async def test_all_five_mutations_use_fixed_endpoints_and_correct_rip_scope(self):
        fake = FakeHttpClient([
            FakeResponse({"idstr": FOLLOW_UID}),
            FakeResponse({"status": {"idstr": STATUS_ID}}),
            FakeResponse({
                "idstr": "2001",
                "text": "#lottery# @friend_a @friend_b enter",
                "status": {"idstr": STATUS_ID},
            }),
            FakeResponse({"status": {"idstr": STATUS_ID}}),
            FakeResponse({"idstr": "2002", "retweeted_status": {"idstr": STATUS_ID}}),
        ])
        client = make_client(fake)
        with patch(
            "app.weibo.client.weibo_rip_hmac", return_value="h" * 64
        ):
            receipts = [
                await client.follow(FOLLOW_UID, rip=PUBLIC_RIP, operation_key="weibo:followed:1"),
                await client.like(STATUS_ID, operation_key="weibo:liked:1"),
                await client.comment(
                    STATUS_ID,
                    "#lottery# @friend_a @friend_b enter",
                    rip=PUBLIC_RIP,
                    operation_key="weibo:commented:1",
                ),
                await client.favorite(STATUS_ID, operation_key="weibo:favorited:1"),
                await client.repost(STATUS_ID, rip=PUBLIC_RIP, operation_key="weibo:reposted:1"),
            ]
        self.assertEqual([receipt.action for receipt in receipts], list(WEIBO_ACTION_ORDER))
        self.assertEqual([call[1] for call in fake.calls], [ENDPOINTS[a] for a in WEIBO_ACTION_ORDER])
        bodies = [call[2]["data"] for call in fake.calls]
        self.assertEqual([body.get("rip") for body in bodies], [PUBLIC_RIP, None, PUBLIC_RIP, None, PUBLIC_RIP])
        self.assertEqual(bodies[1], {"id": STATUS_ID, "access_token": TOKEN})
        self.assertNotIn(TOKEN, repr(receipts))

    def test_comment_and_repost_enforce_140_utf16_units(self):
        self.assertEqual(validate_weibo_text("commented", "汉" * 140), "汉" * 140)
        self.assertEqual(validate_weibo_text("reposted", "😀" * 70), "😀" * 70)
        for action, value in (("commented", "汉" * 141), ("reposted", "😀" * 70 + "x")):
            with self.subTest(action=action):
                with self.assertRaises(WeiboApiError) as caught:
                    validate_weibo_text(action, value)
                self.assertEqual(str(caught.exception), f"weibo_{action}_text_too_long")
        with self.assertRaises(WeiboApiError) as caught:
            validate_weibo_text("commented", "\ud800")
        self.assertEqual(
            str(caught.exception),
            "action_payload_commented_text_invalid",
        )

    def test_numeric_status_ids_are_canonical_positive_int64_values(self):
        for status_id in ("7987885345", "9223372036854775807"):
            request = build_weibo_mutation_request("liked", status_id)
            self.assertEqual(request.target_id, status_id)
            self.assertEqual(
                status_identifier_from_canonical_uri(
                    f"canonical://weibo/status/{status_id}"
                ),
                status_id,
            )
        for status_id in (
            "0",
            "07987885345",
            "9223372036854775808",
            "７９８７８８５３４５",
        ):
            with self.subTest(status_id=status_id):
                with self.assertRaises(WeiboApiError):
                    build_weibo_mutation_request("liked", status_id)
                with self.assertRaises(WeiboApiError):
                    status_identifier_from_canonical_uri(
                        f"canonical://weibo/status/{status_id}"
                    )

    async def test_duplicate_unknown_and_explicit_rejection_are_single_attempt(self):
        duplicate_http = FakeHttpClient([FakeResponse({"status": {"idstr": STATUS_ID}})])
        duplicate = make_client(duplicate_http, actions=("liked",))
        await duplicate.like(STATUS_ID, operation_key="weibo:liked:duplicate")
        with self.assertRaises(WeiboDuplicateOperation):
            await duplicate.like(STATUS_ID, operation_key="weibo:liked:duplicate")
        self.assertEqual(len(duplicate_http.calls), 1)

        for outcome in (httpx.TransportError("timeout"), FakeResponse({}, status_code=503)):
            fake = FakeHttpClient([outcome])
            client = make_client(fake, actions=("liked",))
            with self.assertRaises(WeiboApiActionOutcomeUnknown):
                await client.like(STATUS_ID, operation_key=f"weibo:liked:{id(fake)}")
            self.assertEqual(len(fake.calls), 1)

        rejected_http = FakeHttpClient(
            [FakeResponse({"error_code": 21327, "error": "expired secret text"})]
        )
        rejected = make_client(rejected_http, actions=("liked",))
        with self.assertRaises(WeiboApiRejected) as caught:
            await rejected.like(STATUS_ID, operation_key="weibo:liked:rejected")
        self.assertEqual(caught.exception.category, "authentication_invalid")
        self.assertEqual(caught.exception.account_status, "login_required")
        self.assertTrue(caught.exception.confirmed_no_effect)
        self.assertEqual(caught.exception.http_status, 200)
        self.assertNotIn("expired secret text", str(caught.exception))
        self.assertEqual(len(rejected_http.calls), 1)

    def test_rejection_classification_is_small_action_scoped_and_fail_closed(self):
        cases = (
            ("liked", 21327, None, "authentication_invalid", "login_required", True),
            ("liked", 10014, None, "permission_denied", "warming", True),
            ("liked", 10023, None, "rate_limited", "cooling", True),
            ("commented", 20016, None, "rate_limited", "cooling", True),
            ("liked", 20016, None, "rate_limited", "cooling", False),
            ("reposted", 20032, None, "remote_success_possible", "cooling", False),
            ("liked", 29999, None, "platform_rejected", "cooling", False),
            ("liked", -1, 401, "authentication_invalid", "login_required", False),
            ("liked", -1, 403, "permission_denied", "warming", False),
            ("liked", -1, 429, "rate_limited", "cooling", False),
        )
        for action, code, status, category, account_status, known_no_effect in cases:
            with self.subTest(action=action, code=code, status=status):
                observed = classify_weibo_api_rejection(
                    action,
                    code,
                    http_status=status,
                )
                self.assertEqual(observed.category, category)
                self.assertEqual(observed.account_status, account_status)
                self.assertEqual(observed.confirmed_no_effect, known_no_effect)

    async def test_uncertain_remote_responses_are_single_attempt_unknown_outcomes(self):
        success_shape = {"status": {"idstr": STATUS_ID}}
        cases = (
            FakeResponse({"error_code": 20032, "error": "delayed success"}),
            FakeResponse({"error_code": 29999, "error": "new platform code"}),
            FakeResponse({"error_code": "new-code", "error": "format changed"}),
            FakeResponse({"error_code": 21327.9, "error": "float must not truncate"}),
            FakeResponse({"error_code": True, "error": "bool is not an integer code"}),
            FakeResponse({**success_shape, "error_code": 0}),
            FakeResponse({**success_shape, "error_code": False}),
            FakeResponse({**success_shape, "error_code": ""}),
            FakeResponse({**success_shape, "error_code": None}),
            FakeResponse(success_shape, status_code=302),
            FakeResponse(success_shape, status_code=400),
            FakeResponse({}, status_code=401),
            FakeResponse({}, status_code=403),
            FakeResponse({}, status_code=429),
            FakeResponse({"error_code": 21327, "error": "expired"}, status_code=503),
        )
        for index, response in enumerate(cases):
            with self.subTest(index=index, status=response.status_code):
                fake = FakeHttpClient([response])
                client = make_client(fake, actions=("liked",))
                with self.assertRaises(WeiboApiActionOutcomeUnknown):
                    await client.like(
                        STATUS_ID,
                        operation_key=f"weibo:liked:uncertain-{index}",
                    )
                self.assertEqual(len(fake.calls), 1)

    async def test_changed_comment_receipt_text_is_unknown(self):
        fake = FakeHttpClient([
            FakeResponse({
                "idstr": "2001",
                "text": "platform changed the reviewed text",
                "status": {"idstr": STATUS_ID},
            })
        ])
        client = make_client(fake, actions=("commented",))
        with patch(
            "app.weibo.client.weibo_rip_hmac", return_value="h" * 64
        ):
            with self.assertRaises(WeiboApiActionOutcomeUnknown):
                await client.comment(
                    STATUS_ID,
                    "#lottery# @friend_a @friend_b enter",
                    rip=PUBLIC_RIP,
                    operation_key="weibo:commented:text-mismatch",
                )
        self.assertEqual(len(fake.calls), 1)

        repost_http = FakeHttpClient([
            FakeResponse({
                "idstr": "2002",
                "text": "changed repost",
                "retweeted_status": {"idstr": STATUS_ID},
            })
        ])
        repost = make_client(repost_http, actions=("reposted",))
        with patch(
            "app.weibo.client.weibo_rip_hmac", return_value="h" * 64
        ):
            with self.assertRaises(WeiboApiActionOutcomeUnknown):
                await repost.repost(
                    STATUS_ID,
                    "exact repost",
                    rip=PUBLIC_RIP,
                    operation_key="weibo:reposted:text-mismatch",
                )
        self.assertEqual(len(repost_http.calls), 1)

    async def test_numeric_status_is_preflighted_with_statuses_show(self):
        fake = FakeHttpClient([FakeResponse({"idstr": STATUS_ID})])
        client = make_client(fake, actions=("liked",))
        self.assertEqual(await client.preflight_status(STATUS_ID), STATUS_ID)
        self.assertEqual(fake.calls[0][0], "GET")
        self.assertTrue(fake.calls[0][1].endswith("/statuses/show.json"))
        self.assertEqual(fake.calls[0][2]["params"]["id"], STATUS_ID)


class FakeActionClient:
    def __init__(self):
        self.calls = []

    async def _receipt(self, action, target, operation_key, text=None, rip=None):
        self.calls.append((action, target, operation_key, text, rip))
        return WeiboActionReceipt(
            action=action,
            target_id=str(target),
            remote_id=f"remote-{action}",
            operation_key=operation_key,
            request_payload_hash="f" * 64,
        )

    async def follow(self, target_uid, *, rip, operation_key):
        return await self._receipt("followed", target_uid, operation_key, rip=rip)

    async def like(self, status_id, *, operation_key):
        return await self._receipt("liked", status_id, operation_key)

    async def comment(self, status_id, text, *, rip, operation_key):
        return await self._receipt("commented", status_id, operation_key, text, rip)

    async def favorite(self, status_id, *, operation_key):
        return await self._receipt("favorited", status_id, operation_key)

    async def repost(self, status_id, text=None, *, rip, operation_key):
        return await self._receipt("reposted", status_id, operation_key, text, rip)


class OAuthExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_preserves_all_five_actions_and_rip_scope(self):
        client = FakeActionClient()
        started = []
        persisted = []

        async def before_action(action):
            started.append(action)

        async def operation_key_for(action):
            return f"intent:task-1:{action}"

        async def after_receipt(action, receipt):
            persisted.append((action, receipt.operation_key))

        result = await WeiboOAuthExecutor(
            client,
            operation_key_for=operation_key_for,
            before_action=before_action,
            after_receipt=after_receipt,
        ).execute(
            validate_action_plan_v2(oauth_plan()),
            status_id=STATUS_ID,
            follow_target_uid=FOLLOW_UID,
            rip=PUBLIC_RIP,
        )
        self.assertTrue(result.success)
        self.assertEqual(started, list(WEIBO_ACTION_ORDER))
        self.assertEqual([call[0] for call in client.calls], list(WEIBO_ACTION_ORDER))
        self.assertEqual([call[4] for call in client.calls], [PUBLIC_RIP, None, PUBLIC_RIP, None, PUBLIC_RIP])
        self.assertEqual([item[0] for item in persisted], list(WEIBO_ACTION_ORDER))

    async def test_receipt_persistence_failure_becomes_unknown(self):
        async def operation_key_for(action):
            return f"intent:task-2:{action}"

        async def fail_after_receipt(action, receipt):
            del action, receipt
            raise RuntimeError("database unavailable")

        executor = WeiboOAuthExecutor(
            FakeActionClient(),
            operation_key_for=operation_key_for,
            after_receipt=fail_after_receipt,
        )
        with self.assertRaises(WeiboExecutionOutcomeUnknown) as caught:
            await executor.execute(
                validate_action_plan_v2(oauth_plan(("liked",))),
                status_id=STATUS_ID,
            )
        self.assertEqual(caught.exception.action, "liked")

    async def test_non_follow_receipt_cannot_claim_follow_target_as_target(self):
        class WrongTargetClient(FakeActionClient):
            async def like(self, status_id, *, operation_key):
                del status_id
                return await self._receipt(
                    "liked", FOLLOW_UID, operation_key
                )

        async def operation_key_for(action):
            return f"intent:task-wrong-target:{action}"

        with self.assertRaises(WeiboExecutionOutcomeUnknown) as caught:
            await WeiboOAuthExecutor(
                WrongTargetClient(),
                operation_key_for=operation_key_for,
            ).execute(
                validate_action_plan_v2(oauth_plan(("liked",))),
                status_id=STATUS_ID,
                follow_target_uid=FOLLOW_UID,
            )
        self.assertEqual(caught.exception.action, "liked")

    async def test_friend_mentions_are_resolved_and_counted_by_uid_before_actions(self):
        validated = validate_action_plan_v2(three_friend_plan())

        class IdentityClient:
            def __init__(self, mapping):
                self.mapping = mapping
                self.calls = []

            async def resolve_user_uid(self, handle):
                self.calls.append(handle)
                return self.mapping[handle]

        unique = IdentityClient({
            "@Alice": "101",
            "@bob": "102",
            "@carol": "103",
        })
        await task_runner.preflight_weibo_friend_mentions(
            unique,
            validated,
            pre_resolved={"@host": "900"},
        )
        self.assertEqual(unique.calls, ["@Alice", "@bob", "@carol"])

        alias_collision = IdentityClient({
            "@Alice": "101",
            "@bob": "101",
            "@carol": "103",
        })
        with self.assertRaisesRegex(
            RuntimeError,
            "weibo_friend_identity_count_mismatch:commented",
        ):
            await task_runner.preflight_weibo_friend_mentions(
                alias_collision,
                validated,
                pre_resolved={"@host": "900"},
            )

    async def test_mentions_are_resolved_even_without_friend_count_constraint(self):
        raw = oauth_plan(("commented",))
        raw["source_content_requirements"]["commented"]["mentions"] = [
            "@friend_a",
            "@friend_b",
        ]
        raw["friend_mention_requirements"] = {}
        raw["plan_hash"] = compute_action_plan_hash(raw)
        validated = validate_action_plan_v2(raw)

        class IdentityClient:
            async def resolve_user_uid(self, handle):
                if handle == "@friend_b":
                    raise WeiboApiRejected("users/show", 20003, "user not found")
                return "101"

        with self.assertRaises(WeiboApiRejected):
            await task_runner.preflight_weibo_friend_mentions(
                IdentityClient(), validated
            )

    async def test_unconstrained_action_mentions_are_also_resolved(self):
        raw = oauth_plan(("commented", "reposted"))
        raw["action_payloads"]["reposted"] = {
            "text": "@missing-account",
            "topic_tags": [],
            "mentions": ["@missing-account"],
        }
        raw["content_requirements"]["reposted"] = {
            "topic_tags": [],
            "mentions": ["@missing-account"],
        }
        raw["source_content_requirements"]["reposted"] = {
            "topic_tags": [],
            "mentions": ["@missing-account"],
        }
        raw["friend_mention_requirements"] = {
            "commented": {"mode": "exact", "count": 2}
        }
        raw["plan_hash"] = compute_action_plan_hash(raw)
        validated = validate_action_plan_v2(raw)

        class IdentityClient:
            async def resolve_user_uid(self, handle):
                if handle == "@missing-account":
                    raise WeiboApiRejected("users/show", 20003, "user not found")
                return {"@friend_a": "101", "@friend_b": "102"}[handle]

        with self.assertRaises(WeiboApiRejected):
            await task_runner.preflight_weibo_friend_mentions(
                IdentityClient(), validated
            )

    async def test_preflight_rejects_more_than_plan_wide_unique_handle_limit(self):
        handles = [f"@friend_{index}" for index in range(33)]
        plan = SimpleNamespace(
            friend_mention_requirements={},
            source_content_requirements={
                "follow_targets": [],
                "commented": {"mentions": handles},
                "reposted": {"mentions": []},
            },
            content_requirements={"follow_targets": []},
            payload_for=lambda action: {
                "mentions": handles if action == "commented" else []
            },
        )
        client = SimpleNamespace(resolve_user_uid=AsyncMock())
        with self.assertRaisesRegex(
            RuntimeError, "weibo_preflight_unique_handle_limit_exceeded"
        ):
            await task_runner.preflight_weibo_friend_mentions(client, plan)
        client.resolve_user_uid.assert_not_awaited()


class QueuePrivacyTests(unittest.IsolatedAsyncioTestCase):
    def test_rip_digest_is_keyed_and_purpose_bound(self):
        master = b"k" * 32
        encoded_key = base64.b64encode(master).decode("ascii")
        derived = hmac.new(
            master, b"dpms:weibo-rip-hmac:v1", hashlib.sha256
        ).digest()
        expected = hmac.new(
            derived, PUBLIC_RIP.encode("ascii"), hashlib.sha256
        ).hexdigest()
        with patch(
            "app.weibo.credentials.settings",
            type("Settings", (), {"encryption_key": encoded_key})(),
        ):
            observed = weibo_rip_hmac(PUBLIC_RIP)
            self.assertEqual(weibo_rip_hmac(""), "")
            self.assertEqual(weibo_rip_hmac(None), "")
        self.assertEqual(observed, expected)
        self.assertNotEqual(
            observed, hashlib.sha256(PUBLIC_RIP.encode("ascii")).hexdigest()
        )

    def test_rip_must_already_be_a_canonical_public_ip(self):
        self.assertEqual(
            validate_weibo_rip(PUBLIC_RIP, required=True),
            PUBLIC_RIP,
        )
        for invalid in (
            f" {PUBLIC_RIP}",
            "2001:4860:4860:0:0:0:0:8888",
            "192.168.1.22",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WeiboOAuthCredentialError):
                    validate_weibo_rip(invalid, required=True)

    def test_credential_token_is_printable_ascii_only(self):
        envelope = {
            "credential_kind": "weibo_oauth",
            "access_token": "ascii-token_123",
            "uid": "12345678",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }
        parsed = parse_weibo_oauth_credential(envelope, now=NOW)
        self.assertNotIn(envelope["access_token"], repr(parsed))
        for invalid in ("token with space", "令牌", "token\nvalue"):
            tampered = dict(envelope, access_token=invalid)
            with self.assertRaises(WeiboOAuthCredentialError) as caught:
                parse_weibo_oauth_credential(tampered, now=NOW)
            self.assertEqual(caught.exception.code, "weibo_oauth_access_token_invalid")

    def test_credential_requires_execution_time_reserve(self):
        base = {
            "credential_kind": "weibo_oauth",
            "access_token": "ascii-token_123",
            "uid": "12345678",
        }
        expiring = dict(
            base,
            expires_at=(NOW + timedelta(seconds=899)).isoformat(),
        )
        with self.assertRaises(WeiboOAuthCredentialError) as caught:
            parse_weibo_oauth_credential(expiring, now=NOW)
        self.assertEqual(
            caught.exception.code, "weibo_oauth_credential_expiring_soon"
        )

        sufficient = dict(
            base,
            expires_at=(NOW + timedelta(seconds=900)).isoformat(),
        )
        parsed = parse_weibo_oauth_credential(sufficient, now=NOW)
        parsed.require_fresh(now=NOW, min_remaining_seconds=900)

    def test_encrypted_rip_is_strictly_decrypted_with_dedicated_aad(self):
        blob = b"n" * 12 + b"ciphertext-and-tag"
        encoded = base64.urlsafe_b64encode(blob).decode("ascii")

        class Vault:
            def decrypt_strict(self, ciphertext, *, aad):
                self.seen = (ciphertext, aad)
                return PUBLIC_RIP

        vault = Vault()
        with patch("app.weibo.credentials.cookie_vault", vault):
            self.assertEqual(decrypt_weibo_rip(encoded, required=True), PUBLIC_RIP)
        self.assertEqual(vault.seen, (blob, WEIBO_RIP_AAD))

        class PrivateVault:
            def decrypt_strict(self, ciphertext, *, aad):
                del ciphertext, aad
                return "192.168.1.22"

        with patch("app.weibo.credentials.cookie_vault", PrivateVault()):
            with self.assertRaises(WeiboOAuthCredentialError) as caught:
                decrypt_weibo_rip(encoded, required=True)
        self.assertEqual(caught.exception.code, "weibo_rip_not_public")
        self.assertNotIn("192.168.1.22", str(caught.exception))

    async def test_plaintext_rip_is_removed_before_dead_letter_serialization(self):
        raw_ip = PUBLIC_RIP
        task = {
            "task_id": "task-privacy",
            "account_id": "17",
            "lottery_id": "801",
            "platform": "weibo",
            "mode": "real_run",
            "action_plan": oauth_plan(("followed",)),
            "weibo_rip": raw_ip,
        }
        with self.assertRaises(task_runner.InvalidTaskMessage) as caught:
            task_runner.validate_task_message(task)
        self.assertEqual(str(caught.exception), "weibo_rip_plaintext_forbidden")
        self.assertNotIn(raw_ip, repr(task))

        captured = []

        class Database:
            async def execute(self, query, values):
                del query
                captured.append(values["payload"])

        class Redis:
            async def xadd(self, stream, values):
                del stream
                captured.append(values["payload"])

        malicious = dict(task, weibo_rip=raw_ip)
        with patch.object(task_runner, "database", Database()), patch.object(
            task_runner, "redis", Redis()
        ):
            await task_runner.dead_letter_message("1-0", malicious, "invalid")
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(raw_ip not in payload for payload in captured))

    async def test_invalid_plaintext_message_for_active_task_is_retained_for_recovery(self):
        task_id = str(uuid4())

        class Database:
            async def fetch_one(self, query, values):
                self.seen = (query, values)
                return {"status": "queued"}

        redis = SimpleNamespace(xack=AsyncMock(), xdel=AsyncMock())
        dead_letter = AsyncMock()
        task = {"task_id": task_id}
        with patch.object(task_runner, "database", Database()), patch.object(
            task_runner, "redis", redis
        ), patch.object(task_runner, "dead_letter_message", dead_letter):
            acknowledged = await task_runner.handle_invalid_task_message(
                "1-0", task, "weibo_rip_plaintext_forbidden"
            )

        self.assertFalse(acknowledged)
        dead_letter.assert_awaited_once_with(
            "1-0", task, "weibo_rip_plaintext_forbidden"
        )
        redis.xack.assert_not_awaited()
        redis.xdel.assert_not_awaited()

    async def test_invalid_message_without_active_authoritative_task_is_acked(self):
        class Database:
            async def fetch_one(self, query, values):
                del query, values
                return {"status": "failed"}

        redis = SimpleNamespace(xack=AsyncMock(), xdel=AsyncMock())
        with patch.object(task_runner, "database", Database()), patch.object(
            task_runner, "redis", redis
        ), patch.object(
            task_runner, "dead_letter_message", AsyncMock()
        ):
            acknowledged = await task_runner.handle_invalid_task_message(
                "2-0",
                {"task_id": str(uuid4())},
                "weibo_rip_plaintext_forbidden",
            )

        self.assertTrue(acknowledged)
        redis.xack.assert_awaited_once_with(
            task_runner.STREAM_KEY, task_runner.GROUP_NAME, "2-0"
        )
        redis.xdel.assert_awaited_once_with(task_runner.STREAM_KEY, "2-0")

    async def test_noncanonical_task_id_cannot_retain_invalid_message(self):
        database = SimpleNamespace(fetch_one=AsyncMock())
        redis = SimpleNamespace(xack=AsyncMock(), xdel=AsyncMock())
        with patch.object(task_runner, "database", database), patch.object(
            task_runner, "redis", redis
        ), patch.object(
            task_runner, "dead_letter_message", AsyncMock()
        ):
            acknowledged = await task_runner.handle_invalid_task_message(
                "3-0", {"task_id": "not-a-core-task"}, "invalid"
            )

        self.assertTrue(acknowledged)
        database.fetch_one.assert_not_awaited()
        redis.xack.assert_awaited_once()
        redis.xdel.assert_not_awaited()

    async def test_plaintext_stream_entry_is_not_acked_when_delete_fails(self):
        class Database:
            async def fetch_one(self, query, values):
                del query, values
                return {"status": "failed"}

        redis = SimpleNamespace(
            xack=AsyncMock(),
            xdel=AsyncMock(side_effect=RuntimeError("redis unavailable")),
        )
        with patch.object(task_runner, "database", Database()), patch.object(
            task_runner, "redis", redis
        ), patch.object(
            task_runner, "dead_letter_message", AsyncMock()
        ):
            with self.assertRaisesRegex(RuntimeError, "redis unavailable"):
                await task_runner.handle_invalid_task_message(
                    "4-0",
                    {"task_id": str(uuid4())},
                    "weibo_rip_plaintext_forbidden",
                )

        redis.xdel.assert_awaited_once_with(task_runner.STREAM_KEY, "4-0")
        redis.xack.assert_not_awaited()


class CalibrationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_message_claim_is_exact_transactional_cas(self):
        class Database:
            def __init__(self):
                self.transaction_active = False
                self.rolled_back = False

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

        db = Database()
        claim_calls = []

        async def affected(query, values, *, db):
            self.assertTrue(db.transaction_active)
            claim_calls.append((" ".join(query.split()), dict(values)))
            return 0

        pool = SimpleNamespace(get_account_context=AsyncMock())
        with patch.object(account_calibrator, "database", db), patch.object(
            account_calibrator, "execute_affected_rows", affected
        ), patch.object(
            account_calibrator, "record_event", AsyncMock()
        ) as event, patch.object(
            account_calibrator, "structured_log"
        ) as log:
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    "platform": "weibo",
                },
            )

        self.assertTrue(db.rolled_back)
        self.assertEqual(len(claim_calls), 1)
        query, values = claim_calls[0]
        self.assertIn("calibration_id = :calibration_id", query)
        self.assertIn("account_id = :account_id", query)
        self.assertIn("platform = :platform", query)
        self.assertIn("status = 'queued'", query)
        self.assertEqual(
            values,
            {
                "calibration_id": CALIBRATION_ID,
                "account_id": ACCOUNT_ID,
                "platform": "weibo",
            },
        )
        event.assert_not_awaited()
        pool.get_account_context.assert_not_awaited()
        self.assertEqual(
            log.call_args.args[:2],
            ("warning", "account_calibration_claim_rejected"),
        )

    async def test_persisted_credential_shape_controls_weibo_calibration_route(self):
        oauth_envelope = {
            "credential_kind": "weibo_oauth",
            "access_token": "ascii-token",
            "uid": "12345678",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }

        class Database:
            async def fetch_one(self, query, values):
                del query, values
                return {"platform": "weibo", "encrypted_credential": b"encrypted"}

        class Vault:
            def __init__(self, decrypted):
                self.decrypted = decrypted

            def decrypt(self, blob, *, aad):
                del blob, aad
                return self.decrypted

        async def resolve(task, decrypted):
            with patch.object(account_calibrator, "database", Database()), patch.object(
                account_calibrator, "cookie_vault", Vault(decrypted)
            ):
                return await account_calibrator.resolve_weibo_calibration_kind(task)

        self.assertEqual(
            await resolve(
                {"account_id": "17", "calibration_kind": "weibo_oauth_identity"},
                oauth_envelope,
            ),
            "weibo_oauth_identity",
        )
        self.assertEqual(
            await resolve({"account_id": "17"}, "SUB=browser-cookie; XSRF=abc"),
            "browser_session",
        )
        with self.assertRaisesRegex(
            ValueError, "weibo_browser_calibration_credential_kind_mismatch"
        ):
            await resolve(
                {"account_id": "17", "calibration_kind": "browser_session"},
                oauth_envelope,
            )
        with self.assertRaisesRegex(
            ValueError, "weibo_oauth_calibration_credential_kind_mismatch"
        ):
            await resolve(
                {"account_id": "17", "calibration_kind": "weibo_oauth_capability"},
                "SUB=browser-cookie; XSRF=abc",
            )

    async def test_oauth_calibration_status_and_account_settle_atomically(self):
        envelope = {
            "credential_kind": "weibo_oauth",
            "access_token": TOKEN,
            "uid": "12345678",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }

        class Database:
            def __init__(self):
                self.transaction_active = False

            @asynccontextmanager
            async def transaction(self):
                self.transaction_active = True
                try:
                    yield
                finally:
                    self.transaction_active = False

            async def fetch_one(self, query, values):
                del values
                if "SELECT c.calibration_id" in query:
                    return {
                        "calibration_id": CALIBRATION_ID,
                        "account_id": ACCOUNT_ID,
                        "platform": "weibo",
                        "calibration_status": "running",
                        "staged_result": None,
                        "account_status": "warming",
                        "execution_revision": EXECUTION_REVISION,
                        "encrypted_credential": b"encrypted",
                    }
                if "SELECT c.status AS calibration_status" in query:
                    self.assert_transaction_active()
                    return {
                        "calibration_status": "running",
                        "account_status": "warming",
                        "execution_revision": EXECUTION_REVISION,
                    }
                raise AssertionError(query)

            def assert_transaction_active(self):
                if not self.transaction_active:
                    raise AssertionError("settlement read must be transactional")

        class Vault:
            def decrypt(self, blob, *, aad):
                del blob, aad
                return envelope

        class IdentityClient:
            def __init__(self, token):
                self.token = token

            async def __aenter__(self):
                self.assert_token()
                return self

            async def __aexit__(self, *_exc):
                return None

            def assert_token(self):
                if self.token != TOKEN:
                    raise AssertionError("wrong OAuth token")

            async def check_identity(self):
                return "12345678"

        db = Database()
        affected_calls = []

        async def affected(query, values, *, db):
            del query, values
            affected_calls.append(db.transaction_active)
            return 1 if len(affected_calls) == 1 else 0

        with patch.object(account_calibrator, "database", db), patch.object(
            account_calibrator, "cookie_vault", Vault()
        ), patch.object(
            account_calibrator, "execute_affected_rows", affected
        ), patch.object(
            account_calibrator, "emit_calibration_notification", AsyncMock()
        ) as notify, patch.object(
            account_calibrator, "record_event", AsyncMock()
        ) as event:
            with self.assertRaisesRegex(
                ValueError,
                "weibo_oauth_account_settlement_lost",
            ):
                await account_calibrator.handle_weibo_oauth_calibration(
                    {
                        "calibration_id": CALIBRATION_ID,
                        "account_id": str(ACCOUNT_ID),
                    },
                    capability_calibration=False,
                    identity_client_factory=IdentityClient,
                )
        self.assertEqual(affected_calls, [True, True])
        self.assertFalse(db.transaction_active)
        notify.assert_not_awaited()
        event.assert_not_awaited()

    async def test_post_commit_delivery_failures_do_not_reverse_oauth_success(self):
        envelope = {
            "credential_kind": "weibo_oauth",
            "access_token": TOKEN,
            "uid": "12345678",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }

        class Database:
            def __init__(self):
                self.transaction_depth = 0

            @asynccontextmanager
            async def transaction(self):
                self.transaction_depth += 1
                try:
                    yield
                finally:
                    self.transaction_depth -= 1

            async def fetch_one(self, query, values):
                del values
                if "SELECT c.calibration_id" in query:
                    return {
                        "calibration_id": CALIBRATION_ID,
                        "account_id": ACCOUNT_ID,
                        "platform": "weibo",
                        "calibration_status": "running",
                        "staged_result": None,
                        "account_status": "warming",
                        "execution_revision": EXECUTION_REVISION,
                        "encrypted_credential": b"encrypted",
                    }
                if "SELECT c.status AS calibration_status" in query:
                    if self.transaction_depth <= 0:
                        raise AssertionError(
                            "settlement read must be transactional"
                        )
                    return {
                        "calibration_status": "running",
                        "account_status": "warming",
                        "execution_revision": EXECUTION_REVISION,
                    }
                raise AssertionError(query)

        class Vault:
            def decrypt(self, blob, *, aad):
                del blob, aad
                return envelope

        class IdentityClient:
            def __init__(self, token):
                self.token = token

            async def __aenter__(self):
                self.assertEqualToken()
                return self

            async def __aexit__(self, *_exc):
                return None

            def assertEqualToken(self):
                if self.token != TOKEN:
                    raise AssertionError("wrong OAuth token")

            async def check_identity(self):
                return "12345678"

        db = Database()
        affected_queries = []

        async def affected(query, values, *, db):
            del values
            self.assertGreater(db.transaction_depth, 0)
            affected_queries.append(" ".join(query.split()))
            return 1

        original_oauth_handler = (
            account_calibrator.handle_weibo_oauth_calibration
        )

        async def oauth_handler(task, *, capability_calibration):
            return await original_oauth_handler(
                task,
                capability_calibration=capability_calibration,
                identity_client_factory=IdentityClient,
            )

        notify = AsyncMock(side_effect=RuntimeError("notify unavailable"))
        event = AsyncMock(
            side_effect=[None, RuntimeError("event store unavailable")]
        )
        pool = SimpleNamespace(get_account_context=AsyncMock())
        with patch.object(account_calibrator, "database", db), patch.object(
            account_calibrator, "execute_affected_rows", affected
        ), patch.object(
            account_calibrator, "cookie_vault", Vault()
        ), patch.object(
            account_calibrator,
            "resolve_weibo_calibration_kind",
            AsyncMock(return_value="weibo_oauth_identity"),
        ), patch.object(
            account_calibrator,
            "handle_weibo_oauth_calibration",
            AsyncMock(side_effect=oauth_handler),
        ), patch.object(
            account_calibrator, "emit_calibration_notification", notify
        ), patch.object(
            account_calibrator, "record_event", event
        ), patch.object(
            account_calibrator, "structured_log"
        ) as log:
            await account_calibrator.handle_calibration(
                pool,
                {
                    "calibration_id": CALIBRATION_ID,
                    "account_id": str(ACCOUNT_ID),
                    "platform": "weibo",
                    "calibration_kind": "weibo_oauth_identity",
                },
            )

        self.assertEqual(len(affected_queries), 3)
        self.assertIn("status = 'queued'", affected_queries[0])
        self.assertIn("SET status = 'succeeded'", affected_queries[1])
        self.assertIn("UPDATE accounts", affected_queries[2])
        self.assertFalse(
            any("SET status = 'failed'" in query for query in affected_queries)
        )
        notify.assert_awaited_once()
        self.assertEqual(event.await_count, 2)
        pool.get_account_context.assert_not_awaited()
        failed_deliveries = {
            item.kwargs.get("delivery")
            for item in log.call_args_list
            if item.args[:2]
            == ("warning", "account_calibration_post_commit_delivery_failed")
        }
        self.assertEqual(
            failed_deliveries,
            {"notification", "event"},
        )


class TaskRunnerOAuthIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_oauth_gate_does_not_depend_on_browser_shadow_evidence(self):
        plan = validate_action_plan_v2(oauth_plan(("liked",)))
        canonical_url = f"canonical://weibo/status/{STATUS_ID}"
        target_hash = real_run_gate.compute_target_hash(canonical_url)
        config_hash = real_run_gate.compute_config_hash(
            {
                "execution_path_id": WEIBO_OAUTH_EXECUTION_PATH,
                "execution_revision": EXECUTION_REVISION,
                "runtime_capability_requirements": (
                    plan.runtime_capability_requirements
                ),
                "weibo_rip_hash": "",
            }
        )
        task = {
            "execution_evidence_id": CALIBRATION_ID,
            "execution_revision": EXECUTION_REVISION,
            "target_hash": target_hash,
            "config_hash": config_hash,
            "weibo_rip_encrypted": "",
        }
        row = {
            "task_execution_evidence_id": CALIBRATION_ID,
            "oauth_calibration_id": CALIBRATION_ID,
            "oauth_calibration_account_id": ACCOUNT_ID,
            "account_execution_revision": EXECUTION_REVISION,
            "oauth_calibration_platform": "weibo",
            "oauth_calibration_status": "succeeded",
            "oauth_calibration_fresh": 1,
            "account_credential_present": 1,
            "lottery_canonical_url": canonical_url,
            "task_target_hash": target_hash,
            "task_config_hash": config_hash,
            "oauth_calibration_result": {
                "identity": {
                    "verified": True,
                    "method": "weibo_account_get_uid",
                    "uid": "12345678",
                },
                "calibration_scope": "oauth_identity_and_capabilities",
                "requires_manual_identity_review": False,
                "account_status_target": "ready",
                "oauth_capabilities": attestation(actions=("liked",)),
            },
            # These old selector-evidence columns may be absent/null. OAuth
            # authorization is independently bound to account_calibrations.
            "evidence_shadow_task_id": None,
            "evidence_shadow_status": None,
            "evidence_shadow_observation": None,
        }
        evidence_id, revision, capabilities, uid = (
            real_run_gate._validate_weibo_oauth_execution_evidence(
                task,
                row,
                account_id=ACCOUNT_ID,
                plan=plan,
            )
        )
        self.assertEqual(evidence_id, CALIBRATION_ID)
        self.assertEqual(revision, EXECUTION_REVISION)
        self.assertEqual(uid, "12345678")
        self.assertEqual(capabilities["calibration_id"], CALIBRATION_ID)

    async def test_all_read_preflights_precede_intents_and_all_five_mutations(self):
        plan = validate_action_plan_v2(oauth_plan())
        sequence = []
        intents = []
        settlements = []
        saved_phases = []

        class Credential:
            access_token = TOKEN

            def require_fresh(self, **kwargs):
                self.last_freshness_budget = kwargs.get("min_remaining_seconds")
                sequence.append("credential:fresh")

        class Client:
            async def resolve_status_id(self, value):
                sequence.append("get:queryid")
                return value

            async def preflight_status(self, value):
                sequence.append("get:status_show")
                return value

            async def resolve_user_uid(self, handle):
                sequence.append(f"get:user:{handle}")
                return {
                    "@lottery_host": FOLLOW_UID,
                    "@friend_a": "101",
                    "@friend_b": "102",
                }[handle]

            async def _receipt(
                self,
                action,
                target,
                operation_key,
                *,
                payload=None,
                rip="",
            ):
                sequence.append(f"post:{action}")
                mutation = build_weibo_mutation_request(
                    action,
                    target,
                    payload=payload,
                    rip=rip,
                )
                return WeiboActionReceipt(
                    action=action,
                    target_id=str(target),
                    remote_id=f"remote-{action}",
                    operation_key=operation_key,
                    request_payload_hash=mutation.audit_spec_hash,
                )

            async def follow(self, uid, *, rip, operation_key):
                self.last = (rip, operation_key)
                return await self._receipt(
                    "followed", uid, operation_key, rip=rip
                )

            async def like(self, status, *, operation_key):
                self.last = operation_key
                return await self._receipt("liked", status, operation_key)

            async def comment(self, status, text, *, rip, operation_key):
                self.last = (text, rip, operation_key)
                return await self._receipt(
                    "commented",
                    status,
                    operation_key,
                    payload={"text": text},
                    rip=rip,
                )

            async def favorite(self, status, *, operation_key):
                self.last = operation_key
                return await self._receipt(
                    "favorited", status, operation_key
                )

            async def repost(self, status, text=None, *, rip, operation_key):
                self.last = (text, rip, operation_key)
                return await self._receipt(
                    "reposted",
                    status,
                    operation_key,
                    payload={"text": text} if text is not None else {},
                    rip=rip,
                )

            async def aclose(self):
                sequence.append("close")

        client = Client()
        gate = SimpleNamespace(
            platform="weibo",
            execution_evidence_id=CALIBRATION_ID,
            execution_revision=EXECUTION_REVISION,
            oauth_capabilities={"safe": True},
            weibo_uid="12345678",
            action_plan=plan,
        )

        async def allow_gate(*args, **kwargs):
            del args, kwargs
            return gate

        async def prepare(**kwargs):
            sequence.append(f"intent:{kwargs['action']}")
            intents.append(kwargs["payload"])
            return task_runner.StartedActionIntent(
                intent_id=f"intent-{kwargs['action']}",
                task_id=kwargs["task_id"],
                account_id=kwargs["account_id"],
                lottery_id=kwargs["lottery_id"],
                lease_id="lease-1",
                lease_generation=1,
                action=kwargs["action"],
                payload_hash="a" * 64,
                attempt_no=1,
            )

        async def settle(**kwargs):
            sequence.append(f"settle:{kwargs['intent'].action}")
            settlements.append(kwargs)

        async def save_phase(*args):
            saved_phases.append(args[-1])

        task = {
            "task_id": "weibo-real-1",
            "account_id": str(ACCOUNT_ID),
            "lottery_id": "801",
            "platform": "weibo",
            "canonical_url": f"canonical://weibo/status/{STATUS_ID}",
            "action_plan": plan.plan,
            "execution_evidence_id": CALIBRATION_ID,
            "weibo_rip_encrypted": "ciphertext",
        }
        with patch.object(task_runner, "get_latest_phase", AsyncMock(return_value=None)), patch.object(
            task_runner, "enforce_task_real_run_gate", allow_gate
        ), patch.object(
            task_runner, "load_weibo_oauth_credential", AsyncMock(return_value=Credential())
        ), patch.object(
            task_runner, "decrypt_weibo_rip", return_value=PUBLIC_RIP
        ), patch(
            "app.weibo.client.weibo_rip_hmac", return_value="h" * 64
        ), patch.object(
            task_runner, "WeiboApiClient", side_effect=lambda *args, **kwargs: client
        ), patch.object(
            task_runner, "refresh_task_lease", AsyncMock()
        ), patch.object(
            task_runner, "renew_account_operation_lease", AsyncMock()
        ), patch.object(
            task_runner, "prepare_and_start_action_intent", prepare
        ), patch.object(
            task_runner, "settle_action_intent", settle
        ), patch.object(
            task_runner, "record_event", AsyncMock(return_value="event-1")
        ), patch.object(task_runner, "save_phase", save_phase):
            await task_runner.execute_weibo_oauth_real_task(task)

        first_intent = next(i for i, value in enumerate(sequence) if value.startswith("intent:"))
        self.assertTrue(all(value.startswith(("get:", "credential:")) for value in sequence[:first_intent]))
        self.assertEqual(
            [value for value in sequence if value.startswith("post:")],
            [f"post:{action}" for action in WEIBO_ACTION_ORDER],
        )
        rip_specs = [
            item["mutation_spec"]["body"]
            for item in intents
            if "rip_hash" in item["mutation_spec"]["body"]
        ]
        self.assertEqual(
            [body["rip_hash"] for body in rip_specs],
            ["h" * 64] * 3,
        )
        self.assertTrue(all("rip" not in body for body in rip_specs))
        self.assertTrue(all(PUBLIC_RIP not in repr(item) for item in intents))
        self.assertEqual(len(settlements), 5)
        self.assertEqual(saved_phases, ["completed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
