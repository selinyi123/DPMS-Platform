import unittest

from app.orchestration.engine import (
    RAMP_STAGES,
    build_campaign_plan,
    campaign_risk_summary,
    target_stage,
    validate_campaign,
)


def target(lottery_id, *, platform="bilibili", value_score=50, gate_ready=False, shadow_eligible=False):
    return {
        "lottery_id": lottery_id,
        "platform": platform,
        "value_score": value_score,
        "gate_ready": gate_ready,
        "shadow_eligible": shadow_eligible,
    }


class TargetStageTests(unittest.TestCase):
    def test_gate_ready_target_is_real_run(self):
        self.assertEqual(target_stage(target(1, gate_ready=True)), "real_run")

    def test_shadow_eligible_only_is_shadow_run(self):
        self.assertEqual(target_stage(target(1, shadow_eligible=True)), "shadow_run")

    def test_unready_target_is_dry_run(self):
        self.assertEqual(target_stage(target(1)), "dry_run")


class BuildCampaignPlanTests(unittest.TestCase):
    def test_waves_follow_ramp_order_dry_then_shadow_then_real(self):
        targets = [
            target(1, gate_ready=True),       # real_run
            target(2, shadow_eligible=True),  # shadow_run
            target(3),                        # dry_run
        ]
        plan = build_campaign_plan(targets=targets, wave_capacity=10)
        modes_in_order = [w["mode"] for w in plan["waves"]]
        # dry_run wave(s) must come before shadow_run before real_run.
        self.assertEqual(modes_in_order, sorted(modes_in_order, key=lambda m: RAMP_STAGES.index(m)))
        self.assertEqual(modes_in_order[0], "dry_run")
        self.assertEqual(modes_in_order[-1], "real_run")

    def test_wave_capacity_splits_a_mode_into_subwaves(self):
        targets = [target(i, shadow_eligible=True) for i in range(1, 6)]  # 5 shadow targets
        plan = build_campaign_plan(targets=targets, wave_capacity=2)
        shadow_waves = [w for w in plan["waves"] if w["mode"] == "shadow_run"]
        self.assertEqual(len(shadow_waves), 3)  # 2 + 2 + 1
        self.assertTrue(all(w["size"] <= 2 for w in shadow_waves))

    def test_summary_counts_and_review_flag(self):
        targets = [target(1, gate_ready=True), target(2), target(3)]
        plan = build_campaign_plan(targets=targets, wave_capacity=10)
        self.assertEqual(plan["summary"]["by_mode"]["real_run"], 1)
        self.assertEqual(plan["summary"]["by_mode"]["dry_run"], 2)
        self.assertTrue(plan["summary"]["requires_review"])

    def test_capacity_caps_wave_size(self):
        targets = [target(i, shadow_eligible=True) for i in range(1, 5)]
        capacity = {"bilibili": {"sustainable_daily": 1}}
        plan = build_campaign_plan(targets=targets, capacity=capacity)
        shadow_waves = [w for w in plan["waves"] if w["mode"] == "shadow_run"]
        self.assertTrue(all(w["size"] <= 1 for w in shadow_waves))


class ValidateCampaignTests(unittest.TestCase):
    def test_real_run_without_gate_is_rejected(self):
        # Hand-build an illegal plan: a real_run item whose gate is not ready.
        plan = {"waves": [{"index": 0, "mode": "real_run", "items": [
            {"lottery_id": 9, "platform": "bilibili", "mode": "real_run"}
        ]}]}
        gate_state = {9: {"gate_ready": False, "shadow_eligible": True}}
        ok, errors = validate_campaign(plan, gate_state=gate_state)
        self.assertFalse(ok)
        self.assertTrue(any("real_run_without_gate" in e for e in errors))

    def test_shadow_without_probe_is_rejected(self):
        plan = {"waves": [{"index": 0, "mode": "shadow_run", "items": [
            {"lottery_id": 4, "platform": "bilibili", "mode": "shadow_run"}
        ]}]}
        gate_state = {4: {"gate_ready": False, "shadow_eligible": False}}
        ok, errors = validate_campaign(plan, gate_state=gate_state)
        self.assertFalse(ok)
        self.assertTrue(any("shadow_run_without_probe" in e for e in errors))

    def test_well_formed_plan_validates(self):
        targets = [target(1, gate_ready=True, shadow_eligible=True), target(2, shadow_eligible=True)]
        plan = build_campaign_plan(targets=targets, wave_capacity=10)
        gate_state = {
            1: {"gate_ready": True, "shadow_eligible": True},
            2: {"gate_ready": False, "shadow_eligible": True},
        }
        ok, errors = validate_campaign(plan, gate_state=gate_state)
        self.assertTrue(ok, errors)


class CampaignRiskSummaryTests(unittest.TestCase):
    def test_summary_aggregates_platform_load_and_review(self):
        targets = [target(1, platform="bilibili", gate_ready=True), target(2, platform="weibo")]
        plan = build_campaign_plan(targets=targets, wave_capacity=10)
        summary = campaign_risk_summary(plan)
        self.assertEqual(summary["platform_load"].get("bilibili"), 1)
        self.assertEqual(summary["platform_load"].get("weibo"), 1)
        self.assertTrue(summary["requires_review"])


if __name__ == "__main__":
    unittest.main()
