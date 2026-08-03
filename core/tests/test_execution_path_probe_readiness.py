import unittest

from app.adapter_config import platform_probe_ready_for_real_actions


class ExecutionPathProbeReadinessTests(unittest.TestCase):
    def test_browser_probe_cannot_qualify_bilibili_api_execution_path(self):
        summary = {"ready_for_real_actions": True, "ready_phase_count": 4}

        self.assertFalse(platform_probe_ready_for_real_actions("bilibili", summary))

    def test_selector_probe_without_config_version_binding_cannot_qualify_real_actions(self):
        summary = {"ready_for_real_actions": True, "ready_phase_count": 4}

        self.assertFalse(platform_probe_ready_for_real_actions("weibo", summary))

    def test_probe_flag_must_be_explicit_boolean_true(self):
        for value in (None, {}, {"ready_for_real_actions": 1}, {"ready_for_real_actions": "true"}):
            with self.subTest(value=value):
                self.assertFalse(platform_probe_ready_for_real_actions("weibo", value))


if __name__ == "__main__":
    unittest.main()
