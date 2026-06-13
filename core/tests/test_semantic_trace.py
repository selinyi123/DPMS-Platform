import unittest

from app.semantic.trace import (
    build_semantic_trace,
    consistency_checks,
    narrative_lines,
)


def full_inputs():
    intent = {
        "strategy_score": 82,
        "priority_tier": "A",
        "recommended_mode": "shadow_run",
        "top_drivers": [{"feature": "win_rate", "contribution": 0.4}],
        "probability": 0.61,
    }
    institution = {"policy_key": "real_run_gate", "active_version": 3}
    policy_decision = {
        "decision_id": "dec-1",
        "policy_version": 3,
        "outcome": "block",
        "reasons": [{"code": "recent_shadow_run", "remediation": "run a shadow first"}],
    }
    transition_lineage = {
        "policy_key": "real_run_gate",
        "active_version": 3,
        "chain": [
            {"from_version": 1, "to_version": 2, "classification": "tightening", "reason_code": "experiment_result", "requires_extra_review": 0, "created_at": "2026-06-12"},
            {"from_version": 2, "to_version": 3, "classification": "tightening", "reason_code": "risk_tightening", "requires_extra_review": 0, "created_at": "2026-06-13"},
        ],
    }
    execution = {"status": "pending", "task_runs": [{"task_id": "t1", "status": "pending"}]}
    return intent, institution, policy_decision, transition_lineage, execution


class BuildSemanticTraceTests(unittest.TestCase):
    def test_all_inputs_present_yields_complete_structure(self):
        intent, institution, policy_decision, lineage, execution = full_inputs()
        trace = build_semantic_trace(
            subject_type="lottery",
            subject_id=123,
            intent=intent,
            institution=institution,
            policy_decision=policy_decision,
            transition_lineage=lineage,
            execution=execution,
        )
        self.assertEqual(trace["subject_type"], "lottery")
        self.assertEqual(trace["subject_id"], "123")
        for key in ("intent", "institution", "policy", "transition", "execution", "narrative", "consistency_checks"):
            self.assertIn(key, trace)
        self.assertEqual(trace["transition"]["active_version"], 3)
        self.assertEqual(len(trace["transition"]["lineage"]), 2)
        self.assertTrue(trace["narrative"]["en"])
        self.assertTrue(trace["narrative"]["zh"])

    def test_missing_inputs_yield_empty_sections_without_error(self):
        trace = build_semantic_trace(subject_type="lottery", subject_id=None)
        self.assertIsNone(trace["subject_id"])
        self.assertEqual(trace["intent"], {})
        self.assertEqual(trace["institution"], {})
        self.assertEqual(trace["policy"], {})
        self.assertEqual(trace["transition"]["lineage"], [])
        self.assertEqual(trace["execution"], {})
        # narrative still covers all five layers
        self.assertEqual(len(trace["narrative"]["en"]), 5)
        self.assertEqual(len(trace["narrative"]["zh"]), 5)

    def test_bare_chain_list_accepted_as_transition_input(self):
        chain = [{"from_version": 1, "to_version": 2, "classification": "loosening", "reason_code": "manual_review"}]
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=1, transition_lineage=chain
        )
        self.assertEqual(len(trace["transition"]["lineage"]), 1)
        self.assertEqual(trace["transition"]["lineage"][0]["classification"], "loosening")


class NarrativeLinesTests(unittest.TestCase):
    def test_block_pending_mentions_outcome_and_next_action(self):
        intent, institution, policy_decision, lineage, _ = full_inputs()
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=123,
            intent=intent, institution=institution,
            policy_decision=policy_decision, transition_lineage=lineage,
            execution={"status": "pending", "task_runs": []},
        )
        text = " ".join(narrative_lines(trace, lang="en"))
        self.assertIn("block", text)
        self.assertIn("recent_shadow_run", text)
        self.assertIn("run a shadow first", text)
        self.assertIn("v3", text)
        self.assertIn("pending", text)

    def test_allow_succeeded_mentions_outcome_and_status(self):
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=9,
            institution={"policy_key": "real_run_gate", "active_version": 1},
            policy_decision={"outcome": "allow", "policy_version": 1, "next_action": "real_run", "reasons": []},
            execution={"status": "succeeded", "task_runs": [{"task_id": "t"}]},
        )
        text = " ".join(narrative_lines(trace, lang="en"))
        self.assertIn("allow", text)
        self.assertIn("real_run", text)
        self.assertIn("succeeded", text)

    def test_chinese_template_covered(self):
        intent, institution, policy_decision, lineage, execution = full_inputs()
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=123,
            intent=intent, institution=institution,
            policy_decision=policy_decision, transition_lineage=lineage, execution=execution,
        )
        zh = narrative_lines(trace, lang="zh")
        text = " ".join(zh)
        self.assertEqual(len(zh), 5)
        self.assertIn("门禁评估", text)
        self.assertIn("生效版本", text)

    def test_missing_layers_render_no_record_lines(self):
        trace = build_semantic_trace(subject_type="lottery", subject_id=1)
        en = " ".join(narrative_lines(trace, lang="en"))
        self.assertIn("no strategy/learning record", en)
        self.assertIn("no active policy", en)
        self.assertIn("no gate-evaluation decision", en)


class ConsistencyChecksTests(unittest.TestCase):
    def test_block_but_succeeded_is_flagged(self):
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=1,
            policy_decision={"outcome": "block", "policy_version": 2, "reasons": []},
            institution={"policy_key": "real_run_gate", "active_version": 2},
            execution={"status": "succeeded", "task_runs": []},
        )
        hints = consistency_checks(trace)
        self.assertTrue(any("succeeded" in h for h in hints))

    def test_decision_under_older_version_is_flagged(self):
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=1,
            policy_decision={"outcome": "allow", "policy_version": 1, "reasons": []},
            institution={"policy_key": "real_run_gate", "active_version": 3},
            execution={"status": "pending", "task_runs": []},
        )
        hints = consistency_checks(trace)
        self.assertTrue(any("v1" in h and "v3" in h for h in hints))

    def test_consistent_trace_returns_no_hints(self):
        trace = build_semantic_trace(
            subject_type="lottery", subject_id=1,
            policy_decision={"outcome": "block", "policy_version": 3, "reasons": []},
            institution={"policy_key": "real_run_gate", "active_version": 3},
            execution={"status": "pending", "task_runs": []},
        )
        self.assertEqual(consistency_checks(trace), [])


if __name__ == "__main__":
    unittest.main()
