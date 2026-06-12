import unittest

from app.governance.policy import (
    DEFAULT_REAL_RUN_POLICY,
    build_decision_record,
    default_policies,
    diff_policies,
    evaluate_policy,
    replay_decision,
    validate_policy,
)

ALL_GATES_PASS = {gate["code"]: True for gate in DEFAULT_REAL_RUN_POLICY["gates"]}


class ValidatePolicyTests(unittest.TestCase):
    def test_default_policy_is_valid(self):
        valid, errors = validate_policy(DEFAULT_REAL_RUN_POLICY)
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_missing_key_rejected(self):
        valid, errors = validate_policy({"version": 1, "gates": [{"code": "g"}]})
        self.assertFalse(valid)
        self.assertIn("policy_key_required", errors)

    def test_bad_version_rejected(self):
        valid, errors = validate_policy({"policy_key": "p", "version": 0, "gates": [{"code": "g"}]})
        self.assertFalse(valid)
        self.assertIn("version_must_be_positive_int", errors)

    def test_duplicate_gate_codes_rejected(self):
        valid, errors = validate_policy({"policy_key": "p", "version": 1, "gates": [{"code": "g"}, {"code": "g"}]})
        self.assertFalse(valid)
        self.assertIn("gate_codes_must_be_unique", errors)

    def test_no_gates_rejected(self):
        valid, errors = validate_policy({"policy_key": "p", "version": 1, "gates": []})
        self.assertFalse(valid)
        self.assertIn("at_least_one_gate_required", errors)


class EvaluatePolicyTests(unittest.TestCase):
    def test_all_gates_pass_allows(self):
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=ALL_GATES_PASS)
        self.assertEqual(decision["outcome"], "allow")
        self.assertEqual(decision["failed"], [])
        self.assertEqual(decision["next_action"], "real_run")

    def test_fail_closed_when_input_missing(self):
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs={})
        self.assertEqual(decision["outcome"], "block")
        self.assertTrue(decision["failed"])

    def test_single_failed_gate_blocks(self):
        inputs = {**ALL_GATES_PASS, "recent_shadow_run": False}
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["outcome"], "block")
        self.assertIn("recent_shadow_run", decision["failed"])

    def test_next_action_is_first_failed_remediation(self):
        inputs = {**ALL_GATES_PASS, "global_real_run_enabled": False}
        decision = evaluate_policy(policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs)
        self.assertEqual(decision["next_action"], "enable_real_run")

    def test_default_policies_keyed_by_policy_key(self):
        policies = default_policies()
        self.assertIn("real_run_gate", policies)


class ReplayTests(unittest.TestCase):
    def test_replay_matches_recorded_allow(self):
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=ALL_GATES_PASS, subject_type="lottery", subject_id="42"
        )
        replay = replay_decision(policy=DEFAULT_REAL_RUN_POLICY, record=record)
        self.assertTrue(replay["matches"])
        self.assertTrue(replay["version_matches"])

    def test_replay_matches_recorded_block(self):
        inputs = {**ALL_GATES_PASS, "no_recent_account_risk": False}
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=inputs, subject_type="lottery", subject_id="7"
        )
        self.assertEqual(record["outcome"], "block")
        replay = replay_decision(policy=DEFAULT_REAL_RUN_POLICY, record=record)
        self.assertTrue(replay["matches"])

    def test_record_preserves_inputs(self):
        record = build_decision_record(
            policy=DEFAULT_REAL_RUN_POLICY, inputs=ALL_GATES_PASS, subject_type="lottery", subject_id="1"
        )
        self.assertEqual(record["inputs"], ALL_GATES_PASS)
        self.assertEqual(record["subject_id"], "1")


class DiffTests(unittest.TestCase):
    def test_added_and_removed_gates(self):
        v1 = {"policy_key": "p", "version": 1, "gates": [{"code": "a"}, {"code": "b"}]}
        v2 = {"policy_key": "p", "version": 2, "gates": [{"code": "b"}, {"code": "c"}]}
        diff = diff_policies(v1, v2)
        self.assertEqual(diff["added_gates"], ["c"])
        self.assertEqual(diff["removed_gates"], ["a"])

    def test_changed_required_flag(self):
        v1 = {"policy_key": "p", "version": 1, "gates": [{"code": "a", "required": True}]}
        v2 = {"policy_key": "p", "version": 2, "gates": [{"code": "a", "required": False}]}
        diff = diff_policies(v1, v2)
        self.assertEqual(diff["changed_required"], ["a"])


if __name__ == "__main__":
    unittest.main()
