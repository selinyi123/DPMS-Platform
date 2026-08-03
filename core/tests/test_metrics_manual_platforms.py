import unittest

from app.api.metrics import build_production_checks, stream


class ManualPlatformReadinessTests(unittest.TestCase):
    def test_manual_shadow_platforms_are_not_counted_as_missing_dry_run(self):
        summary = {
            "platforms_total": 4,
            "dry_run_supported": 2,
            "dry_run_ready": 2,
            "real_run_ready": 0,
        }
        checks = {
            item["code"]: item
            for item in build_production_checks([], summary)
        }
        self.assertTrue(checks["all_platforms_dry_ready"]["passed"])
        self.assertIn("2/2", checks["all_platforms_dry_ready"]["detail"])

    def test_supported_dry_run_platform_still_fails_when_not_ready(self):
        summary = {
            "platforms_total": 4,
            "dry_run_supported": 2,
            "dry_run_ready": 1,
            "real_run_ready": 0,
        }
        checks = {
            item["code"]: item
            for item in build_production_checks([], summary)
        }
        self.assertFalse(checks["all_platforms_dry_ready"]["passed"])

    def test_global_worker_does_not_hide_one_platform_transport_failure(self):
        summary = {
            "platforms_total": 4,
            "dry_run_supported": 2,
            "dry_run_ready": 2,
            "real_run_ready": 0,
            "workers_online": 3,
            "task_transport_ready": 3,
            "task_transport_by_platform": {
                "bilibili": {"ready": True},
                "weibo": {"ready": False},
                "xiaohongshu": {"ready": True},
                "douyin": {"ready": True},
            },
        }
        checks = {
            item["code"]: item
            for item in build_production_checks([], summary)
        }

        self.assertTrue(checks["worker_online"]["passed"])
        self.assertFalse(
            checks["all_platform_task_transports_ready"]["passed"]
        )
        self.assertIn(
            "weibo",
            checks["all_platform_task_transports_ready"]["detail"],
        )

    def test_sparse_summary_does_not_invent_runtime_gate_checks(self):
        summary = {
            "platforms_total": 1,
            "dry_run_supported": 1,
            "dry_run_ready": 1,
            "real_run_ready": 0,
        }
        codes = {
            item["code"]
            for item in build_production_checks([], summary)
        }

        self.assertTrue({
            "global_circuit_breaker_closed",
            "autopilot_heartbeat_fresh",
            "autopilot_dispatch_configured",
            "autopilot_real_run_authorized",
        }.isdisjoint(codes))


class MetricsStreamResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_disables_proxy_buffering_and_storage(self):
        response = await stream()

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(
            response.headers["x-content-type-options"],
            "nosniff",
        )


if __name__ == "__main__":
    unittest.main()
