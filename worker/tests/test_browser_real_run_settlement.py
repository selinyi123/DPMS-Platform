import asyncio
import unittest

from app import task_runner


class FakePage:
    def __init__(self, *, close_raises=False):
        self.url = "https://weibo.com/123456/AbCdEf1"
        self.close_raises = close_raises

    async def goto(self, url, **_kwargs):
        self.url = url

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def close(self):
        if self.close_raises:
            raise RuntimeError("page close failed")
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


class FakePool:
    def __init__(self, page):
        self.context = FakeContext(page)

    async def get_account_context(self, *_args):
        return self.context


class ClickThenFailAdapter:
    async def _follow(self, _page):
        return None

    async def _like(self, _page):
        raise RuntimeError("verification failed after click")

    async def _comment(self, _page):
        return None

    async def _repost(self, _page):
        return None


class SuccessfulAdapter(ClickThenFailAdapter):
    async def _like(self, _page):
        return None


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class EmergencyStopDatabase:
    def __init__(
        self,
        *,
        fail_global_breaker=False,
        fail_runtime_setting=False,
        fail_lease_revoke=False,
    ):
        self.fail_global_breaker = fail_global_breaker
        self.fail_runtime_setting = fail_runtime_setting
        self.fail_lease_revoke = fail_lease_revoke
        self.global_breaker_status = "closed"
        self.real_run_enabled = "true"
        self.task = {
            "status": "running",
            "worker_id": "worker-emergency",
            "lease_active": 1,
        }

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, values=None):
        normalized = " ".join(query.lower().split())
        if "insert into circuit_breakers" in normalized:
            if self.fail_global_breaker:
                raise RuntimeError("global breaker unavailable")
            self.global_breaker_status = "open"
        elif "insert into runtime_settings" in normalized:
            if self.fail_runtime_setting:
                raise RuntimeError("runtime settings unavailable")
            self.real_run_enabled = "false"
        elif "update task_runs" in normalized:
            if self.fail_lease_revoke:
                raise RuntimeError("task lease unavailable")
            self.task["worker_id"] = None
            self.task["lease_active"] = 0
        return 1

    async def fetch_one(self, query, _values=None):
        normalized = " ".join(query.lower().split())
        if "from circuit_breakers" in normalized:
            return {"status": self.global_breaker_status}
        if "from runtime_settings" in normalized:
            return {"setting_value": self.real_run_enabled}
        if "from task_runs" in normalized:
            return dict(self.task)
        return None


class BrowserRealRunSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.originals = {
            name: getattr(task_runner, name)
            for name in (
                "get_latest_phase",
                "prepare_account_login",
                "install_main_frame_navigation_guard",
                "validated_platform_navigation_url",
                "refresh_task_lease",
                "detect_page_risk",
                "enforce_task_real_run_gate",
                "quarantine_external_action_outcome",
                "capture_failure_screenshot",
                "save_phase",
            )
        }
        self.quarantines = []

        async def no_op(*_args, **_kwargs):
            return None

        async def latest(_task_id):
            return "init"

        async def quarantine(**kwargs):
            self.quarantines.append(kwargs)

        task_runner.get_latest_phase = latest
        task_runner.prepare_account_login = no_op
        task_runner.install_main_frame_navigation_guard = no_op
        task_runner.validated_platform_navigation_url = lambda _platform, url: url
        task_runner.refresh_task_lease = no_op
        task_runner.detect_page_risk = no_op
        task_runner.enforce_task_real_run_gate = no_op
        task_runner.quarantine_external_action_outcome = quarantine
        task_runner.capture_failure_screenshot = no_op
        task_runner.save_phase = no_op

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(task_runner, name, value)

    async def test_phase_exception_after_possible_click_is_quarantined(self):
        task = {
            "task_id": "task-browser-unknown",
            "account_id": "9001",
            "lottery_id": "7001",
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/AbCdEf1",
            "canonical_url": "canonical://weibo/status/AbCdEf1",
            "action_plan": {"required_actions": ["liked"], "review_required": False},
        }

        with self.assertRaises(task_runner.ExternalActionOutcomeUnknown) as caught:
            await task_runner.execute_real_task(
                task,
                ClickThenFailAdapter(),
                FakePool(FakePage()),
            )

        self.assertEqual(caught.exception.platform, "weibo")
        self.assertEqual(caught.exception.action, "liked")
        self.assertEqual(len(self.quarantines), 1)
        self.assertEqual(self.quarantines[0]["action"], "liked")

    async def test_phase_persistence_failure_after_click_is_quarantined(self):
        async def fail_phase(_task_id, _account_id, _lottery_id, phase):
            if phase == "liked":
                raise RuntimeError("phase database unavailable")

        task_runner.save_phase = fail_phase
        task = {
            "task_id": "task-browser-settlement",
            "account_id": "9001",
            "lottery_id": "7001",
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/AbCdEf1",
            "canonical_url": "canonical://weibo/status/AbCdEf1",
            "action_plan": {"required_actions": ["liked"], "review_required": False},
        }

        with self.assertRaises(task_runner.ExternalActionOutcomeUnknown):
            await task_runner.execute_real_task(task, SuccessfulAdapter(), FakePool(FakePage()))

        self.assertEqual(len(self.quarantines), 1)
        self.assertEqual(self.quarantines[0]["action"], "liked")

    async def test_page_close_failure_does_not_reverse_settled_actions(self):
        task = {
            "task_id": "task-browser-close",
            "account_id": "9001",
            "lottery_id": "7001",
            "platform": "weibo",
            "raw_url": "https://weibo.com/123456/AbCdEf1",
            "canonical_url": "canonical://weibo/status/AbCdEf1",
            "action_plan": {"required_actions": ["liked"], "review_required": False},
        }

        await task_runner.execute_real_task(
            task,
            SuccessfulAdapter(),
            FakePool(FakePage(close_raises=True)),
        )

        self.assertEqual(self.quarantines, [])


class TaskTerminalSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_outcome_without_durable_breaker_remains_unsettled(self):
        original_breaker = task_runner.open_unknown_outcome_breaker
        original_emergency = task_runner.emergency_stop_real_runs_and_revoke_lease
        original_status = task_runner.set_account_status
        emergency_calls = []

        async def fail_breaker(**_kwargs):
            raise RuntimeError("breaker database unavailable")

        async def record_status(*_args, **_kwargs):
            return None

        async def record_emergency(**kwargs):
            emergency_calls.append(kwargs)
            return "global_breaker"

        task_runner.open_unknown_outcome_breaker = fail_breaker
        task_runner.emergency_stop_real_runs_and_revoke_lease = record_emergency
        task_runner.set_account_status = record_status
        try:
            with self.assertRaises(task_runner.TaskSettlementUnconfirmed):
                await task_runner.quarantine_external_action_outcome(
                    task_id="task-unsettled-breaker",
                    account_id=9001,
                    platform="weibo",
                    action="liked",
                    cause=RuntimeError("click outcome unknown"),
                )
        finally:
            task_runner.open_unknown_outcome_breaker = original_breaker
            task_runner.emergency_stop_real_runs_and_revoke_lease = original_emergency
            task_runner.set_account_status = original_status

        self.assertEqual(len(emergency_calls), 1)
        self.assertEqual(emergency_calls[0]["task_id"], "task-unsettled-breaker")

    async def test_failed_terminal_write_raises_so_stream_message_is_not_acked(self):
        original_gate = task_runner.enforce_task_real_run_gate
        original_finish = task_runner.mark_task_finished
        original_database = task_runner.database

        async def fail_gate(*_args, **_kwargs):
            raise RuntimeError("gate unavailable")

        async def fail_finish(*_args, **_kwargs):
            raise RuntimeError("task database unavailable")

        class FailingLookupDatabase:
            async def fetch_one(self, *_args, **_kwargs):
                raise RuntimeError("task database unavailable")

        task_runner.enforce_task_real_run_gate = fail_gate
        task_runner.mark_task_finished = fail_finish
        task_runner.database = FailingLookupDatabase()
        try:
            with self.assertRaises(task_runner.TaskSettlementUnconfirmed):
                await task_runner.execute_task_with_phases(
                    {
                        "task_id": "task-unsettled",
                        "account_id": "9001",
                        "lottery_id": "7001",
                        "platform": "weibo",
                        "mode": "real_run",
                        "raw_url": "https://weibo.com/123456/AbCdEf1",
                        "canonical_url": "canonical://weibo/status/AbCdEf1",
                        "action_plan": {
                            "required_actions": ["liked"],
                            "review_required": False,
                        },
                    },
                    SuccessfulAdapter(),
                    pool=None,
                )
        finally:
            task_runner.enforce_task_real_run_gate = original_gate
            task_runner.mark_task_finished = original_finish
            task_runner.database = original_database


class EmergencyStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_database = task_runner.database
        self.original_worker_id = task_runner.WORKER_ID
        task_runner.WORKER_ID = "worker-emergency"

    async def asyncTearDown(self):
        task_runner.database = self.original_database
        task_runner.WORKER_ID = self.original_worker_id

    async def test_global_breaker_is_verified_and_active_lease_is_revoked(self):
        fake_db = EmergencyStopDatabase()
        task_runner.database = fake_db

        barrier = await task_runner.emergency_stop_real_runs_and_revoke_lease(
            task_id="task-emergency-global",
            platform="weibo",
            action="liked",
        )

        self.assertEqual(barrier, "global_breaker")
        self.assertEqual(fake_db.global_breaker_status, "open")
        self.assertEqual(fake_db.real_run_enabled, "true")
        self.assertIsNone(fake_db.task["worker_id"])
        self.assertEqual(fake_db.task["lease_active"], 0)

    async def test_runtime_setting_is_fallback_and_active_lease_is_revoked(self):
        fake_db = EmergencyStopDatabase(fail_global_breaker=True)
        task_runner.database = fake_db

        barrier = await task_runner.emergency_stop_real_runs_and_revoke_lease(
            task_id="task-emergency-setting",
            platform="weibo",
            action="liked",
        )

        self.assertEqual(barrier, "runtime_setting")
        self.assertEqual(fake_db.real_run_enabled, "false")
        self.assertIsNone(fake_db.task["worker_id"])
        self.assertEqual(fake_db.task["lease_active"], 0)

    async def test_revoke_clears_a_reassigned_active_lease_after_global_stop(self):
        fake_db = EmergencyStopDatabase()
        fake_db.task["worker_id"] = "replacement-worker"
        task_runner.database = fake_db

        barrier = await task_runner.emergency_stop_real_runs_and_revoke_lease(
            task_id="task-emergency-reassigned",
            platform="weibo",
            action="liked",
        )

        self.assertEqual(barrier, "global_breaker")
        self.assertIsNone(fake_db.task["worker_id"])
        self.assertEqual(fake_db.task["lease_active"], 0)

    async def test_missing_global_barrier_still_revokes_lease_and_raises(self):
        fake_db = EmergencyStopDatabase(
            fail_global_breaker=True,
            fail_runtime_setting=True,
        )
        task_runner.database = fake_db

        with self.assertRaisesRegex(RuntimeError, "emergency_global_stop_not_persisted"):
            await task_runner.emergency_stop_real_runs_and_revoke_lease(
                task_id="task-emergency-no-barrier",
                platform="weibo",
                action="liked",
            )

        self.assertIsNone(fake_db.task["worker_id"])
        self.assertEqual(fake_db.task["lease_active"], 0)

    async def test_barrier_without_lease_revocation_is_reported_as_failure(self):
        fake_db = EmergencyStopDatabase(fail_lease_revoke=True)
        task_runner.database = fake_db

        with self.assertRaisesRegex(RuntimeError, "emergency_task_lease_revoke_failed"):
            await task_runner.emergency_stop_real_runs_and_revoke_lease(
                task_id="task-emergency-lease",
                platform="weibo",
                action="liked",
            )

        self.assertEqual(fake_db.global_breaker_status, "open")
        self.assertEqual(fake_db.task["worker_id"], "worker-emergency")
        self.assertEqual(fake_db.task["lease_active"], 1)


if __name__ == "__main__":
    unittest.main()
