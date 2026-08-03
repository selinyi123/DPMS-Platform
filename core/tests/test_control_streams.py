"""Shared Probe/calibration transport topology contracts."""

from __future__ import annotations

import unittest
import uuid

from app.account_calibration_streams import (
    account_calibration_stream_binding_for_platform,
    account_calibration_stream_bindings,
    validate_account_calibration_stream_message,
)
from app.adapter_probe_streams import (
    adapter_probe_stream_binding_for_platform,
    adapter_probe_stream_bindings,
    validate_adapter_probe_stream_message,
)
from app.services.outbox import _validate_task_stream_binding
from app.task_streams import task_stream_bindings
from shared.platform_ids import PLATFORM_IDS


class ControlStreamTopologyTests(unittest.TestCase):
    def test_all_platforms_have_disjoint_probe_and_calibration_lanes(self):
        probe_bindings = adapter_probe_stream_bindings(
            include_legacy=False
        )
        calibration_bindings = account_calibration_stream_bindings(
            include_legacy=False
        )
        self.assertEqual(
            {binding.platform for binding in probe_bindings},
            set(PLATFORM_IDS),
        )
        self.assertEqual(
            {binding.platform for binding in calibration_bindings},
            set(PLATFORM_IDS),
        )
        all_keys = {
            binding.stream_key
            for binding in (
                *task_stream_bindings(include_legacy=True),
                *adapter_probe_stream_bindings(include_legacy=True),
                *account_calibration_stream_bindings(include_legacy=True),
            )
        }
        self.assertEqual(
            len(all_keys),
            len(task_stream_bindings(include_legacy=True))
            + len(adapter_probe_stream_bindings(include_legacy=True))
            + len(account_calibration_stream_bindings(include_legacy=True)),
        )

    def test_probe_envelope_cannot_cross_platform_lane(self):
        binding = adapter_probe_stream_binding_for_platform("bilibili")
        with self.assertRaisesRegex(
            ValueError,
            "adapter_probe_stream_platform_mismatch",
        ):
            validate_adapter_probe_stream_message(
                binding,
                {"probe_id": "probe-1", "platform": "weibo"},
            )

    def test_platform_probe_envelope_requires_complete_authority(self):
        binding = adapter_probe_stream_binding_for_platform("bilibili")
        with self.assertRaisesRegex(
            ValueError,
            "adapter_probe_stream_message_contract_invalid",
        ):
            validate_adapter_probe_stream_message(
                binding,
                {"probe_id": "probe-1", "platform": "bilibili"},
            )

    def test_calibration_envelope_cannot_cross_platform_lane(self):
        binding = account_calibration_stream_binding_for_platform(
            "bilibili"
        )
        message = {
            "calibration_id": str(uuid.uuid4()),
            "account_id": "7",
            "platform": "weibo",
            "check_url": "https://weibo.com/",
            "calibration_kind": "browser",
            "fallback_account_status": "login_required",
        }
        with self.assertRaisesRegex(
            ValueError,
            "account_calibration_stream_platform_mismatch",
        ):
            validate_account_calibration_stream_message(binding, message)

    def test_generic_outbox_validator_applies_control_lane_contracts(self):
        probe_binding = adapter_probe_stream_binding_for_platform(
            "bilibili"
        )
        with self.assertRaisesRegex(
            ValueError,
            "adapter_probe_stream_platform_mismatch",
        ):
            _validate_task_stream_binding(
                {"probe_id": "probe-1", "platform": "weibo"},
                probe_binding.stream_key,
            )


if __name__ == "__main__":
    unittest.main()
