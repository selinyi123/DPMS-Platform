import unittest

from app.learning.feature_store import (
    FEATURE_SET_VERSION,
    FEATURE_DESCRIPTIONS,
    extract_features,
)
from app.learning.model import (
    MODEL_VERSION,
    WEIGHTS,
    explain_prediction,
    predict_success_probability,
)


class FeatureExtractionTests(unittest.TestCase):
    def _extract(self, **overrides):
        base = dict(
            value_score=60,
            win_rate=0.2,
            knowledge_confidence=50,
            account_reputation=80,
            dry_success=1,
            shadow_success=1,
            recent_platform_risk=0,
        )
        base.update(overrides)
        return extract_features(**base)

    def test_features_normalized_to_unit_range(self):
        result = self._extract(value_score=100, win_rate=2.0, knowledge_confidence=200, account_reputation=100, recent_platform_risk=100)
        for value in result["features"].values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_version_tagged(self):
        self.assertEqual(self._extract()["version"], FEATURE_SET_VERSION)

    def test_raw_inputs_preserved(self):
        result = self._extract(value_score=42)
        self.assertEqual(result["raw"]["value_score"], 42)

    def test_unknown_win_rate_is_zero(self):
        result = self._extract(win_rate=None)
        self.assertEqual(result["features"]["win_rate"], 0.0)

    def test_every_feature_has_a_description(self):
        result = self._extract()
        for feature in result["features"]:
            self.assertIn(feature, FEATURE_DESCRIPTIONS)

    def test_shadow_worth_more_than_dry(self):
        dry_only = self._extract(dry_success=1, shadow_success=0)["features"]["validation_depth"]
        shadow_only = self._extract(dry_success=0, shadow_success=1)["features"]["validation_depth"]
        self.assertGreater(shadow_only, dry_only)


class ModelTests(unittest.TestCase):
    def _features(self, **overrides):
        base = {name: 0.0 for name in WEIGHTS}
        base.update(overrides)
        return base

    def test_probability_in_range(self):
        for win in (0.0, 0.5, 1.0):
            for rep in (0.0, 0.5, 1.0):
                result = predict_success_probability(self._features(win_rate=win, account_reputation=rep))
                self.assertGreaterEqual(result["probability"], 0.0)
                self.assertLessEqual(result["probability"], 1.0)

    def test_cold_start_is_low_probability(self):
        result = predict_success_probability(self._features())
        self.assertLess(result["probability"], 0.2)

    def test_recent_risk_lowers_probability(self):
        calm = predict_success_probability(self._features(win_rate=0.5, value=0.6))
        risky = predict_success_probability(self._features(win_rate=0.5, value=0.6, recent_risk=1.0))
        self.assertGreater(calm["probability"], risky["probability"])

    def test_higher_win_rate_raises_probability(self):
        low = predict_success_probability(self._features(win_rate=0.1))
        high = predict_success_probability(self._features(win_rate=0.9))
        self.assertGreater(high["probability"], low["probability"])

    def test_contributions_sum_into_logit(self):
        features = self._features(win_rate=0.5, value=0.6, account_reputation=0.8)
        result = predict_success_probability(features)
        total = result["bias"] + sum(result["contributions"].values())
        self.assertAlmostEqual(total, result["logit"], places=4)

    def test_prediction_is_marked_advisory(self):
        self.assertTrue(predict_success_probability(self._features())["advisory"])

    def test_version_tagged(self):
        self.assertEqual(predict_success_probability(self._features())["model_version"], MODEL_VERSION)

    def test_explain_orders_top_drivers_by_magnitude(self):
        features = self._features(win_rate=0.9, recent_risk=0.8, value=0.1)
        result = explain_prediction(features)
        magnitudes = [abs(driver["contribution"]) for driver in result["top_drivers"]]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))


if __name__ == "__main__":
    unittest.main()
