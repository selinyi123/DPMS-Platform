import unittest
import uuid

from shared.redis_consumer_groups import (
    expected_consumer_group_names,
    runtime_consumer_group_specs,
)
from shared.xiaohongshu_target_pursuit_streams import (
    XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME,
    XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
    validate_target_pursuit_stream_fields,
    xiaohongshu_target_pursuit_result_key,
)


class XiaohongshuTargetPursuitStreamTests(unittest.TestCase):
    def request(self, **overrides):
        values = {
            "protocol_version": "1",
            "request_id": str(uuid.uuid4()),
            "source_type": "keyword",
            "source_value": "抽奖",
            "requested_at_ms": "1785254400000",
            "max_candidates": "20",
        }
        values.update(overrides)
        return values

    def test_exact_request_contract_and_result_key(self):
        request = validate_target_pursuit_stream_fields(self.request())

        self.assertEqual("keyword", request["source_type"])
        self.assertEqual("20", request["max_candidates"])
        self.assertEqual(
            (
                "xiaohongshu_target_pursuit_result:v1:"
                f"{request['request_id']}"
            ),
            xiaohongshu_target_pursuit_result_key(
                request["request_id"]
            ),
        )

    def test_offline_import_never_enters_browser_queue(self):
        with self.assertRaisesRegex(
            ValueError,
            "xiaohongshu_target_pursuit_browser_source_unsupported",
        ):
            validate_target_pursuit_stream_fields(
                self.request(source_type="offline_search_result")
            )

    def test_unknown_fields_and_unbounded_limits_fail_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "xiaohongshu_target_pursuit_request_fields_invalid",
        ):
            validate_target_pursuit_stream_fields(
                {**self.request(), "cookie": "must-not-enter-redis"}
            )
        with self.assertRaisesRegex(
            ValueError,
            "xiaohongshu_target_pursuit_request_number_invalid",
        ):
            validate_target_pursuit_stream_fields(
                self.request(max_candidates="51")
            )
        with self.assertRaisesRegex(
            ValueError,
            "xiaohongshu_target_pursuit_source_value_invalid",
        ):
            validate_target_pursuit_stream_fields(
                self.request(source_value="抽" * 65)
            )

    def test_worker_topology_owns_xiaohongshu_lane_only(self):
        names = expected_consumer_group_names(
            XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY
        )
        self.assertEqual(
            frozenset({XIAOHONGSHU_TARGET_PURSUIT_GROUP_NAME}),
            names,
        )
        xhs_specs = runtime_consumer_group_specs(
            "worker",
            platforms=("xiaohongshu",),
            include_shared=False,
        )
        weibo_specs = runtime_consumer_group_specs(
            "worker",
            platforms=("weibo",),
            include_shared=False,
        )
        self.assertIn(
            XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
            {spec.stream_key for spec in xhs_specs},
        )
        self.assertNotIn(
            XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
            {spec.stream_key for spec in weibo_specs},
        )


if __name__ == "__main__":
    unittest.main()
