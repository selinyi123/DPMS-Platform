import unittest

from app.adapter_config import (
    PHASES,
    STRUCTURED_SELECTOR_PLATFORMS,
    click_selectors,
    decode_b64,
    has_complete_selectors,
    load_selector_config,
    selector_values,
    selectors_for,
)


def complete_structured_config():
    """A minimal but complete selector config for a structured platform."""
    return {
        "followed": {"click": ["button.follow"], "done": ["button.following"]},
        "liked": {"click": ["button.like"], "done": ["button.liked"]},
        "reposted": {"click": ["button.repost"], "done": ["div.repost-sent"]},
        "commented": {
            "input": ["textarea.comment"],
            "submit": ["button.send"],
            "done": ["div.comment-sent"],
        },
    }


class SelectorValueTests(unittest.TestCase):
    def test_string_value(self):
        self.assertEqual(selector_values("a.btn"), ["a.btn"])

    def test_blank_string_is_empty(self):
        self.assertEqual(selector_values("   "), [])

    def test_list_is_trimmed_and_filtered(self):
        self.assertEqual(selector_values(["  a ", "", "b"]), ["a", "b"])

    def test_non_selector_types_are_empty(self):
        self.assertEqual(selector_values(None), [])
        self.assertEqual(selector_values(123), [])
        self.assertEqual(selector_values({}), [])

    def test_click_selectors_unwraps_dict_keys(self):
        self.assertEqual(click_selectors({"click": ["x"]}), ["x"])
        self.assertEqual(click_selectors({"selectors": ["y"]}), ["y"])
        self.assertEqual(click_selectors({"buttons": ["z"]}), ["z"])
        self.assertEqual(click_selectors(["plain"]), ["plain"])
        self.assertEqual(click_selectors({}), [])


class DecodeB64Tests(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(decode_b64(""), "")

    def test_invalid_returns_empty(self):
        self.assertEqual(decode_b64("not-valid-base64!!!"), "")

    def test_round_trip(self):
        from base64 import b64encode

        encoded = b64encode(b'{"bilibili": {}}').decode("utf-8")
        self.assertEqual(decode_b64(encoded), '{"bilibili": {}}')


class LoadSelectorConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for key in ("DPMS_ADAPTER_SELECTORS", "DPMS_ADAPTER_SELECTORS_B64"):
            self._saved[key] = __import__("os").environ.pop(key, None)

    def tearDown(self):
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_no_env_returns_empty(self):
        self.assertEqual(load_selector_config(), {})

    def test_raw_json_env(self):
        import os

        os.environ["DPMS_ADAPTER_SELECTORS"] = '{"weibo": {"followed": "x"}}'
        self.assertEqual(load_selector_config(), {"weibo": {"followed": "x"}})

    def test_base64_env_takes_precedence(self):
        import os
        from base64 import b64encode

        os.environ["DPMS_ADAPTER_SELECTORS"] = '{"weibo": {}}'
        os.environ["DPMS_ADAPTER_SELECTORS_B64"] = b64encode(b'{"douyin": {}}').decode("utf-8")
        self.assertEqual(load_selector_config(), {"douyin": {}})

    def test_malformed_json_returns_empty(self):
        import os

        os.environ["DPMS_ADAPTER_SELECTORS"] = "{not json"
        self.assertEqual(load_selector_config(), {})

    def test_non_object_json_returns_empty(self):
        import os

        os.environ["DPMS_ADAPTER_SELECTORS"] = "[1, 2, 3]"
        self.assertEqual(load_selector_config(), {})

    def test_selectors_for_missing_platform(self):
        self.assertEqual(selectors_for("bilibili"), {})


class HasCompleteSelectorsTests(unittest.TestCase):
    def setUp(self):
        import os
        from base64 import b64encode
        import json

        self._saved = os.environ.pop("DPMS_ADAPTER_SELECTORS_B64", None)
        self._saved_raw = os.environ.pop("DPMS_ADAPTER_SELECTORS", None)
        self._b64encode = b64encode
        self._json = json

    def tearDown(self):
        import os

        for key, value in (("DPMS_ADAPTER_SELECTORS_B64", self._saved),
                           ("DPMS_ADAPTER_SELECTORS", self._saved_raw)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set(self, config):
        import os

        os.environ["DPMS_ADAPTER_SELECTORS_B64"] = self._b64encode(
            self._json.dumps(config).encode("utf-8")
        ).decode("utf-8")

    def test_all_structured_platforms_complete(self):
        for platform in STRUCTURED_SELECTOR_PLATFORMS:
            with self.subTest(platform=platform):
                self._set({platform: complete_structured_config()})
                self.assertEqual(
                    has_complete_selectors(platform),
                    platform != "xiaohongshu",
                )

    def test_xiaohongshu_selectors_are_observation_only(self):
        self._set({"xiaohongshu": complete_structured_config()})
        self.assertFalse(has_complete_selectors("xiaohongshu"))

    def test_missing_phase_is_incomplete(self):
        config = complete_structured_config()
        del config["liked"]
        self._set({"bilibili": config})
        self.assertFalse(has_complete_selectors("bilibili"))

    def test_comment_requires_input_and_submit(self):
        config = complete_structured_config()
        config["commented"] = {"input": ["textarea"]}  # missing submit
        self._set({"xiaohongshu": config})
        self.assertFalse(has_complete_selectors("xiaohongshu"))

    def test_every_mutation_requires_success_readback_selector(self):
        for phase in ("followed", "liked", "commented", "reposted"):
            with self.subTest(phase=phase):
                config = complete_structured_config()
                del config[phase]["done"]
                self._set({"weibo": config})
                self.assertFalse(has_complete_selectors("weibo"))

    def test_comment_must_be_dict(self):
        config = complete_structured_config()
        config["commented"] = ["button.send"]  # wrong shape
        self._set({"douyin": config})
        self.assertFalse(has_complete_selectors("douyin"))

    def test_no_config_is_incomplete(self):
        self.assertFalse(has_complete_selectors("bilibili"))


class StructureConstantsTests(unittest.TestCase):
    def test_four_required_phases(self):
        self.assertEqual(PHASES, ("followed", "liked", "commented", "reposted"))

    def test_all_four_platforms_are_structured(self):
        self.assertEqual(
            set(STRUCTURED_SELECTOR_PLATFORMS),
            {"bilibili", "weibo", "xiaohongshu", "douyin"},
        )


if __name__ == "__main__":
    unittest.main()
