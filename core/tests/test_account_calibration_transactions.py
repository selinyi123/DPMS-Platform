"""Structural guards for calibration producer transaction boundaries."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.accounts import (  # noqa: E402
    MAX_SUPERSEDED_LOGIN_SESSIONS,
    account_has_deletion_blocking_real_action_state,
    account_has_frozen_real_action_state,
    delete_account,
    queue_account_calibration,
    supersede_active_login_sessions,
    update_credential,
)
from app.models.schemas import AccountCredentialUpdate  # noqa: E402


CORE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CORE_ROOT.parent
WORKER_ROOT = (
    REPO_ROOT / "worker"
    if (REPO_ROOT / "worker").exists()
    else Path("/worker")
)


def function_node(path: Path, name: str) -> ast.AsyncFunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function {name} in {path}")


def call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def transaction_scoped_calls(function: ast.AsyncFunctionDef) -> list[str]:
    calls = []
    for node in ast.walk(function):
        if not isinstance(node, ast.AsyncWith):
            continue
        transaction_context = any(
            isinstance(item.context_expr, ast.Call)
            and call_name(item.context_expr) == "transaction"
            for item in node.items
        )
        if transaction_context:
            calls.extend(
                call_name(child)
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
            )
    return calls


class CalibrationTransactionStructureTests(unittest.TestCase):
    def test_all_core_account_mutations_enqueue_inside_outer_transaction(self):
        path = CORE_ROOT / "app" / "api" / "accounts.py"
        for function_name in (
            "create_account",
            "update_credential",
            "calibrate_account",
            "finalize_bilibili_qr_login",
        ):
            with self.subTest(function=function_name):
                function = function_node(path, function_name)
                all_queue_calls = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and call_name(node) == "queue_account_calibration"
                ]
                self.assertTrue(all_queue_calls)
                self.assertIn(
                    "queue_account_calibration",
                    transaction_scoped_calls(function),
                )

    def test_worker_qr_account_and_calibration_share_outer_transaction(self):
        path = WORKER_ROOT / "app" / "login_broker.py"
        function = function_node(path, "create_account_from_cookies")
        calls = transaction_scoped_calls(function)
        self.assertIn("execute", calls)
        self.assertIn("queue_account_calibration", calls)

    def test_producers_never_directly_xadd_calibration_requests(self):
        for path in (
            CORE_ROOT / "app" / "api" / "accounts.py",
            WORKER_ROOT / "app" / "login_broker.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "xadd"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).startswith(
                        "account_calibration_requests"
                    )
                ):
                    self.fail(f"direct calibration Redis write in {path}")

    def test_browser_login_session_and_outbox_share_transaction(self):
        path = CORE_ROOT / "app" / "api" / "accounts.py"
        function = function_node(path, "start_qr_login")
        scoped_calls = transaction_scoped_calls(function)
        self.assertIn("enqueue_login_request_outbox", scoped_calls)
        self.assertIn("supersede_active_login_sessions", scoped_calls)
        self.assertIn("audit_event", scoped_calls)
        self.assertIn("record_event", scoped_calls)
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "xadd"
            ):
                self.fail("browser login producer bypasses its DB Outbox")

    def test_credential_create_and_delete_audits_share_mutation_transaction(self):
        path = CORE_ROOT / "app" / "api" / "accounts.py"
        for function_name in ("create_account", "delete_account"):
            with self.subTest(function=function_name):
                calls = transaction_scoped_calls(
                    function_node(path, function_name)
                )
                self.assertIn("audit_event", calls)
                self.assertIn("record_event", calls)

    def test_credential_update_checks_remote_subject_freeze_inside_transaction(self):
        path = CORE_ROOT / "app" / "api" / "accounts.py"
        function = function_node(path, "update_credential")
        calls = transaction_scoped_calls(function)
        self.assertIn("account_remote_subject", calls)
        self.assertIn("account_has_frozen_real_action_state", calls)

    def test_account_delete_checks_repair_state_inside_transaction(self):
        path = CORE_ROOT / "app" / "api" / "accounts.py"
        function = function_node(path, "delete_account")
        calls = transaction_scoped_calls(function)
        self.assertIn(
            "account_has_deletion_blocking_real_action_state",
            calls,
        )
        self.assertIn("enqueue_account_profile_cleanup", calls)


class CalibrationOutboxAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_login_session_retires_and_cleans_hidden_sessions(self):
        database = AsyncMock()
        database.fetch_all.return_value = [
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "platform": "weibo",
                "status": "waiting_scan",
            },
            {
                "session_id": "22222222-2222-4222-8222-222222222222",
                "platform": "xiaohongshu",
                "status": "queued",
            },
        ]
        cleanup = AsyncMock()
        event = AsyncMock()
        replacement = "33333333-3333-4333-8333-333333333333"

        with (
            patch("app.api.accounts.database", database),
            patch(
                "app.api.accounts.enqueue_login_profile_cleanup",
                new=cleanup,
            ),
            patch("app.api.accounts.record_event", new=event),
        ):
            count = await supersede_active_login_sessions(
                replacement_session_id=replacement,
                actor_id="operator-1",
            )

        self.assertEqual(count, 2)
        self.assertEqual(database.execute.await_count, 2)
        for call in database.execute.await_args_list:
            self.assertIn("status = 'expired'", call.args[0])
            self.assertIn("Superseded by a newer login request", call.args[0])
            self.assertIn(
                call.args[1]["expected_status"],
                {"queued", "waiting_scan"},
            )
        self.assertEqual(cleanup.await_count, 2)
        self.assertEqual(event.await_count, 2)
        self.assertEqual(
            event.await_args_list[0].kwargs["correlation_id"],
            replacement,
        )

    async def test_login_supersession_fails_closed_when_bound_is_exceeded(self):
        database = AsyncMock()
        database.fetch_all.return_value = [
            {
                "session_id": f"{index:08d}-0000-4000-8000-000000000000",
                "platform": "weibo",
                "status": "queued",
            }
            for index in range(MAX_SUPERSEDED_LOGIN_SESSIONS + 1)
        ]

        with patch("app.api.accounts.database", database):
            with self.assertRaises(HTTPException) as raised:
                await supersede_active_login_sessions(
                    replacement_session_id=(
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    actor_id="operator-1",
                )

        self.assertEqual(raised.exception.status_code, 503)
        database.execute.assert_not_awaited()

    async def test_subject_guard_uses_confirmed_and_uncertain_authority(self):
        database = AsyncMock()
        database.fetch_one.side_effect = (
            {"has_unsettled_state": 0},
            {"has_frozen_state": 1},
        )
        with patch("app.api.accounts.database", database):
            self.assertTrue(
                await account_has_frozen_real_action_state(7)
            )
        unsettled_query = database.fetch_one.await_args_list[0].args[0]
        confirmed_query = database.fetch_one.await_args_list[1].args[0]
        self.assertIn("tr.status IN ('queued', 'running')", unsettled_query)
        self.assertIn("tr.reconciliation_required = 1", unsettled_query)
        self.assertIn(
            "eai.status IN ('started', 'unknown')",
            unsettled_query,
        )
        self.assertIn("eai.effect_certainty = 'unknown'", unsettled_query)
        self.assertIn("lottery_execution_intent_heads", confirmed_query)
        self.assertIn("external_action_intents", confirmed_query)
        self.assertIn("bilibili_action_ledger", confirmed_query)
        self.assertIn("task_phases", confirmed_query)
        self.assertIn("TaskPhaseCompleted", confirmed_query)

    async def test_delete_guard_blocks_partial_current_intent_but_not_complete(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"has_unsettled_state": 0}
        database.fetch_all.return_value = [
            {
                "lottery_id": 11,
                "platform": "weibo",
                "full_required_actions": json.dumps(["followed", "reposted"]),
            }
        ]
        loader = AsyncMock(
            return_value={
                11: SimpleNamespace(
                    blockers=(),
                    completed_actions=("followed",),
                )
            }
        )
        with (
            patch("app.api.accounts.database", database),
            patch(
                "app.services.execution_intents.coerce_frozen_execution_intent",
                return_value=SimpleNamespace(
                    lottery_id=11,
                    platform="weibo",
                    source_account_id=7,
                    full_required_actions=("followed", "reposted"),
                ),
            ),
            patch(
                "app.api.lotteries.load_real_run_completion_authorities_for_lotteries",
                new=loader,
            ),
        ):
            self.assertTrue(
                await account_has_deletion_blocking_real_action_state(7)
            )
            loader.return_value = {
                11: SimpleNamespace(
                    blockers=(),
                    completed_actions=("followed", "reposted"),
                )
            }
            self.assertFalse(
                await account_has_deletion_blocking_real_action_state(7)
            )
        self.assertEqual(loader.await_args_list[0].args[0], {11: "weibo"})
        self.assertNotIn(
            "FOR UPDATE",
            database.fetch_all.await_args_list[0].args[0],
        )

    async def test_delete_guard_fails_closed_on_corrupt_frozen_intent(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"has_unsettled_state": 0}
        database.fetch_all.return_value = [
            {
                "lottery_id": 11,
                "platform": "weibo",
                "full_required_actions": json.dumps(
                    ["followed", "reposted"]
                ),
            }
        ]
        with patch("app.api.accounts.database", database):
            self.assertTrue(
                await account_has_deletion_blocking_real_action_state(7)
            )

    async def test_delete_endpoint_preserves_credential_when_guard_blocks(self):
        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        database = AsyncMock()
        database.transaction = lambda: Transaction()
        locked = {
            "id": 7,
            "platform": "weibo",
            "status": "frozen",
            "encrypted_credential": b"still-required",
            "deleted_at": None,
        }
        with (
            patch(
                "app.api.accounts.require_min_role",
                return_value={"actor_id": "admin-1"},
            ),
            patch("app.api.accounts.require_confirmation"),
            patch(
                "app.api.accounts.lock_account_for_execution_contract_mutation",
                new=AsyncMock(return_value=locked),
            ),
            patch(
                "app.api.accounts.account_has_deletion_blocking_real_action_state",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.api.accounts.execute_locked_account_update",
                new=AsyncMock(),
            ) as update,
            patch("app.api.accounts.database", database),
        ):
            with self.assertRaises(HTTPException) as raised:
                await delete_account(7, object())

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(
            "real_action_state_requires_account_credential",
            raised.exception.detail["code"],
        )
        update.assert_not_awaited()

    async def test_credential_subject_change_after_confirmed_action_is_blocked(
        self,
    ):
        old_expired = json.dumps(
            {
                "credential_kind": "weibo_oauth",
                "access_token": "old-placeholder-token",
                "uid": "1001",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        )
        replacement = json.dumps(
            {
                "credential_kind": "weibo_oauth",
                "access_token": "new-placeholder-token",
                "uid": "2002",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        database = AsyncMock()
        database.transaction = lambda: Transaction()
        database.fetch_one.return_value = {
            "platform": "weibo",
            "status": "ready",
        }
        locked = {
            "id": 7,
            "platform": "weibo",
            "status": "ready",
            "encrypted_credential": b"old-encrypted",
            "deleted_at": None,
        }
        with (
            patch(
                "app.api.accounts.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.accounts.normalize_and_validate_credential",
                return_value=replacement,
            ),
            patch(
                "app.api.accounts.cookie_vault.encrypt",
                return_value=b"new-encrypted",
            ),
            patch(
                "app.api.accounts.cookie_vault.decrypt_strict",
                return_value=old_expired,
            ),
            patch(
                "app.api.accounts.lock_account_for_execution_contract_mutation",
                new=AsyncMock(return_value=locked),
            ),
            patch(
                "app.api.accounts.account_has_frozen_real_action_state",
                new=AsyncMock(return_value=True),
            ),
            patch("app.api.accounts.database", database),
        ):
            with self.assertRaises(HTTPException) as raised:
                await update_credential(
                    7,
                    AccountCredentialUpdate(
                        encrypted_credential="replacement"
                    ),
                    object(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "confirmed_real_actions_freeze_account_subject",
        )
        database.execute.assert_not_awaited()

    async def test_unprovable_subjects_fail_closed_for_confirmed_actions(
        self,
    ):
        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        database = AsyncMock()
        database.transaction = lambda: Transaction()
        database.fetch_one.return_value = {
            "platform": "bilibili",
            "status": "ready",
        }
        locked = {
            "id": 7,
            "platform": "bilibili",
            "status": "ready",
            "encrypted_credential": b"old-encrypted",
            "deleted_at": None,
        }
        with (
            patch(
                "app.api.accounts.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.accounts.normalize_and_validate_credential",
                return_value="new-credential",
            ),
            patch(
                "app.api.accounts.cookie_vault.encrypt",
                return_value=b"new-encrypted",
            ),
            patch(
                "app.api.accounts.cookie_vault.decrypt",
                return_value="old-credential",
            ),
            patch(
                "app.api.accounts.lock_account_for_execution_contract_mutation",
                new=AsyncMock(return_value=locked),
            ),
            patch(
                "app.api.accounts.account_remote_subject",
                side_effect=(None, None),
            ),
            patch(
                "app.api.accounts.account_has_frozen_real_action_state",
                new=AsyncMock(return_value=True),
            ),
            patch("app.api.accounts.database", database),
        ):
            with self.assertRaises(HTTPException) as raised:
                await update_credential(
                    7,
                    AccountCredentialUpdate(
                        encrypted_credential="replacement"
                    ),
                    object(),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "confirmed_real_actions_freeze_account_subject",
        )
        database.execute.assert_not_awaited()

    async def test_token_rotation_for_same_remote_subject_remains_allowed(
        self,
    ):
        old_expired = json.dumps(
            {
                "credential_kind": "weibo_oauth",
                "access_token": "old-placeholder-token",
                "uid": "1001",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        )
        replacement = json.dumps(
            {
                "credential_kind": "weibo_oauth",
                "access_token": "new-placeholder-token",
                "uid": "1001",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        database = AsyncMock()
        database.transaction = lambda: Transaction()
        database.fetch_one.return_value = {
            "platform": "weibo",
            "status": "ready",
        }
        locked = {
            "id": 7,
            "platform": "weibo",
            "status": "ready",
            "encrypted_credential": b"old-encrypted",
            "deleted_at": None,
        }
        calibration = {"calibration_id": "calibration-1"}
        with (
            patch(
                "app.api.accounts.require_min_role",
                return_value={"actor_id": "operator-1"},
            ),
            patch(
                "app.api.accounts.normalize_and_validate_credential",
                return_value=replacement,
            ),
            patch(
                "app.api.accounts.cookie_vault.encrypt",
                return_value=b"new-encrypted",
            ),
            patch(
                "app.api.accounts.cookie_vault.decrypt_strict",
                return_value=old_expired,
            ),
            patch(
                "app.api.accounts.lock_account_for_execution_contract_mutation",
                new=AsyncMock(return_value=locked),
            ),
            patch(
                "app.api.accounts.account_has_frozen_real_action_state",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.api.accounts.execute_locked_account_update",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "app.api.accounts.queue_account_calibration",
                new=AsyncMock(return_value=calibration),
            ),
            patch(
                "app.api.accounts.audit_event",
                new=AsyncMock(),
            ),
            patch(
                "app.api.accounts.record_event",
                new=AsyncMock(),
            ),
            patch("app.api.accounts.database", database),
        ):
            result = await update_credential(
                7,
                AccountCredentialUpdate(
                    encrypted_credential="replacement"
                ),
                object(),
            )

        self.assertEqual(result["status"], "credential_updated")
        self.assertEqual(result["calibration"], calibration)

    async def test_outbox_insert_failure_rolls_back_calibration_insert(self):
        class Transaction:
            def __init__(self, database):
                self.database = database

            async def __aenter__(self):
                self.database.depth += 1
                return self

            async def __aexit__(self, exc_type, *_args):
                self.database.depth -= 1
                self.database.rolled_back = exc_type is not None
                return False

        class Database:
            def __init__(self):
                self.depth = 0
                self.rolled_back = False
                self.execute = AsyncMock()
                self.fetch_one = AsyncMock(
                    return_value={"encrypted_credential": b"oauth"}
                )

            def transaction(self):
                return Transaction(self)

        database = Database()
        with (
            patch("app.api.accounts.database", database),
            patch(
                "app.api.accounts.account_credential_kind",
                return_value="browser_session",
            ),
            patch(
                "app.api.accounts.enqueue_account_calibration_outbox",
                new=AsyncMock(side_effect=RuntimeError("outbox insert failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox insert failed"):
                await queue_account_calibration(7, "bilibili")

        self.assertTrue(database.rolled_back)
        self.assertEqual(database.depth, 0)
        self.assertEqual(database.execute.await_count, 1)


if __name__ == "__main__":
    unittest.main()
