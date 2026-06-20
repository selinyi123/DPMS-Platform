import unittest

from app.scheduling.engine import (
    DEFAULT_RATE_LIMIT,
    PLATFORM_RATE_LIMITS,
    detect_overload,
    plan_schedule,
    rate_limit_for,
    respects_rate_limit,
)

# A small, explicit limit table so the tests are independent of the production
# defaults: 2 tasks/account/day, 30-min spacing, 10/window.
TEST_LIMITS = {
    "bilibili": {"max_daily_per_account": 2, "min_spacing_min": 30, "max_per_window": 10},
}


def candidate(lottery_id, value_score, platform="bilibili"):
    return {"lottery_id": lottery_id, "platform": platform, "value_score": value_score}


def account(account_id, *, quota, ready_in_min=0, risk_score=0, platform="bilibili"):
    return {
        "account_id": account_id,
        "platform": platform,
        "remaining_quota": quota,
        "ready_in_min": ready_in_min,
        "risk_score": risk_score,
    }


class RateLimitForTests(unittest.TestCase):
    def test_known_platform_returns_its_limit(self):
        self.assertEqual(rate_limit_for("bilibili"), PLATFORM_RATE_LIMITS["bilibili"])

    def test_unknown_platform_falls_back_to_conservative_default(self):
        self.assertEqual(rate_limit_for("myspace"), DEFAULT_RATE_LIMIT)


class PlanScheduleTests(unittest.TestCase):
    def test_higher_value_is_scheduled_first(self):
        plan = plan_schedule(
            candidates=[candidate(1, 10), candidate(2, 90)],
            accounts=[account(100, quota=2)],
            window_minutes=240,
            limits=TEST_LIMITS,
        )
        # Both fit (quota 2), but the earliest slot (offset 0) goes to the
        # higher-value candidate (#2).
        first = min(plan["slots"], key=lambda s: s["scheduled_at_offset_min"])
        self.assertEqual(first["lottery_id"], 2)

    def test_spacing_is_enforced_for_one_account(self):
        plan = plan_schedule(
            candidates=[candidate(1, 50), candidate(2, 40)],
            accounts=[account(100, quota=2)],
            window_minutes=240,
            limits=TEST_LIMITS,
        )
        offsets = sorted(s["scheduled_at_offset_min"] for s in plan["slots"])
        self.assertEqual(offsets, [0, 30])  # 30-min spacing applied

    def test_daily_quota_caps_an_account(self):
        # 3 candidates, one account with quota 2 -> one is unscheduled.
        plan = plan_schedule(
            candidates=[candidate(1, 50), candidate(2, 40), candidate(3, 30)],
            accounts=[account(100, quota=2)],
            window_minutes=600,
            limits=TEST_LIMITS,
        )
        self.assertEqual(plan["summary"]["planned"], 2)
        self.assertEqual(plan["summary"]["unscheduled"], 1)
        self.assertEqual(plan["unscheduled"][0]["reason"], "no_capacity_within_window")

    def test_window_excludes_slots_that_would_fall_outside(self):
        # Account is only free after 200 min; window is 120 -> nothing fits.
        plan = plan_schedule(
            candidates=[candidate(1, 50)],
            accounts=[account(100, quota=2, ready_in_min=200)],
            window_minutes=120,
            limits=TEST_LIMITS,
        )
        self.assertEqual(plan["summary"]["planned"], 0)
        self.assertEqual(plan["unscheduled"][0]["reason"], "no_capacity_within_window")

    def test_two_accounts_parallelise_within_spacing(self):
        # Two accounts can both take offset 0 (independent spacing).
        plan = plan_schedule(
            candidates=[candidate(1, 50), candidate(2, 40)],
            accounts=[account(100, quota=2), account(101, quota=2)],
            window_minutes=240,
            limits=TEST_LIMITS,
        )
        offsets = sorted(s["scheduled_at_offset_min"] for s in plan["slots"])
        self.assertEqual(offsets, [0, 0])

    def test_empty_inputs_do_not_raise(self):
        plan = plan_schedule(candidates=[], accounts=[], window_minutes=120, limits=TEST_LIMITS)
        self.assertEqual(plan["slots"], [])
        self.assertEqual(plan["unscheduled"], [])


class RespectsRateLimitInvariantTests(unittest.TestCase):
    def test_produced_plan_always_respects_rate_limit(self):
        # Safety invariant: whatever plan_schedule emits must pass the
        # independent re-check. This is the V10 standing safety regression.
        plan = plan_schedule(
            candidates=[candidate(i, 100 - i) for i in range(1, 12)],
            accounts=[account(100, quota=2), account(101, quota=2), account(102, quota=2)],
            window_minutes=300,
            limits=TEST_LIMITS,
        )
        ok, violations = respects_rate_limit(plan, limits=TEST_LIMITS)
        self.assertTrue(ok, violations)

    def test_hand_built_spacing_violation_is_caught(self):
        bad_plan = {
            "window_minutes": 240,
            "slots": [
                {"lottery_id": 1, "account_id": 100, "platform": "bilibili", "scheduled_at_offset_min": 0},
                {"lottery_id": 2, "account_id": 100, "platform": "bilibili", "scheduled_at_offset_min": 5},
            ],
        }
        ok, violations = respects_rate_limit(bad_plan, limits=TEST_LIMITS)
        self.assertFalse(ok)
        self.assertTrue(any("spacing_violation" in v for v in violations))


class DetectOverloadTests(unittest.TestCase):
    def test_demand_over_supply_flags_platform(self):
        overloaded = detect_overload(
            candidates=[candidate(i, 50) for i in range(1, 11)],
            accounts=[account(100, quota=2)],
            window_minutes=120,
            limits=TEST_LIMITS,
        )
        self.assertIn("bilibili", overloaded)

    def test_supply_meets_demand_is_not_flagged(self):
        overloaded = detect_overload(
            candidates=[candidate(1, 50)],
            accounts=[account(100, quota=2), account(101, quota=2)],
            window_minutes=240,
            limits=TEST_LIMITS,
        )
        self.assertEqual(overloaded, [])


if __name__ == "__main__":
    unittest.main()
