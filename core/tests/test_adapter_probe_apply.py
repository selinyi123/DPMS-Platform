import json
import unittest

from app.adapter_config import recommended_config_from_probe, selector_config_complete


def _probe_result(platform="bilibili", recommended=None, **extra):
    """A stored probe result shaped like worker handle_probe writes it."""
    result = {
        "_summary": {"ready_for_real_actions": True},
        **extra,
    }
    if recommended is not None:
        result["_recommended_config"] = recommended
    return result


def _complete_bilibili_recommendation():
    # Matches what worker build_recommended_config emits for a fully visible page.
    return {
        "bilibili": {
            "followed": ["button:has-text('关注')"],
            "liked": ["[aria-label*='点赞']"],
            "reposted": ["button:has-text('转发')"],
            "commented": {"input": ["textarea"], "submit": ["button:has-text('发布')"], "text": "参与抽奖"},
        }
    }


class RecommendedConfigFromProbeTests(unittest.TestCase):
    def test_extracts_platform_recommendation(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        config = recommended_config_from_probe(result, "bilibili")
        self.assertEqual(config["followed"], ["button:has-text('关注')"])
        self.assertIn("commented", config)

    def test_accepts_json_string_result(self):
        # adapter_calibrations.result is stored as a JSON string column.
        result = json.dumps(_probe_result(recommended=_complete_bilibili_recommendation()))
        config = recommended_config_from_probe(result, "bilibili")
        self.assertTrue(config)

    def test_probe_recommendation_without_success_readback_stays_incomplete(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        config = recommended_config_from_probe(result, "bilibili")
        self.assertFalse(selector_config_complete("bilibili", config))

    def test_missing_recommendation_returns_empty(self):
        self.assertEqual(recommended_config_from_probe(_probe_result(), "bilibili"), {})
        self.assertEqual(recommended_config_from_probe({}, "bilibili"), {})
        self.assertEqual(recommended_config_from_probe(None, "bilibili"), {})

    def test_wrong_platform_returns_empty(self):
        result = _probe_result(recommended=_complete_bilibili_recommendation())
        self.assertEqual(recommended_config_from_probe(result, "weibo"), {})

    def test_incomplete_recommendation_is_extracted_but_fails_gate(self):
        # A probe that only saw follow/like/repost (no comment input+submit pair)
        # still yields a dict, but must not pass the real-actions completeness bar.
        partial = {"bilibili": {"followed": ["a"], "liked": ["b"], "reposted": ["c"]}}
        config = recommended_config_from_probe(_probe_result(recommended=partial), "bilibili")
        self.assertTrue(config)
        self.assertFalse(selector_config_complete("bilibili", config))

    def test_malformed_recommendation_shapes_return_empty(self):
        for bad in ([], "nope", 5, {"_recommended_config": []}, {"_recommended_config": {"bilibili": "x"}}):
            self.assertEqual(recommended_config_from_probe(bad, "bilibili"), {})


if __name__ == "__main__":
    unittest.main()
