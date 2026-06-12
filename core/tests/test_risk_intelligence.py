import unittest

from app.risk.intelligence import (
    RECOMMENDED_ACTIONS,
    forecast_account_risk,
    tighten_action,
)


class TightenActionTests(unittest.TestCase):
    def test_picks_more_conservative(self):
        self.assertEqual(tighten_action("allow", "cooldown"), "cooldown")
        self.assertEqual(tighten_action("manual_review", "allow"), "manual_review")

    def test_block_is_most_severe(self):
        for action in RECOMMENDED_ACTIONS:
            self.assertEqual(tighten_action(action, "block"), "block")
            self.assertEqual(tighten_action("block", action), "block")

    def test_allow_never_overrides(self):
        self.assertEqual(tighten_action("dry_run_only", "allow"), "dry_run_only")

    def test_unknown_action_treated_as_allow(self):
        self.assertEqual(tighten_action("nonsense", "cooldown"), "cooldown")


class ForecastTests(unittest.TestCase):
    def _forecast(self, **overrides):
        base = dict(
            reputation_score=80,
            risk_events_24h=0,
            risk_events_7d=0,
            status="ready",
            latest_calibration_status="succeeded",
            consecutive_failures=0,
            daily_task_count=0,
        )
        base.update(overrides)
        return forecast_account_risk(**base)

    def test_healthy_account_allows(self):
        result = self._forecast()
        self.assertEqual(result["recommended_action"], "allow")
        self.assertLess(result["forecast_24h"], 0.1)
        self.assertEqual(result["forecast_band"], "low")

    def test_recommended_action_always_in_safe_set(self):
        for status in ("ready", "cooling", "login_required", "frozen", "banned"):
            for events in (0, 1, 3, 10):
                result = self._forecast(status=status, risk_events_24h=events, risk_events_7d=events)
                self.assertIn(result["recommended_action"], RECOMMENDED_ACTIONS)

    def test_banned_blocks(self):
        self.assertEqual(self._forecast(status="banned")["recommended_action"], "block")

    def test_login_required_recommends_relogin(self):
        self.assertEqual(self._forecast(status="login_required")["recommended_action"], "relogin")

    def test_single_risk_event_recommends_cooldown(self):
        self.assertEqual(self._forecast(risk_events_24h=1, risk_events_7d=1)["recommended_action"], "cooldown")

    def test_hard_signal_recommends_manual_review(self):
        result = self._forecast(hard_signal_24h=True)
        self.assertEqual(result["recommended_action"], "manual_review")

    def test_consecutive_failures_downgrade_to_dry_run(self):
        self.assertEqual(self._forecast(consecutive_failures=3)["recommended_action"], "dry_run_only")

    def test_low_reputation_downgrades_to_dry_run(self):
        self.assertEqual(self._forecast(reputation_score=40)["recommended_action"], "dry_run_only")

    def test_risk_score_bounds(self):
        for rep in (0, 50, 100):
            for events in (0, 5, 50):
                result = self._forecast(reputation_score=rep, risk_events_24h=events, risk_events_7d=events)
                self.assertGreaterEqual(result["risk_score"], 0)
                self.assertLessEqual(result["risk_score"], 100)

    def test_forecast_bounded_below_certainty(self):
        result = self._forecast(status="banned", risk_events_24h=50, risk_events_7d=50, hard_signal_24h=True)
        self.assertLessEqual(result["forecast_24h"], 0.95)

    def test_more_recent_risk_raises_forecast(self):
        calm = self._forecast(risk_events_24h=0, risk_events_7d=0)
        risky = self._forecast(risk_events_24h=2, risk_events_7d=4)
        self.assertGreater(risky["forecast_24h"], calm["forecast_24h"])
        self.assertGreater(risky["risk_score"], calm["risk_score"])


if __name__ == "__main__":
    unittest.main()
