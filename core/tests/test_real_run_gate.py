import base64
import os
import unittest

# Valid env so importing app.db / app.config never fails during collection.
os.environ.setdefault("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.governance.policy import DEFAULT_REAL_RUN_POLICY, evaluate_policy  # noqa: E402
from app.services.real_run_gate import gate_inputs  # noqa: E402


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

    def test_real_run_disabled_blocks(self):
        gate = _full_pass_gate()
        gate["real_run_enabled"] = False
        inputs = gate_inputs(gate, breaker_allowed=True)
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("global_real_run_enabled", decision["failed"])


if __name__ == "__main__":
    unittest.main()
