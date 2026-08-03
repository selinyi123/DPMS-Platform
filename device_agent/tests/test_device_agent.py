from __future__ import annotations

import contextlib
import io
import json
import queue
import subprocess
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from device_agent.adb import SubprocessAdb
from device_agent.calibration import CalibrationManifest, ManifestError
from device_agent.cli import main as cli_main
from device_agent.engine import (
    ActionRequest,
    ActionStatus,
    DeviceActionEngine,
)
from device_agent.guard import AccountFileLock, AccountLockError, RateLimitPolicy
from device_agent.loop import HealthHeartbeat, QueueCommandSource, ResidentDeviceLoop


DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"


def node(
    *,
    text: str = "",
    resource_id: str = "",
    content_desc: str = "",
    bounds: str = "[10,20][110,120]",
    enabled: bool = True,
    clickable: bool = True,
) -> dict[str, str]:
    return {
        "text": text,
        "resource-id": resource_id,
        "content-desc": content_desc,
        "bounds": bounds,
        "enabled": str(enabled).lower(),
        "clickable": str(clickable).lower(),
    }


def hierarchy(*nodes: dict[str, str]) -> str:
    root = ET.Element("hierarchy", {"rotation": "0"})
    for attributes in nodes:
        ET.SubElement(root, "node", attributes)
    return ET.tostring(root, encoding="unicode")


def manifest_mapping(*, settle_seconds: float = 0) -> dict[str, object]:
    return {
        "version": 1,
        "package": DOUYIN_PACKAGE,
        "risk_texts": ["安全验证", "操作频繁", "人脸验证", "滑块验证"],
        "settle_seconds": settle_seconds,
        "actions": {
            "follow": {
                "trigger": {"resource_id": "follow", "text": "关注"},
                "done": [{"resource_id": "follow", "text": "已关注"}],
            },
            "like": {
                "trigger": {"resource_id": "like", "content_desc": "未点赞"},
                "done": [{"resource_id": "like", "content_desc": "已点赞"}],
            },
            "comment": {
                "trigger": {"resource_id": "comment-input"},
                "typed": {"resource_id": "comment-input", "text": "$comment"},
                "submit": {"resource_id": "comment-submit", "text": "发送"},
                "done": [{"resource_id": "comment-item", "text": "$comment"}],
            },
            "favorite": {
                "trigger": {"resource_id": "favorite", "content_desc": "收藏"},
                "done": [
                    {"resource_id": "favorite", "content_desc": "已收藏"}
                ],
            },
        },
    }


class FakeAdb:
    def __init__(self, frames: list[str], *, package: str = DOUYIN_PACKAGE) -> None:
        self.frames = frames
        self.package = package
        self.index = 0
        self.serial = "FAKE-SERIAL"
        self.operations: list[tuple[object, ...]] = []
        self.is_healthy = True

    def foreground_package(self) -> str:
        return self.package

    def dump_ui_xml(self) -> str:
        return self.frames[self.index]

    def _advance(self) -> None:
        if self.index + 1 < len(self.frames):
            self.index += 1

    def tap(self, x: int, y: int) -> None:
        self.operations.append(("tap", x, y))
        self._advance()

    def input_text(self, value: str) -> None:
        self.operations.append(("input_text", value))
        self._advance()

    def health(self) -> bool:
        return self.is_healthy


class DeviceAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = CalibrationManifest.from_mapping(manifest_mapping())
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.policy = RateLimitPolicy(
            min_interval_seconds=0,
            max_actions_per_hour=100,
            unknown_cooldown_seconds=30,
            blocked_cooldown_seconds=60,
        )

    def engine(self, adb: FakeAdb, *, account_id: str = "account-a") -> DeviceActionEngine:
        return DeviceActionEngine(
            adb=adb,
            manifest=self.manifest,
            account_id=account_id,
            state_dir=self.state_dir,
            rate_policy=self.policy,
            sleeper=lambda _seconds: None,
        )

    def test_read_only_snapshot_does_not_invoke_input(self) -> None:
        adb = FakeAdb(
            [hierarchy(node(resource_id="like", content_desc="未点赞"))]
        )
        report = self.engine(adb).snapshot()
        self.assertTrue(report.package_ok)
        self.assertFalse(report.blocked)
        self.assertEqual(report.action_states["like"]["trigger_matches"], 1)
        self.assertEqual(adb.operations, [])

    def test_like_reads_before_and_confirms_done_after(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(node(resource_id="like", content_desc="未点赞")),
                hierarchy(node(resource_id="like", content_desc="已点赞")),
            ]
        )
        result = self.engine(adb).execute(ActionRequest("like"))
        self.assertEqual(result.status, ActionStatus.SUCCEEDED)
        self.assertTrue(result.outcome_known)
        self.assertFalse(result.halt)
        self.assertEqual(adb.operations, [("tap", 60, 70)])

    def test_already_done_is_no_op(self) -> None:
        adb = FakeAdb(
            [hierarchy(node(resource_id="favorite", content_desc="已收藏"))]
        )
        result = self.engine(adb).execute(ActionRequest("favorite"))
        self.assertEqual(result.status, ActionStatus.ALREADY_DONE)
        self.assertEqual(adb.operations, [])

    def test_comment_requires_exact_typed_readback_before_submit(self) -> None:
        comment = "精准评论"
        adb = FakeAdb(
            [
                hierarchy(node(resource_id="comment-input")),
                hierarchy(node(resource_id="comment-input")),
                hierarchy(
                    node(resource_id="comment-input", text=comment),
                    node(resource_id="comment-submit", text="发送"),
                ),
                hierarchy(node(resource_id="comment-item", text=comment)),
            ]
        )
        result = self.engine(adb).execute(ActionRequest("comment", comment=comment))
        self.assertEqual(result.status, ActionStatus.SUCCEEDED)
        self.assertEqual(
            adb.operations,
            [("tap", 60, 70), ("input_text", comment), ("tap", 60, 70)],
        )

    def test_comment_does_not_submit_when_typed_readback_differs(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(node(resource_id="comment-input")),
                hierarchy(node(resource_id="comment-input")),
                hierarchy(
                    node(resource_id="comment-input", text="错误文本"),
                    node(resource_id="comment-submit", text="发送"),
                ),
            ]
        )
        result = self.engine(adb).execute(
            ActionRequest("comment", comment="期望文本")
        )
        self.assertEqual(result.status, ActionStatus.BLOCKED)
        self.assertTrue(result.halt)
        self.assertFalse(result.mutation_attempted)
        self.assertEqual(len(adb.operations), 2)

    def test_missing_done_state_is_unknown_and_halts(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(node(resource_id="follow", text="关注")),
                hierarchy(node(resource_id="follow", text="关注")),
            ]
        )
        result = self.engine(adb).execute(ActionRequest("follow"))
        self.assertEqual(result.status, ActionStatus.UNKNOWN)
        self.assertFalse(result.outcome_known)
        self.assertTrue(result.halt)
        self.assertTrue(result.mutation_attempted)

    def test_risk_text_blocks_without_mutation(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(
                    node(resource_id="like", content_desc="未点赞"),
                    node(text="请完成安全验证", clickable=False),
                )
            ]
        )
        result = self.engine(adb).execute(ActionRequest("like"))
        self.assertEqual(result.status, ActionStatus.BLOCKED)
        self.assertIn("risk_text_detected", result.reason)
        self.assertEqual(adb.operations, [])

    def test_wrong_foreground_package_blocks_without_dump_mutation(self) -> None:
        adb = FakeAdb(
            [hierarchy(node(resource_id="like", content_desc="未点赞"))],
            package="com.example.other",
        )
        result = self.engine(adb).execute(ActionRequest("like"))
        self.assertEqual(result.status, ActionStatus.BLOCKED)
        self.assertEqual(adb.operations, [])

    def test_ambiguous_exact_trigger_blocks_without_coordinate_guess(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(
                    node(resource_id="like", content_desc="未点赞"),
                    node(
                        resource_id="like",
                        content_desc="未点赞",
                        bounds="[200,20][300,120]",
                    ),
                )
            ]
        )
        result = self.engine(adb).execute(ActionRequest("like"))
        self.assertEqual(result.status, ActionStatus.BLOCKED)
        self.assertEqual(adb.operations, [])

    def test_manifest_rejects_non_douyin_package_and_wildcard_field(self) -> None:
        wrong_package = manifest_mapping()
        wrong_package["package"] = "com.example.other"
        with self.assertRaises(ManifestError):
            CalibrationManifest.from_mapping(wrong_package)

        wildcard = manifest_mapping()
        wildcard["actions"]["like"]["trigger"] = {"text_regex": ".*"}  # type: ignore[index]
        with self.assertRaises(ManifestError):
            CalibrationManifest.from_mapping(wildcard)

    def test_account_file_lock_prevents_concurrent_owner(self) -> None:
        first = AccountFileLock(
            state_dir=self.state_dir, account_id="same-account", timeout_seconds=0
        )
        second = AccountFileLock(
            state_dir=self.state_dir, account_id="same-account", timeout_seconds=0
        )
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(AccountLockError):
            second.acquire()

    def test_persisted_minimum_interval_blocks_second_action(self) -> None:
        policy = RateLimitPolicy(
            min_interval_seconds=60,
            max_actions_per_hour=100,
            unknown_cooldown_seconds=30,
            blocked_cooldown_seconds=60,
        )
        first_adb = FakeAdb(
            [
                hierarchy(node(resource_id="like", content_desc="未点赞")),
                hierarchy(node(resource_id="like", content_desc="已点赞")),
            ]
        )
        first_engine = DeviceActionEngine(
            adb=first_adb,
            manifest=self.manifest,
            account_id="rate-account",
            state_dir=self.state_dir,
            rate_policy=policy,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(
            first_engine.execute(ActionRequest("like")).status,
            ActionStatus.SUCCEEDED,
        )
        second_adb = FakeAdb(
            [hierarchy(node(resource_id="follow", text="关注"))]
        )
        second_engine = DeviceActionEngine(
            adb=second_adb,
            manifest=self.manifest,
            account_id="rate-account",
            state_dir=self.state_dir,
            rate_policy=policy,
            sleeper=lambda _seconds: None,
        )
        second_result = second_engine.execute(ActionRequest("follow"))
        self.assertEqual(second_result.status, ActionStatus.BLOCKED)
        self.assertEqual(second_result.reason, "minimum_interval")
        self.assertGreater(second_result.retry_after_seconds, 0)

    def test_subprocess_adb_uses_argument_list_serial_and_timeout(self) -> None:
        executable = self.state_dir / "adb.exe"
        executable.write_bytes(b"fixture")
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "device\n", "")

        adb = SubprocessAdb(
            executable=executable,
            serial="SERIAL-123",
            timeout_seconds=7,
            runner=runner,
        )
        self.assertTrue(adb.health())
        command, kwargs = calls[0]
        self.assertEqual(
            command,
            [str(executable), "-s", "SERIAL-123", "get-state"],
        )
        self.assertEqual(kwargs["timeout"], 7.0)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertNotIn("shell", kwargs)

    def test_resident_loop_writes_heartbeat(self) -> None:
        adb = FakeAdb(
            [
                hierarchy(node(resource_id="like", content_desc="未点赞")),
                hierarchy(node(resource_id="like", content_desc="已点赞")),
            ]
        )
        commands: queue.Queue[ActionRequest] = queue.Queue()
        commands.put(ActionRequest("like"))
        heartbeat_path = self.state_dir / "health.json"
        loop = ResidentDeviceLoop(
            engine=self.engine(adb, account_id="loop-account"),
            source=QueueCommandSource(commands),
            heartbeat=HealthHeartbeat(
                path=heartbeat_path,
                account_id="loop-account",
                serial=adb.serial,
            ),
            poll_seconds=0.05,
        )
        result = loop.run(stop_event=threading.Event(), max_iterations=1)
        self.assertIsNotNone(result)
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "stopped")
        self.assertTrue(payload["healthy"])
        self.assertNotIn("loop-account", heartbeat_path.read_text(encoding="utf-8"))

    def test_resident_loop_preserves_unhealthy_terminal_heartbeat(self) -> None:
        adb = FakeAdb(
            [hierarchy(node(resource_id="like", content_desc="未点赞"))]
        )
        adb.is_healthy = False
        heartbeat_path = self.state_dir / "unhealthy-health.json"
        loop = ResidentDeviceLoop(
            engine=self.engine(adb, account_id="unhealthy-account"),
            source=QueueCommandSource(queue.Queue()),
            heartbeat=HealthHeartbeat(
                path=heartbeat_path,
                account_id="unhealthy-account",
                serial=adb.serial,
            ),
            poll_seconds=0.05,
        )
        self.assertIsNone(loop.run(max_iterations=1))
        payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "device_unhealthy")
        self.assertFalse(payload["healthy"])

    def test_fixture_cli_runs_without_adb(self) -> None:
        manifest_path = self.state_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest_mapping(), ensure_ascii=False), encoding="utf-8"
        )
        before_path = self.state_dir / "before.xml"
        after_path = self.state_dir / "after.xml"
        before_path.write_text(
            hierarchy(node(resource_id="like", content_desc="未点赞")),
            encoding="utf-8",
        )
        after_path.write_text(
            hierarchy(node(resource_id="like", content_desc="已点赞")),
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli_main(
                [
                    "fixture",
                    "--manifest",
                    str(manifest_path),
                    "--account-id",
                    "fixture-account",
                    "--state-dir",
                    str(self.state_dir / "fixture-state"),
                    "--action",
                    "like",
                    "--frame",
                    str(before_path),
                    "--frame",
                    str(after_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["fixture"])
        self.assertEqual(payload["result"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
