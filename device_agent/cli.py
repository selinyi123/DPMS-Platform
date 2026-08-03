from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .adb import AdbError, SubprocessAdb
from .calibration import CalibrationManifest, ManifestError, SUPPORTED_ACTIONS
from .engine import ActionRequest, ActionStatus, DeviceActionEngine
from .guard import RateLimitPolicy
from .http_service import DeviceAgentHttpService, create_http_server


class FixtureAdb:
    """Non-device transport used by the dry fixture runner.

    Each tap or text-input operation advances to the next supplied XML frame.
    A click action normally needs two frames.  A comment action normally needs
    four: before, focused, typed, and submitted/done.
    """

    def __init__(self, *, package: str, frames: Sequence[str]) -> None:
        if not frames:
            raise ValueError("at least one fixture frame is required")
        self.package = package
        self.frames = tuple(frames)
        self.index = 0
        self.serial = "fixture:no-device"
        self.operations: list[str] = []

    def foreground_package(self) -> str:
        return self.package

    def dump_ui_xml(self) -> str:
        return self.frames[self.index]

    def _advance(self) -> None:
        if self.index + 1 < len(self.frames):
            self.index += 1

    def tap(self, x: int, y: int) -> None:
        self.operations.append(f"tap:{x},{y}")
        self._advance()

    def input_text(self, value: str) -> None:
        self.operations.append(f"input_text:length={len(value)}")
        self._advance()

    def health(self) -> bool:
        return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m device_agent",
        description="Fail-closed Douyin Android device primitives",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fixture = subparsers.add_parser(
        "fixture", help="dry-run an action against ordered uiautomator XML fixtures"
    )
    fixture.add_argument("--manifest", required=True, type=Path)
    fixture.add_argument("--account-id", required=True)
    fixture.add_argument("--state-dir", required=True, type=Path)
    fixture.add_argument("--action", required=True, choices=sorted(SUPPORTED_ACTIONS))
    fixture.add_argument("--comment")
    fixture.add_argument(
        "--frame",
        action="append",
        required=True,
        type=Path,
        help="ordered XML frame; repeat once per simulated UI transition",
    )

    snapshot = subparsers.add_parser(
        "snapshot", help="capture one read-only snapshot from an explicitly configured device"
    )
    snapshot.add_argument("--manifest", required=True, type=Path)
    snapshot.add_argument("--adb-path", required=True, type=Path)
    snapshot.add_argument("--serial", required=True)
    snapshot.add_argument("--account-id", required=True)
    snapshot.add_argument("--state-dir", required=True, type=Path)
    snapshot.add_argument("--timeout-seconds", type=float, default=15.0)
    snapshot.add_argument("--comment")

    serve = subparsers.add_parser(
        "serve",
        help="run the authenticated, loopback-only device HTTP service",
    )
    serve.add_argument("--manifest", required=True, type=Path)
    serve.add_argument("--adb-path", required=True, type=Path)
    serve.add_argument("--serial", required=True)
    serve.add_argument("--account-id", required=True)
    serve.add_argument("--state-dir", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--adb-timeout-seconds", type=float, default=15.0)
    serve.add_argument("--operation-timeout-seconds", type=float, default=60.0)
    serve.add_argument("--request-body-limit-bytes", type=int, default=16 * 1024)
    serve.add_argument(
        "--bearer-token",
        help=(
            "explicit bearer token; prefer the "
            "DPMS_DEVICE_AGENT_BEARER_TOKEN environment variable"
        ),
    )
    return parser


def _fixture(args: argparse.Namespace) -> int:
    manifest = CalibrationManifest.load(args.manifest)
    frames = [path.read_text(encoding="utf-8") for path in args.frame]
    adb = FixtureAdb(package=manifest.package, frames=frames)
    engine = DeviceActionEngine(
        adb=adb,
        manifest=manifest,
        account_id=args.account_id,
        state_dir=args.state_dir,
        rate_policy=RateLimitPolicy(
            min_interval_seconds=0,
            max_actions_per_hour=1000,
            unknown_cooldown_seconds=0,
            blocked_cooldown_seconds=0,
        ),
        sleeper=lambda _seconds: None,
    )
    result = engine.execute(ActionRequest(action=args.action, comment=args.comment))
    print(
        json.dumps(
            {"fixture": True, "result": result.to_dict(), "operations": adb.operations},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.status in {ActionStatus.SUCCEEDED, ActionStatus.ALREADY_DONE} else 2


def _snapshot(args: argparse.Namespace) -> int:
    manifest = CalibrationManifest.load(args.manifest)
    adb = SubprocessAdb(
        executable=args.adb_path,
        serial=args.serial,
        timeout_seconds=args.timeout_seconds,
    )
    engine = DeviceActionEngine(
        adb=adb,
        manifest=manifest,
        account_id=args.account_id,
        state_dir=args.state_dir,
    )
    report = engine.snapshot(comment=args.comment)
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 2 if report.blocked else 0


def _serve(args: argparse.Namespace) -> int:
    token = args.bearer_token or os.environ.get("DPMS_DEVICE_AGENT_BEARER_TOKEN")
    if not token:
        raise ValueError(
            "bearer token is required via --bearer-token or "
            "DPMS_DEVICE_AGENT_BEARER_TOKEN"
        )
    manifest_bytes = args.manifest.read_bytes()
    manifest = CalibrationManifest.load(args.manifest)
    adb = SubprocessAdb(
        executable=args.adb_path,
        serial=args.serial,
        timeout_seconds=args.adb_timeout_seconds,
    )
    engine = DeviceActionEngine(
        adb=adb,
        manifest=manifest,
        account_id=args.account_id,
        state_dir=args.state_dir,
    )
    service = DeviceAgentHttpService(
        engine=engine,
        bearer_token=token,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        request_body_limit_bytes=args.request_body_limit_bytes,
        operation_timeout_seconds=args.operation_timeout_seconds,
    )
    server = create_http_server(service=service, port=args.port)
    try:
        print(
            json.dumps(
                {
                    "status": "listening",
                    "host": server.server_address[0],
                    "port": server.server_address[1],
                    "agent_id": service.agent_id,
                    "manifest_sha256": service.manifest_sha256,
                    "device_serial_sha256": service.device_serial_sha256,
                    "account_id_sha256": service.account_id_sha256,
                    "supported_actions": sorted(SUPPORTED_ACTIONS),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        service.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fixture":
            return _fixture(args)
        if args.command == "snapshot":
            return _snapshot(args)
        if args.command == "serve":
            return _serve(args)
    except (ManifestError, AdbError, OSError, UnicodeError, ValueError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    parser.error("unknown command")
    return 2
