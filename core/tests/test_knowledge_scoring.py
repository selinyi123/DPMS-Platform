import unittest

from app.knowledge.scoring import (
    account_reputation,
    as_float,
    as_int,
    build_data_maturity,
    build_learning_gaps,
    clamp,
    confidence_score,
    gap,
    ratio,
)


def reputation(**overrides):
    base = dict(
        status="ready",
        risk_score=0,
        latest_calibration_status="succeeded",
        total_runs=0,
        succeeded_runs=0,
        failed_runs=0,
        shadow_runs=0,
        real_runs=0,
        risk_events=0,
    )
    base.update(overrides)
    return account_reputation(**base)


class AccountReputationTests(unittest.TestCase):
    def test_calibrated_idle_account_is_healthy(self):
        # 70 base + 8 calibrated + 4 (ready, no runs) = 82
        self.assertEqual(reputation(), 82)

    def test_bounds(self):
        self.assertLessEqual(reputation(risk_events=100, failed_runs=100, status="banned"), 100)
        self.assertGreaterEqual(reputation(risk_events=100, failed_runs=100, status="banned"), 0)
        self.assertEqual(reputation(succeeded_runs=100, shadow_runs=100, real_runs=100), 100)

    def test_risk_events_reduce_reputation(self):
        self.assertGreater(reputation(risk_events=0, total_runs=1), reputation(risk_events=2, total_runs=1))

    def test_banned_status_penalised_more_than_cooling(self):
        banned = reputation(status="banned", total_runs=1)
        cooling = reputation(status="cooling", total_runs=1)
        self.assertLess(banned, cooling)

    def test_uncalibrated_scores_lower_than_calibrated(self):
        self.assertLess(
            reputation(latest_calibration_status=None, total_runs=1),
            reputation(latest_calibration_status="succeeded", total_runs=1),
        )

    def test_successful_runs_raise_reputation(self):
        self.assertGreater(reputation(succeeded_runs=4, total_runs=4), reputation(total_runs=4))


class ConfidenceScoreTests(unittest.TestCase):
    def test_zero_inputs_zero_confidence(self):
        self.assertEqual(
            confidence_score(total_lotteries=0, result_labels=0, task_runs=0,
                             shadow_success=0, real_success=0, event_count=0),
            0,
        )

    def test_capped_at_100(self):
        self.assertEqual(
            confidence_score(total_lotteries=100, result_labels=100, task_runs=100,
                             shadow_success=100, real_success=100, event_count=100),
            100,
        )

    def test_more_evidence_more_confidence(self):
        low = confidence_score(total_lotteries=1, result_labels=0, task_runs=0,
                               shadow_success=0, real_success=0, event_count=1)
        high = confidence_score(total_lotteries=5, result_labels=2, task_runs=3,
                                shadow_success=2, real_success=1, event_count=5)
        self.assertGreater(high, low)


class DataMaturityTests(unittest.TestCase):
    def _profiles(self, **overrides):
        base = dict(
            total_lotteries=0, total_runs=0, won_lotteries=0, lost_lotteries=0,
            shadow_success=0, real_success=0, event_count=0,
        )
        base.update(overrides)
        return [base]

    def test_cold_start_when_no_data(self):
        summary = build_data_maturity(
            platform_profiles=self._profiles(),
            account_profiles=[],
            risk_profile={"by_type": []},
            lottery_profile={"by_value_band": []},
            event_profile={"total_events": 0},
        )
        self.assertEqual(summary["data_maturity_level"], "cold_start")
        self.assertEqual(summary["data_maturity_score"], 0)

    def test_score_rises_with_evidence(self):
        summary = build_data_maturity(
            platform_profiles=self._profiles(total_lotteries=5, total_runs=4, won_lotteries=2,
                                             lost_lotteries=1, shadow_success=2, event_count=10),
            account_profiles=[{"account_id": 1}],
            risk_profile={"by_type": [{"count": 2}]},
            lottery_profile={"by_value_band": [{"value_band": "high"}]},
            event_profile={"total_events": 20},
        )
        self.assertGreater(summary["data_maturity_score"], 0)
        self.assertIn(summary["data_maturity_level"], {"warming", "usable", "learning_ready"})
        self.assertEqual(summary["accounts_profiled"], 1)

    def test_score_bounded_at_100(self):
        summary = build_data_maturity(
            platform_profiles=self._profiles(total_lotteries=999, total_runs=999, won_lotteries=999,
                                             lost_lotteries=999, shadow_success=999, event_count=999),
            account_profiles=[{"account_id": i} for i in range(20)],
            risk_profile={"by_type": [{"count": 999}]},
            lottery_profile={"by_value_band": [{"value_band": "high"}]},
            event_profile={"total_events": 9999},
        )
        self.assertLessEqual(summary["data_maturity_score"], 100)
        self.assertEqual(summary["data_maturity_level"], "learning_ready")


class LearningGapsTests(unittest.TestCase):
    def test_cold_system_flags_p0_gaps(self):
        gaps = build_learning_gaps(
            summary={"total_events": 0, "result_labels": 0, "shadow_success": 0, "risk_events": 0},
            platform_profiles=[],
            account_profiles=[],
            risk_profile={"by_type": []},
            lottery_profile={"by_value_band": []},
        )
        codes = {g["code"] for g in gaps}
        self.assertIn("event_memory_empty", codes)
        self.assertIn("account_profiles_empty", codes)
        self.assertTrue(any(g["priority"] == "P0" for g in gaps))

    def test_returns_at_most_eight_gaps(self):
        gaps = build_learning_gaps(
            summary={"total_events": 0, "result_labels": 0, "shadow_success": 0, "risk_events": 0},
            platform_profiles=[{"platform": f"p{i}", "total_lotteries": 0, "total_runs": 0} for i in range(20)],
            account_profiles=[],
            risk_profile={"by_type": []},
            lottery_profile={"by_value_band": []},
        )
        self.assertLessEqual(len(gaps), 8)

    def test_mature_system_has_no_blocking_gaps(self):
        gaps = build_learning_gaps(
            summary={"total_events": 100, "result_labels": 10, "shadow_success": 5, "risk_events": 3},
            platform_profiles=[{"platform": "bilibili", "total_lotteries": 10, "total_runs": 10}],
            account_profiles=[{"account_id": 1}],
            risk_profile={"by_type": [{"count": 3}]},
            lottery_profile={"by_value_band": [{"value_band": "high"}]},
        )
        self.assertEqual(gaps, [])

    def test_gap_shape(self):
        item = gap("c", "P1", "title", "detail", {"x": 1})
        self.assertEqual(item["code"], "c")
        self.assertEqual(item["evidence"], {"x": 1})


class NumericHelperTests(unittest.TestCase):
    def test_ratio_handles_zero_denominator(self):
        self.assertIsNone(ratio(5, 0))
        self.assertEqual(ratio(1, 2), 0.5)

    def test_as_int_coerces_safely(self):
        self.assertEqual(as_int(None), 0)
        self.assertEqual(as_int("7"), 7)
        self.assertEqual(as_int("bad"), 0)

    def test_as_float_coerces_safely(self):
        self.assertEqual(as_float(None), 0.0)
        self.assertEqual(as_float("2.5"), 2.5)
        self.assertEqual(as_float("bad"), 0.0)

    def test_clamp(self):
        self.assertEqual(clamp(150, 0, 100), 100)
        self.assertEqual(clamp(-5, 0, 100), 0)
        self.assertEqual(clamp(42.6, 0, 100), 43)


if __name__ == "__main__":
    unittest.main()
