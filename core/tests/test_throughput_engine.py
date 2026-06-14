import unittest

from app.throughput.engine import (
    backpressure_recommendation,
    measure_throughput,
    saturation_alerts,
    sustainable_ceiling,
)


def run(platform, status):
    return {"platform": platform, "status": status}


class MeasureThroughputTests(unittest.TestCase):
    def test_counts_and_rates_per_platform(self):
        runs = [run("bilibili", "succeeded"), run("bilibili", "failed"), run("weibo", "succeeded")]
        out = measure_throughput(runs, window_min=60)
        self.assertEqual(out["bilibili"]["observed"], 2)
        self.assertEqual(out["bilibili"]["success_rate"], 0.5)
        self.assertEqual(out["bilibili"]["failure_rate"], 0.5)
        self.assertEqual(out["weibo"]["observed"], 1)


class SustainableCeilingTests(unittest.TestCase):
    def test_risk_only_tightens_the_ceiling(self):
        capacity = {"bilibili": {"sustainable_daily": 100}}
        no_risk = sustainable_ceiling(capacity, {"bilibili": {"risk_rate": 0, "failure_rate": 0}})
        with_risk = sustainable_ceiling(capacity, {"bilibili": {"risk_rate": 0.5, "failure_rate": 0}})
        self.assertEqual(no_risk["bilibili"]["ceiling"], 100)
        self.assertLess(with_risk["bilibili"]["ceiling"], 100)

    def test_ceiling_never_drops_below_a_quarter(self):
        capacity = {"bilibili": {"sustainable_daily": 100}}
        crushing = sustainable_ceiling(capacity, {"bilibili": {"risk_rate": 5, "failure_rate": 5}})
        self.assertGreaterEqual(crushing["bilibili"]["ceiling"], 25)


class BackpressureRecommendationTests(unittest.TestCase):
    def test_rising_risk_never_scales_up(self):
        # SAFETY INVARIANT (V13): with risk rising, the action is throttle/pause,
        # never scale_up — even when utilisation is low.
        observed = {"bilibili": {"observed": 1, "risk_rate": 0.3}}
        ceiling = {"bilibili": {"ceiling": 100}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "rising"})
        self.assertIn(rec["bilibili"]["action"], ("throttle", "pause"))

    def test_high_risk_and_rising_pauses(self):
        observed = {"bilibili": {"observed": 50, "risk_rate": 0.4}}
        ceiling = {"bilibili": {"ceiling": 100}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "rising"})
        self.assertEqual(rec["bilibili"]["action"], "pause")

    def test_over_ceiling_throttles(self):
        observed = {"bilibili": {"observed": 120, "risk_rate": 0.0}}
        ceiling = {"bilibili": {"ceiling": 100}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "flat"})
        self.assertEqual(rec["bilibili"]["action"], "throttle")
        self.assertLess(rec["bilibili"]["factor"], 1.0)

    def test_low_utilization_stable_risk_may_scale_up_within_ceiling(self):
        observed = {"bilibili": {"observed": 10, "risk_rate": 0.0}}
        ceiling = {"bilibili": {"ceiling": 100}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "flat"})
        self.assertEqual(rec["bilibili"]["action"], "scale_up")
        # Projected load must not exceed the ceiling.
        self.assertLessEqual(10 * rec["bilibili"]["factor"], 100)

    def test_no_capacity_pauses(self):
        observed = {"bilibili": {"observed": 5, "risk_rate": 0.0}}
        ceiling = {"bilibili": {"ceiling": 0}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "flat"})
        self.assertEqual(rec["bilibili"]["action"], "pause")

    def test_approaching_ceiling_holds(self):
        observed = {"bilibili": {"observed": 85, "risk_rate": 0.0}}
        ceiling = {"bilibili": {"ceiling": 100}}
        rec = backpressure_recommendation(observed, ceiling, {"bilibili": "flat"})
        self.assertEqual(rec["bilibili"]["action"], "hold")


class SaturationAlertsTests(unittest.TestCase):
    def test_saturated_platform_alerts(self):
        observed = {"bilibili": {"observed": 95}, "weibo": {"observed": 10}}
        ceiling = {"bilibili": {"ceiling": 100}, "weibo": {"ceiling": 100}}
        alerts = saturation_alerts(observed, ceiling)
        self.assertEqual(alerts, ["bilibili"])

    def test_load_without_capacity_alerts(self):
        observed = {"bilibili": {"observed": 3}}
        ceiling = {"bilibili": {"ceiling": 0}}
        self.assertEqual(saturation_alerts(observed, ceiling), ["bilibili"])


if __name__ == "__main__":
    unittest.main()
