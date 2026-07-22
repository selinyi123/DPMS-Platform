import unittest

from app.strategy.engine import (
    account_tier,
    choose_strategy_mode,
    empty_platform_knowledge,
    estimate_trust_score,
    estimate_win_probability,
    first_or_none,
    priority_tier,
    strategy_knowledge_confidence,
    strategy_score,
)


READY_GATE = dict(
    safe_accounts=1,
    active_runs=0,
    dry_success=1,
    shadow_success=1,
    adapter_ready=True,
    probe_ready=True,
    real_run_enabled=True,
    breaker_allowed=True,
    breaker_reason=None,
)


class ChooseStrategyModeTests(unittest.TestCase):
    def test_active_run_blocks_first(self):
        mode, reasons, blockers = choose_strategy_mode(**{**READY_GATE, "active_runs": 1})
        self.assertEqual(mode, "blocked")
        self.assertIn("active_run_in_progress", reasons)
        self.assertTrue(blockers)

    def test_no_safe_account_blocks(self):
        mode, reasons, _ = choose_strategy_mode(**{**READY_GATE, "safe_accounts": 0})
        self.assertEqual(mode, "blocked")
        self.assertIn("no_safe_account", reasons)

    def test_open_circuit_breaker_blocks_with_reason(self):
        mode, reasons, blockers = choose_strategy_mode(
            **{**READY_GATE, "breaker_allowed": False, "breaker_reason": "too many failures"}
        )
        self.assertEqual(mode, "blocked")
        self.assertIn("circuit_breaker_open", reasons)
        self.assertEqual(blockers, ["too many failures"])

    def test_breaker_block_has_default_reason(self):
        _, _, blockers = choose_strategy_mode(
            **{**READY_GATE, "breaker_allowed": False, "breaker_reason": None}
        )
        self.assertEqual(blockers, ["circuit breaker open"])

    def test_requires_dry_validation_first(self):
        mode, reasons, _ = choose_strategy_mode(**{**READY_GATE, "dry_success": 0})
        self.assertEqual(mode, "dry_run")
        self.assertIn("dry_validation_needed", reasons)

    def test_requires_shadow_validation_before_real(self):
        mode, reasons, _ = choose_strategy_mode(**{**READY_GATE, "shadow_success": 0})
        self.assertEqual(mode, "shadow_run")
        self.assertIn("shadow_validation_needed", reasons)

    def test_manual_assisted_skips_unavailable_dry_run(self):
        mode, reasons, blockers = choose_strategy_mode(
            **{**READY_GATE, "dry_success": 0, "shadow_success": 0},
            manual_assisted=True,
        )
        self.assertEqual(mode, "shadow_run")
        self.assertEqual(reasons, ["manual_assisted_shadow_only"])
        self.assertEqual(blockers, [])

    def test_manual_assisted_does_not_bypass_hard_blockers(self):
        mode, reasons, blockers = choose_strategy_mode(
            **{**READY_GATE, "safe_accounts": 0},
            manual_assisted=True,
        )
        self.assertEqual(mode, "blocked")
        self.assertIn("no_safe_account", reasons)
        self.assertTrue(blockers)

    def test_full_gate_recommends_real_run(self):
        mode, reasons, blockers = choose_strategy_mode(**READY_GATE)
        self.assertEqual(mode, "real_run")
        self.assertEqual(reasons, ["real_gate_ready"])
        self.assertEqual(blockers, [])

    def test_partial_gate_falls_back_to_shadow_with_reasons(self):
        mode, reasons, _ = choose_strategy_mode(
            **{**READY_GATE, "adapter_ready": False, "probe_ready": False, "real_run_enabled": False}
        )
        self.assertEqual(mode, "shadow_run")
        self.assertIn("adapter_not_ready", reasons)
        self.assertIn("probe_not_ready", reasons)
        self.assertIn("real_run_disabled", reasons)

    def test_real_run_disabled_never_yields_real_run(self):
        # The single most important safety property: a disabled global switch
        # must never produce a real_run recommendation.
        mode, _, _ = choose_strategy_mode(**{**READY_GATE, "real_run_enabled": False})
        self.assertNotEqual(mode, "real_run")


class StrategyScoreTests(unittest.TestCase):
    def _score(self, **overrides):
        base = dict(
            value_score=60,
            recommended_mode="real_run",
            dry_success=1,
            shadow_success=1,
            failed_runs=0,
            recent_risk=0,
            knowledge_confidence=50,
            estimated_win_probability=0.1,
            account_reputation_score=80,
            expected_value=0.5,
        )
        base.update(overrides)
        return strategy_score(**base)

    def test_blocked_mode_scores_zero(self):
        self.assertEqual(self._score(recommended_mode="blocked"), 0.0)

    def test_score_never_negative(self):
        self.assertGreaterEqual(
            self._score(value_score=0, recent_risk=10, failed_runs=10, knowledge_confidence=0,
                        account_reputation_score=0, expected_value=0, estimated_win_probability=0),
            0.0,
        )

    def test_higher_value_scores_higher(self):
        self.assertGreater(self._score(value_score=90), self._score(value_score=20))

    def test_real_run_outranks_shadow_and_dry(self):
        real = self._score(recommended_mode="real_run")
        shadow = self._score(recommended_mode="shadow_run")
        dry = self._score(recommended_mode="dry_run")
        self.assertGreater(real, shadow)
        self.assertGreater(shadow, dry)

    def test_risk_and_failures_reduce_score(self):
        clean = self._score(recent_risk=0, failed_runs=0)
        risky = self._score(recent_risk=3, failed_runs=3)
        self.assertGreater(clean, risky)

    def test_risk_penalty_is_capped(self):
        # recent_risk * 8 + failed_runs * 7 saturates at 45, so extreme values
        # do not produce an unbounded penalty.
        capped = self._score(recent_risk=100, failed_runs=100)
        moderate = self._score(recent_risk=6, failed_runs=0)  # already hits the cap
        self.assertEqual(capped, moderate)


class WinProbabilityTests(unittest.TestCase):
    def test_none_win_rate_returns_baseline(self):
        self.assertEqual(estimate_win_probability(None, 100), 0.05)

    def test_low_confidence_stays_near_baseline(self):
        value = estimate_win_probability(0.5, 0)
        self.assertAlmostEqual(value, 0.05, places=4)

    def test_higher_confidence_moves_toward_observed_rate(self):
        low = estimate_win_probability(0.5, 10)
        high = estimate_win_probability(0.5, 100)
        self.assertGreater(high, low)

    def test_confidence_weight_capped_below_full_trust(self):
        # Even at confidence 100, weight caps at 0.75, so the estimate never
        # fully equals the raw observed rate.
        value = estimate_win_probability(0.8, 100)
        self.assertLess(value, 0.8)


class TrustScoreTests(unittest.TestCase):
    def test_bounds(self):
        for rep in (0, 50, 100):
            for risk in (0, 5, 50):
                for conf in (0, 50, 100):
                    value = estimate_trust_score(
                        account_reputation_score=rep, recent_platform_risk=risk, knowledge_confidence=conf
                    )
                    self.assertGreaterEqual(value, 0.05)
                    self.assertLessEqual(value, 1.0)

    def test_recent_risk_lowers_trust(self):
        calm = estimate_trust_score(account_reputation_score=80, recent_platform_risk=0, knowledge_confidence=0)
        risky = estimate_trust_score(account_reputation_score=80, recent_platform_risk=5, knowledge_confidence=0)
        self.assertGreater(calm, risky)

    def test_zero_reputation_uses_conservative_floor(self):
        value = estimate_trust_score(account_reputation_score=0, recent_platform_risk=0, knowledge_confidence=0)
        self.assertAlmostEqual(value, 0.35, places=4)


class KnowledgeConfidenceTests(unittest.TestCase):
    def test_empty_knowledge_is_zero(self):
        self.assertEqual(strategy_knowledge_confidence(empty_platform_knowledge("bilibili")), 0)

    def test_confidence_is_capped_at_100(self):
        rich = {
            "total_lotteries": 100,
            "won_lotteries": 100,
            "lost_lotteries": 100,
            "total_runs": 100,
            "shadow_success": 100,
            "real_success": 100,
        }
        self.assertEqual(strategy_knowledge_confidence(rich), 100)

    def test_more_evidence_raises_confidence(self):
        light = {"total_lotteries": 1, "won_lotteries": 0, "lost_lotteries": 0, "total_runs": 0}
        heavy = {"total_lotteries": 6, "won_lotteries": 3, "lost_lotteries": 1, "total_runs": 4, "shadow_success": 2}
        self.assertGreater(strategy_knowledge_confidence(heavy), strategy_knowledge_confidence(light))


class TierTests(unittest.TestCase):
    def test_priority_tier_thresholds(self):
        self.assertEqual(priority_tier(80), "S")
        self.assertEqual(priority_tier(70), "S")
        self.assertEqual(priority_tier(45), "A")
        self.assertEqual(priority_tier(20), "B")
        self.assertEqual(priority_tier(0), "hold")

    def test_account_tier_thresholds(self):
        self.assertEqual(account_tier(90), "S")
        self.assertEqual(account_tier(85), "S")
        self.assertEqual(account_tier(70), "A")
        self.assertEqual(account_tier(50), "B")
        self.assertEqual(account_tier(49), "watch")

    def test_tiers_are_monotonic(self):
        order = {"hold": 0, "B": 1, "A": 2, "S": 3}
        scores = [0, 10, 25, 50, 70, 95]
        tiers = [order[priority_tier(s)] for s in scores]
        self.assertEqual(tiers, sorted(tiers))


class HelperTests(unittest.TestCase):
    def test_first_or_none(self):
        self.assertIsNone(first_or_none([]))
        self.assertEqual(first_or_none([1, 2, 3]), 1)

    def test_empty_platform_knowledge_shape(self):
        knowledge = empty_platform_knowledge("weibo")
        self.assertEqual(knowledge["platform"], "weibo")
        self.assertIsNone(knowledge["win_rate"])
        self.assertEqual(knowledge["knowledge_confidence"], 0)


if __name__ == "__main__":
    unittest.main()
