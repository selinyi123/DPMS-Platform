import unittest

from app.experiment.engine import (
    SAFE_EXPERIMENT_MODES,
    assign_branch,
    branch_stats,
    compare_branches,
    experiment_guardrails,
    should_stop_branch,
    validate_experiment_spec,
)

PLATFORMS = {"bilibili", "weibo", "xiaohongshu", "douyin"}
TWO_BRANCHES = [{"key": "a", "weight": 1}, {"key": "b", "weight": 1}]


class ValidateExperimentSpecTests(unittest.TestCase):
    def _validate(self, **overrides):
        base = dict(
            name="comment timing",
            platform="bilibili",
            mode="shadow_run",
            branches=TWO_BRANCHES,
            supported_platforms=PLATFORMS,
        )
        base.update(overrides)
        return validate_experiment_spec(**base)

    def test_valid_shadow_experiment(self):
        valid, errors = self._validate()
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_blank_name_rejected(self):
        valid, errors = self._validate(name="   ")
        self.assertFalse(valid)
        self.assertIn("experiment_name_required", errors)

    def test_unsupported_platform_rejected(self):
        valid, errors = self._validate(platform="tiktok")
        self.assertFalse(valid)
        self.assertIn("unsupported_platform", errors)

    def test_real_run_requires_admin_gate(self):
        valid, errors = self._validate(mode="real_run")
        self.assertFalse(valid)
        self.assertIn("real_run_experiment_requires_admin_gate", errors)

    def test_real_run_allowed_only_with_explicit_flag(self):
        valid, errors = self._validate(mode="real_run", allow_real_run=True)
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_unknown_mode_rejected(self):
        valid, errors = self._validate(mode="turbo_run")
        self.assertFalse(valid)
        self.assertIn("unsupported_experiment_mode", errors)

    def test_requires_two_branches(self):
        valid, errors = self._validate(branches=[{"key": "a", "weight": 1}])
        self.assertFalse(valid)
        self.assertIn("at_least_two_branches_required", errors)

    def test_branch_keys_must_be_unique(self):
        valid, errors = self._validate(branches=[{"key": "a", "weight": 1}, {"key": "a", "weight": 1}])
        self.assertFalse(valid)
        self.assertIn("branch_keys_must_be_unique", errors)

    def test_branch_weight_must_be_positive(self):
        valid, errors = self._validate(branches=[{"key": "a", "weight": 0}, {"key": "b", "weight": 1}])
        self.assertFalse(valid)
        self.assertIn("branch_a_weight_must_be_positive", errors)

    def test_safe_modes_exclude_real_run(self):
        self.assertNotIn("real_run", SAFE_EXPERIMENT_MODES)


class AssignBranchTests(unittest.TestCase):
    def test_deterministic_assignment(self):
        first = assign_branch(branches=TWO_BRANCHES, unit_key="lottery:42")
        second = assign_branch(branches=TWO_BRANCHES, unit_key="lottery:42")
        self.assertEqual(first, second)
        self.assertIn(first, {"a", "b"})

    def test_distribution_covers_both_branches(self):
        seen = {assign_branch(branches=TWO_BRANCHES, unit_key=f"lottery:{i}") for i in range(200)}
        self.assertEqual(seen, {"a", "b"})

    def test_no_usable_branch_returns_none(self):
        self.assertIsNone(assign_branch(branches=[{"key": "", "weight": 1}], unit_key="x"))

    def test_zero_weight_branch_never_assigned(self):
        branches = [{"key": "a", "weight": 0}, {"key": "b", "weight": 1}]
        seen = {assign_branch(branches=branches, unit_key=f"u{i}") for i in range(100)}
        self.assertEqual(seen, {"b"})

    def test_heavier_weight_gets_more_units(self):
        branches = [{"key": "a", "weight": 9}, {"key": "b", "weight": 1}]
        counts = {"a": 0, "b": 0}
        for i in range(1000):
            counts[assign_branch(branches=branches, unit_key=f"u{i}")] += 1
        self.assertGreater(counts["a"], counts["b"])


class BranchStatsTests(unittest.TestCase):
    def test_rates_computed(self):
        stats = branch_stats({"key": "a", "runs": 10, "participated": 8, "won": 3, "lost": 1, "failed": 2, "risk_events": 1})
        self.assertEqual(stats["participation_rate"], 0.8)
        self.assertEqual(stats["win_rate"], 0.75)  # 3 / (3 + 1)
        self.assertEqual(stats["failure_rate"], 0.2)
        self.assertEqual(stats["risk_rate"], 0.1)

    def test_zero_denominator_is_none(self):
        stats = branch_stats({"key": "a"})
        self.assertIsNone(stats["win_rate"])
        self.assertIsNone(stats["participation_rate"])


class CompareBranchesTests(unittest.TestCase):
    def test_insufficient_data_when_samples_thin(self):
        result = compare_branches(
            [{"key": "a", "runs": 2, "won": 2, "lost": 0}, {"key": "b", "runs": 1, "won": 0, "lost": 1}],
            min_samples=8,
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["leader"])

    def test_leader_found_with_enough_samples(self):
        result = compare_branches(
            [
                {"key": "a", "runs": 10, "won": 7, "lost": 3},
                {"key": "b", "runs": 10, "won": 3, "lost": 7},
            ],
            min_samples=8,
        )
        self.assertEqual(result["status"], "leader_found")
        self.assertEqual(result["leader"], "a")

    def test_thin_branch_excluded_from_leadership(self):
        result = compare_branches(
            [
                {"key": "a", "runs": 3, "won": 3, "lost": 0},  # too few runs
                {"key": "b", "runs": 10, "won": 5, "lost": 5},
            ],
            min_samples=8,
        )
        self.assertEqual(result["leader"], "b")


class ShouldStopBranchTests(unittest.TestCase):
    def test_hard_signal_stops_immediately(self):
        stop, reason = should_stop_branch(risk_events=0, runs=1, captcha_or_ban=True)
        self.assertTrue(stop)
        self.assertEqual(reason, "hard_risk_signal")

    def test_single_early_event_does_not_stop(self):
        stop, _ = should_stop_branch(risk_events=1, runs=2)
        self.assertFalse(stop)

    def test_risk_rate_exceeded_stops(self):
        stop, reason = should_stop_branch(risk_events=2, runs=5, risk_rate_threshold=0.2)
        self.assertTrue(stop)
        self.assertEqual(reason, "risk_rate_exceeded")

    def test_low_risk_rate_continues(self):
        stop, _ = should_stop_branch(risk_events=1, runs=20, risk_rate_threshold=0.2)
        self.assertFalse(stop)


class GuardrailTests(unittest.TestCase):
    def test_shadow_guardrails(self):
        notes = experiment_guardrails(mode="shadow_run", has_comment_branch=False)
        self.assertIn("runs_in_dry_or_shadow_only", notes)
        self.assertIn("results_are_advisory_only", notes)

    def test_real_run_guardrail_flagged(self):
        notes = experiment_guardrails(mode="real_run", has_comment_branch=True)
        self.assertIn("real_run_requires_admin_and_evidence_gate", notes)
        self.assertIn("comment_content_from_controlled_template_pool", notes)


if __name__ == "__main__":
    unittest.main()
