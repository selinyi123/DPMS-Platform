import base64
import json
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.governance.policy import (  # noqa: E402
    DEFAULT_REAL_RUN_POLICY,
    build_decision_record,
    evaluate_policy,
)
from app.services import real_run_gate  # noqa: E402
from app.services.real_run_gate import failed_gate_codes, gate_inputs  # noqa: E402


def _full_pass_gate():
    return {
        "real_run_enabled": True,
        "target_valid": True,
        "action_plan_ready": True,
        "safe_accounts": 2,
        "adapter_enabled": True,
        "probe_ready": True,
        "shadow_ready": True,
        "blockers": [],
    }


class GateInputContractTests(unittest.TestCase):
    def test_inputs_cover_every_policy_gate(self):
        """Every gate code in the active policy must have an input producer.

        A fail-closed gate that never receives an input would block forever, so
        gate_inputs() must emit a key for each policy gate code. This is the
        load-bearing invariant of the unified real-run authority (P1-4).
        """
        produced = set(gate_inputs(_full_pass_gate(), breaker_allowed=True).keys())
        policy_codes = {g["code"] for g in DEFAULT_REAL_RUN_POLICY["gates"]}
        self.assertEqual(produced, policy_codes)

    def test_full_pass_allows(self):
        inputs = gate_inputs(_full_pass_gate(), breaker_allowed=True)
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "allow")
        self.assertEqual(decision["failed"], [])

    def test_breaker_open_blocks(self):
        inputs = gate_inputs(_full_pass_gate(), breaker_allowed=False)
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("circuit_breaker_closed", decision["failed"])

    def test_no_safe_account_blocks(self):
        gate = _full_pass_gate()
        gate["safe_accounts"] = 0
        inputs = gate_inputs(gate, breaker_allowed=True)
        self.assertFalse(inputs["calibrated_account_available"])
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("calibrated_account_available", decision["failed"])

    def test_recent_account_risk_blocks(self):
        gate = _full_pass_gate()
        gate["blockers"] = ["recent_account_risk_event"]
        inputs = gate_inputs(gate, breaker_allowed=True)
        self.assertFalse(inputs["no_recent_account_risk"])
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("no_recent_account_risk", decision["failed"])

    def test_missing_probe_or_shadow_blocks(self):
        for missing in ("probe_ready", "shadow_ready", "adapter_enabled", "target_valid"):
            gate = _full_pass_gate()
            gate[missing] = False
            inputs = gate_inputs(gate, breaker_allowed=True)
            decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
            self.assertEqual(decision["outcome"], "block", f"{missing} should block")

    def test_oauth_exact_dry_preflight_satisfies_legacy_shadow_gate(self):
        gate = _full_pass_gate()
        gate["shadow_ready"] = False
        gate["execution_preflight_ready"] = True

        inputs = gate_inputs(gate, breaker_allowed=True)
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)

        self.assertTrue(inputs["recent_shadow_run"])
        self.assertEqual(decision["outcome"], "allow")

    def test_real_run_disabled_blocks(self):
        gate = _full_pass_gate()
        gate["real_run_enabled"] = False
        inputs = gate_inputs(gate, breaker_allowed=True)
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("global_real_run_enabled", decision["failed"])

    def test_action_plan_blockers_override_inconsistent_ready_flag(self):
        blockers = (
            "lottery_action_plan_required",
            "lottery_rule_text_required",
            "lottery_rule_review_required",
            "lottery_required_actions_missing",
            "lottery_action_plan_stale",
            "lottery_action_plan_future_blocker",
        )
        for blocker in blockers:
            with self.subTest(blocker=blocker):
                gate = _full_pass_gate()
                gate["blockers"] = [blocker]

                inputs = gate_inputs(gate, breaker_allowed=True)
                decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)

                self.assertFalse(inputs["action_plan_reviewed"])
                self.assertEqual(decision["outcome"], "block")
                self.assertIn("action_plan_reviewed", decision["failed"])

    def test_permissive_active_policy_cannot_delete_probe_and_shadow_gates(self):
        permissive_policy = {
            "policy_key": "real_run_gate",
            "version": 99,
            "gates": [
                {
                    "code": "global_real_run_enabled",
                    "required": True,
                    "remediation": "enable_real_run",
                }
            ],
        }
        gate = _full_pass_gate()
        gate["probe_ready"] = False
        gate["shadow_ready"] = False
        inputs = gate_inputs(gate, breaker_allowed=True)

        record = build_decision_record(
            policy=permissive_policy,
            inputs=inputs,
            subject_type="lottery",
            subject_id="1",
        )

        self.assertEqual("block", record["outcome"])
        self.assertEqual(
            ["recent_complete_probe", "recent_shadow_run"],
            failed_gate_codes(record),
        )

    def test_permissive_active_policy_cannot_relax_breaker_or_account_risk(self):
        permissive_policy = {
            "policy_key": "real_run_gate",
            "version": 100,
            "gates": [
                {"code": "circuit_breaker_closed", "required": False},
                {"code": "no_recent_account_risk", "required": False},
            ],
        }
        gate = _full_pass_gate()
        gate["blockers"] = ["recent_account_risk_event"]
        inputs = gate_inputs(gate, breaker_allowed=False)

        record = build_decision_record(
            policy=permissive_policy,
            inputs=inputs,
            subject_type="lottery",
            subject_id="1",
        )

        self.assertEqual("block", record["outcome"])
        self.assertEqual(
            ["circuit_breaker_closed", "no_recent_account_risk"],
            failed_gate_codes(record),
        )


class ActivePolicyLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def load_with_row(self, row):
        class FakeDatabase:
            async def fetch_one(self, query, values=None):
                return row

        original_database = real_run_gate.database
        real_run_gate.database = FakeDatabase()
        try:
            return await real_run_gate.load_active_policy()
        finally:
            real_run_gate.database = original_database

    async def test_missing_active_row_uses_built_in_default(self):
        policy = await self.load_with_row(None)
        self.assertIs(policy, DEFAULT_REAL_RUN_POLICY)

    async def test_malformed_active_definition_never_falls_back_to_default(self):
        malformed_definitions = (None, "", "{not-json", "[]", b"\xff{}")
        for definition in malformed_definitions:
            with self.subTest(definition=definition):
                with self.assertRaisesRegex(RuntimeError, "active_policy_definition_invalid"):
                    await self.load_with_row({"definition": definition})

    async def test_structurally_invalid_active_definition_fails_closed(self):
        definition = {
            "policy_key": "real_run_gate",
            "version": 2,
            "gates": [],
        }
        with self.assertRaisesRegex(RuntimeError, "active_policy_definition_invalid"):
            await self.load_with_row({"definition": json.dumps(definition)})

    async def test_valid_active_definition_is_returned(self):
        definition = {
            **DEFAULT_REAL_RUN_POLICY,
            "version": 2,
            "gates": [dict(gate) for gate in DEFAULT_REAL_RUN_POLICY["gates"]],
        }
        policy = await self.load_with_row({"definition": json.dumps(definition)})
        self.assertEqual(policy, definition)


class FailedGateCodesTests(unittest.TestCase):
    """``evaluate_real_run_decision`` derives ``failed_gates`` from a decision
    record, which carries ``reasons`` but no flat ``failed`` list. Reading the
    wrong key 500s every real-run dispatch, so pin the contract here.
    """

    def test_record_has_reasons_not_failed_key(self):
        gate = _full_pass_gate()
        gate["real_run_enabled"] = False
        inputs = gate_inputs(gate, breaker_allowed=True)
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs, subject_type="lottery", subject_id="1"
        )
        self.assertNotIn("failed", record)
        self.assertIn("reasons", record)

    def test_codes_match_evaluate_policy_failed(self):
        gate = _full_pass_gate()
        gate["real_run_enabled"] = False
        gate["safe_accounts"] = 0
        inputs = gate_inputs(gate, breaker_allowed=False)
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs, subject_type="lottery", subject_id="1"
        )
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(failed_gate_codes(record), decision["failed"])
        self.assertIn("global_real_run_enabled", failed_gate_codes(record))

    def test_allow_record_has_no_failed_gates(self):
        inputs = gate_inputs(_full_pass_gate(), breaker_allowed=True)
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs, subject_type="lottery", subject_id="1"
        )
        self.assertEqual(failed_gate_codes(record), [])

    def test_empty_and_malformed_reasons_are_safe(self):
        self.assertEqual(failed_gate_codes({}), [])
        self.assertEqual(failed_gate_codes({"reasons": None}), [])
        self.assertEqual(failed_gate_codes({"reasons": [{"remediation": "x"}]}), [])


if __name__ == "__main__":
    unittest.main()
