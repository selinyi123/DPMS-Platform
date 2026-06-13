import unittest

from app.governance.policy import diff_policies
from app.transition.engine import (
    TRANSITION_REASON_CODES,
    classify_transition,
    impact_scope,
    rollback_plan,
    validate_transition,
)


def policy(version, gates):
    return {"policy_key": "real_run_gate", "version": version, "gates": gates}


class ClassifyTransitionTests(unittest.TestCase):
    def test_added_required_gate_is_tightening(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "tightening")

    def test_added_optional_gate_is_neutral(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}, {"code": "b", "required": False}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "neutral")

    def test_removed_gate_is_loosening(self):
        v1 = policy(1, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "loosening")

    def test_required_true_to_false_is_loosening(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": False}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "loosening")

    def test_required_false_to_true_is_tightening(self):
        v1 = policy(1, [{"code": "a", "required": False}])
        v2 = policy(2, [{"code": "a", "required": True}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "tightening")

    def test_no_change_is_neutral(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}])
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "neutral")

    def test_mixed_loosening_and_tightening_is_loosening(self):
        v1 = policy(1, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        v2 = policy(2, [{"code": "b", "required": True}, {"code": "c", "required": True}])
        # 'a' removed (loosening) while 'c' added required (tightening) -> loosening wins.
        self.assertEqual(classify_transition(diff_policies(v1, v2)), "loosening")


class ValidateTransitionTests(unittest.TestCase):
    def setUp(self):
        self.v1 = policy(1, [{"code": "a", "required": True}])
        self.v2_tightening = policy(2, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        self.v2_loosening = policy(2, [{"code": "a", "required": False}])

    def test_valid_tightening_transition(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_tightening,
            reason_code="risk_tightening",
            rollback_condition="revert if false-positive rate exceeds 5%",
        )
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_version_must_increment_by_one(self):
        v3 = policy(3, [{"code": "a", "required": True}])
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=v3,
            reason_code="manual_review",
            rollback_condition="revert to v1",
        )
        self.assertFalse(valid)
        self.assertIn("version_must_increment_by_one", errors)

    def test_invalid_reason_code_rejected(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_tightening,
            reason_code="because_i_said_so",
            rollback_condition="revert to v1",
        )
        self.assertFalse(valid)
        self.assertIn("invalid_reason_code", errors)

    def test_missing_rollback_condition_rejected(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_tightening,
            reason_code="manual_review",
            rollback_condition="   ",
        )
        self.assertFalse(valid)
        self.assertIn("rollback_condition_required", errors)

    def test_loosening_without_justification_rejected(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_loosening,
            reason_code="manual_review",
            rollback_condition="revert to v1 if abuse increases",
        )
        self.assertFalse(valid)
        self.assertIn("loosening_justification_required", errors)

    def test_loosening_with_justification_accepted(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_loosening,
            reason_code="manual_review",
            rollback_condition="revert to v1 if abuse increases",
            loosening_justification="gate 'a' duplicated by new adapter-level check",
        )
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_loosening_cannot_use_scheduled_review(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_loosening,
            reason_code="scheduled_review",
            rollback_condition="revert to v1 if abuse increases",
            loosening_justification="gate 'a' duplicated by new adapter-level check",
        )
        self.assertFalse(valid)
        self.assertIn("loosening_cannot_use_scheduled_review", errors)

    def test_tightening_does_not_require_loosening_justification(self):
        valid, errors = validate_transition(
            from_policy=self.v1,
            to_policy=self.v2_tightening,
            reason_code="experiment_result",
            rollback_condition="revert if branch B underperforms",
            loosening_justification=None,
        )
        self.assertTrue(valid)
        self.assertEqual(errors, [])


class ImpactScopeTests(unittest.TestCase):
    def test_known_policy_key_returns_subject_type(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        scope = impact_scope("real_run_gate", diff_policies(v1, v2))
        self.assertEqual(scope["subject_type"], "lottery")
        self.assertIn("b", scope["affected_gates"])
        self.assertTrue(scope["description"])

    def test_unknown_policy_key_returns_empty_scope_not_error(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}, {"code": "b", "required": True}])
        scope = impact_scope("some_other_policy", diff_policies(v1, v2))
        self.assertIsNone(scope["subject_type"])
        self.assertEqual(scope["description"], "")

    def test_no_affected_gates_yields_empty_description_for_known_policy(self):
        v1 = policy(1, [{"code": "a", "required": True}])
        v2 = policy(2, [{"code": "a", "required": True}])
        scope = impact_scope("real_run_gate", diff_policies(v1, v2))
        self.assertEqual(scope["affected_gates"], [])
        self.assertIn("No gate changes", scope["description"])


class RollbackPlanTests(unittest.TestCase):
    def test_target_version_is_from_version(self):
        record = {
            "policy_key": "real_run_gate",
            "from_version": 2,
            "to_version": 3,
            "rollback_condition": "revert if shadow-run success rate drops",
        }
        plan = rollback_plan(record)
        self.assertEqual(plan["target_version"], 2)
        self.assertEqual(plan["policy_key"], "real_run_gate")
        self.assertEqual(plan["rollback_condition"], record["rollback_condition"])
        self.assertIn(plan["reason_code"], TRANSITION_REASON_CODES)


if __name__ == "__main__":
    unittest.main()
