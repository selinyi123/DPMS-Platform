from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .adb import AdbError, AdbTransport
from .calibration import (
    SUPPORTED_ACTIONS,
    TARGET_HASH_RE,
    ActionCalibration,
    CalibrationManifest,
    ManifestError,
    NodeSelector,
    TargetCalibration,
)
from .guard import (
    AccountFileLock,
    AccountLockError,
    PersistentRateLimiter,
    RateLimitPolicy,
    RateLimitStateError,
)
from .ui import UiDocumentError, UiSnapshot


class ActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    ALREADY_DONE = "already_done"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionRequest:
    action: str
    comment: str | None = None
    target_hash: str | None = None
    follow_target_handle: str | None = None

    def __post_init__(self) -> None:
        if self.action not in SUPPORTED_ACTIONS:
            raise ValueError(f"unsupported action: {self.action}")
        if self.action == "comment":
            if not self.comment or not self.comment.strip():
                raise ValueError("comment action requires exact non-empty text")
            if len(self.comment) > 500:
                raise ValueError("comment text exceeds 500 characters")
        elif self.comment is not None:
            raise ValueError("comment text is only valid for the comment action")
        if self.target_hash is not None and not TARGET_HASH_RE.fullmatch(
            self.target_hash
        ):
            raise ValueError("target_hash must be lowercase 64-character hex")
        if self.action == "follow":
            if self.target_hash is not None:
                if (
                    not isinstance(self.follow_target_handle, str)
                    or not self.follow_target_handle
                    or self.follow_target_handle != self.follow_target_handle.strip()
                    or len(self.follow_target_handle) > 200
                ):
                    raise ValueError(
                        "follow with target_hash requires an exact follow_target_handle"
                    )
            elif self.follow_target_handle is not None:
                raise ValueError("follow_target_handle requires target_hash")
        elif self.follow_target_handle is not None:
            raise ValueError("follow_target_handle is only valid for follow")


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    action: str
    reason: str
    outcome_known: bool
    halt: bool
    mutation_attempted: bool
    before_done: bool | None
    after_done: bool | None
    observed_at: float
    retry_after_seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "action": self.action,
            "reason": self.reason,
            "outcome_known": self.outcome_known,
            "halt": self.halt,
            "mutation_attempted": self.mutation_attempted,
            "before_done": self.before_done,
            "after_done": self.after_done,
            "observed_at": self.observed_at,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class SnapshotReport:
    package: str | None
    package_ok: bool
    blocked: bool
    reason: str
    risk_texts: tuple[str, ...]
    node_count: int
    xml_sha256: str | None
    action_states: Mapping[str, Mapping[str, object]]
    target_identity_verified: bool | None
    follow_target_verified: bool | None
    observed_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "package": self.package,
            "package_ok": self.package_ok,
            "blocked": self.blocked,
            "reason": self.reason,
            "risk_texts": list(self.risk_texts),
            "node_count": self.node_count,
            "xml_sha256": self.xml_sha256,
            "action_states": {
                key: dict(value) for key, value in self.action_states.items()
            },
            "target_identity_verified": self.target_identity_verified,
            "follow_target_verified": self.follow_target_verified,
            "observed_at": self.observed_at,
        }


class DeviceActionEngine:
    """Fail-closed Douyin action primitives backed by exact UI calibration."""

    def __init__(
        self,
        *,
        adb: AdbTransport,
        manifest: CalibrationManifest,
        account_id: str,
        state_dir: str | Path,
        rate_policy: RateLimitPolicy | None = None,
        lock_timeout_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not account_id.strip():
            raise ValueError("account_id must be explicitly configured")
        if not getattr(adb, "serial", "").strip():
            raise ValueError("ADB transport must expose an explicit serial")
        self.adb = adb
        self.manifest = manifest
        self.account_id = account_id.strip()
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.sleeper = sleeper
        self.lock = AccountFileLock(
            state_dir=self.state_dir,
            account_id=self.account_id,
            timeout_seconds=lock_timeout_seconds,
        )
        self.rate_limiter = PersistentRateLimiter(
            state_dir=self.state_dir,
            account_id=self.account_id,
            policy=rate_policy or RateLimitPolicy(),
            clock=clock,
        )

    def _result(
        self,
        *,
        request: ActionRequest,
        status: ActionStatus,
        reason: str,
        outcome_known: bool,
        halt: bool,
        mutation_attempted: bool = False,
        before_done: bool | None = None,
        after_done: bool | None = None,
        retry_after_seconds: float = 0.0,
    ) -> ActionResult:
        return ActionResult(
            status=status,
            action=request.action,
            reason=reason,
            outcome_known=outcome_known,
            halt=halt,
            mutation_attempted=mutation_attempted,
            before_done=before_done,
            after_done=after_done,
            observed_at=float(self.clock()),
            retry_after_seconds=max(0.0, retry_after_seconds),
        )

    def _read_ui(self) -> tuple[str, UiSnapshot, tuple[str, ...]]:
        package = self.adb.foreground_package()
        if package != self.manifest.package:
            raise UiDocumentError(
                f"foreground package mismatch: expected {self.manifest.package}, got {package}"
            )
        snapshot = UiSnapshot.parse(self.adb.dump_ui_xml())
        risks = snapshot.detect_risk(self.manifest.risk_texts)
        return package, snapshot, risks

    @staticmethod
    def _resolved_done(
        calibration: ActionCalibration, *, comment: str | None
    ) -> tuple:
        return tuple(selector.resolve(comment=comment) for selector in calibration.done)

    @staticmethod
    def _markers_verified(
        snapshot: UiSnapshot, markers: tuple[NodeSelector, ...]
    ) -> bool:
        return bool(markers) and all(
            len(snapshot.matching(selector)) == 1 for selector in markers
        )

    def _target_verification(
        self,
        snapshot: UiSnapshot,
        *,
        target_hash: str | None,
        follow_target_handle: str | None,
    ) -> tuple[bool | None, bool | None, TargetCalibration | None]:
        if target_hash is None:
            return None, None, None
        calibration = self.manifest.target_markers.get(target_hash)
        if calibration is None:
            return False, False if follow_target_handle is not None else None, None
        target_verified = self._markers_verified(snapshot, calibration.markers)
        follow_verified: bool | None = None
        if follow_target_handle is not None:
            follow_verified = (
                follow_target_handle == calibration.author_handle
                and self._markers_verified(snapshot, calibration.follow_markers)
            )
        return target_verified, follow_verified, calibration

    def snapshot(
        self,
        *,
        comment: str | None = None,
        target_hash: str | None = None,
        follow_target_handle: str | None = None,
    ) -> SnapshotReport:
        if target_hash is not None and not TARGET_HASH_RE.fullmatch(target_hash):
            raise ValueError("target_hash must be lowercase 64-character hex")
        if follow_target_handle is not None and target_hash is None:
            raise ValueError("follow_target_handle requires target_hash")
        observed_at = float(self.clock())
        try:
            package = self.adb.foreground_package()
            if package != self.manifest.package:
                return SnapshotReport(
                    package=package,
                    package_ok=False,
                    blocked=True,
                    reason="foreground_package_mismatch",
                    risk_texts=(),
                    node_count=0,
                    xml_sha256=None,
                    action_states={},
                    target_identity_verified=(False if target_hash else None),
                    follow_target_verified=(
                        False if follow_target_handle is not None else None
                    ),
                    observed_at=observed_at,
                )
            xml = self.adb.dump_ui_xml()
            snapshot = UiSnapshot.parse(xml)
        except (AdbError, UiDocumentError, ValueError) as exc:
            return SnapshotReport(
                package=None,
                package_ok=False,
                blocked=True,
                reason=f"snapshot_failed:{type(exc).__name__}",
                risk_texts=(),
                node_count=0,
                xml_sha256=None,
                action_states={},
                target_identity_verified=(False if target_hash else None),
                follow_target_verified=(
                    False if follow_target_handle is not None else None
                ),
                observed_at=observed_at,
            )

        risks = snapshot.detect_risk(self.manifest.risk_texts)
        target_verified, follow_verified, target_calibration = (
            self._target_verification(
                snapshot,
                target_hash=target_hash,
                follow_target_handle=follow_target_handle,
            )
        )
        action_states: dict[str, Mapping[str, object]] = {}
        for action_name, calibration in self.manifest.actions.items():
            trigger_matches = len(snapshot.matching(calibration.trigger))
            if action_name == "comment" and not comment:
                done: bool | None = None
            else:
                try:
                    done = snapshot.has_any(
                        self._resolved_done(calibration, comment=comment)
                    )
                except ManifestError:
                    done = None
            action_states[action_name] = {
                "trigger_matches": trigger_matches,
                "done": done,
                "exact_trigger": trigger_matches == 1,
            }
        if risks:
            reason = "risk_text_detected"
        elif target_hash is not None and target_calibration is None:
            reason = "unknown_target_hash"
        elif target_verified is False:
            reason = "target_identity_unverified"
        elif follow_verified is False:
            reason = "follow_target_unverified"
        else:
            reason = "ok"
        return SnapshotReport(
            package=package,
            package_ok=True,
            blocked=bool(risks) or target_verified is False or follow_verified is False,
            reason=reason,
            risk_texts=risks,
            node_count=len(snapshot.nodes),
            xml_sha256=hashlib.sha256(xml.encode("utf-8")).hexdigest(),
            action_states=action_states,
            target_identity_verified=target_verified,
            follow_target_verified=follow_verified,
            observed_at=observed_at,
        )

    def execute(
        self,
        request: ActionRequest,
        *,
        deadline_monotonic: float | None = None,
    ) -> ActionResult:
        try:
            with self.lock:
                return self._execute_locked(
                    request, deadline_monotonic=deadline_monotonic
                )
        except (AccountLockError, OSError) as exc:
            return self._result(
                request=request,
                status=ActionStatus.BLOCKED,
                reason=(
                    "account_lock_held"
                    if isinstance(exc, AccountLockError)
                    else "account_lock_unavailable"
                ),
                outcome_known=True,
                halt=True,
            )

    def _execute_locked(
        self,
        request: ActionRequest,
        *,
        deadline_monotonic: float | None,
    ) -> ActionResult:
        try:
            decision = self.rate_limiter.check()
        except RateLimitStateError:
            return self._result(
                request=request,
                status=ActionStatus.BLOCKED,
                reason="invalid_rate_limit_state",
                outcome_known=True,
                halt=True,
            )
        if not decision.allowed:
            return self._result(
                request=request,
                status=ActionStatus.BLOCKED,
                reason=decision.reason or "rate_limited",
                outcome_known=True,
                halt=True,
                retry_after_seconds=decision.retry_after_seconds,
            )

        calibration = self.manifest.actions[request.action]
        mutation_attempted = False
        before_done: bool | None = None
        try:
            _, before, risks = self._read_ui()
            if risks:
                self.rate_limiter.record_cooldown(blocked=True)
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason=f"risk_text_detected:{','.join(risks)}",
                    outcome_known=True,
                    halt=True,
                )

            target_verified, follow_verified, target_calibration = (
                self._target_verification(
                    before,
                    target_hash=request.target_hash,
                    follow_target_handle=request.follow_target_handle,
                )
            )
            if request.target_hash is not None and target_calibration is None:
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason="unknown_target_hash",
                    outcome_known=True,
                    halt=True,
                )
            if target_verified is False:
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason="target_identity_unverified_before_action",
                    outcome_known=True,
                    halt=True,
                )
            if follow_verified is False:
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason="follow_target_unverified_before_action",
                    outcome_known=True,
                    halt=True,
                )

            done_selectors = self._resolved_done(
                calibration, comment=request.comment
            )
            before_done = before.has_any(done_selectors)
            if before_done:
                return self._result(
                    request=request,
                    status=ActionStatus.ALREADY_DONE,
                    reason="done_state_present_before_action",
                    outcome_known=True,
                    halt=False,
                    before_done=True,
                    after_done=True,
                )

            trigger = before.unique(
                calibration.trigger, purpose=f"{request.action} trigger"
            )
            trigger_point = trigger.tap_point()

            if (
                deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason="operation_deadline_before_mutation",
                    outcome_known=True,
                    halt=True,
                    before_done=before_done,
                )

            if request.action == "comment":
                self.adb.tap(*trigger_point)
                self.adb.input_text(request.comment or "")
                if self.manifest.settle_seconds:
                    self.sleeper(self.manifest.settle_seconds)
                _, typed_snapshot, typed_risks = self._read_ui()
                if typed_risks:
                    self.rate_limiter.record_cooldown(blocked=True)
                    return self._result(
                        request=request,
                        status=ActionStatus.BLOCKED,
                        reason=f"risk_text_detected:{','.join(typed_risks)}",
                        outcome_known=True,
                        halt=True,
                        before_done=False,
                    )
                typed_target_verified, _, typed_target_calibration = (
                    self._target_verification(
                        typed_snapshot,
                        target_hash=request.target_hash,
                        follow_target_handle=None,
                    )
                )
                if (
                    request.target_hash is not None
                    and (
                        typed_target_calibration is None
                        or typed_target_verified is False
                    )
                ):
                    return self._result(
                        request=request,
                        status=ActionStatus.BLOCKED,
                        reason="target_identity_unverified_before_comment_submit",
                        outcome_known=True,
                        halt=True,
                        before_done=before_done,
                    )
                if calibration.typed is None or calibration.submit is None:
                    raise ManifestError("comment calibration is incomplete")
                typed_selector = calibration.typed.resolve(comment=request.comment)
                typed_snapshot.unique(typed_selector, purpose="typed comment read-back")
                submit = typed_snapshot.unique(
                    calibration.submit, purpose="comment submit"
                )
                submit_point = submit.tap_point()
                if (
                    deadline_monotonic is not None
                    and time.monotonic() >= deadline_monotonic
                ):
                    return self._result(
                        request=request,
                        status=ActionStatus.BLOCKED,
                        reason="operation_deadline_before_comment_submit",
                        outcome_known=True,
                        halt=True,
                        before_done=before_done,
                    )
                self.adb.tap(*submit_point)
                mutation_attempted = True
            else:
                self.adb.tap(*trigger_point)
                mutation_attempted = True

            self.rate_limiter.record_mutation()
            if self.manifest.settle_seconds:
                self.sleeper(self.manifest.settle_seconds)
            _, after, after_risks = self._read_ui()
            if after_risks:
                self.rate_limiter.record_cooldown(blocked=True)
                return self._result(
                    request=request,
                    status=ActionStatus.BLOCKED,
                    reason=f"risk_text_detected:{','.join(after_risks)}",
                    outcome_known=False,
                    halt=True,
                    mutation_attempted=mutation_attempted,
                    before_done=before_done,
                    after_done=None,
                )
            after_target_verified, after_follow_verified, _ = (
                self._target_verification(
                    after,
                    target_hash=request.target_hash,
                    follow_target_handle=request.follow_target_handle,
                )
            )
            if after_target_verified is False or after_follow_verified is False:
                self.rate_limiter.record_cooldown(blocked=False)
                return self._result(
                    request=request,
                    status=ActionStatus.UNKNOWN,
                    reason=(
                        "follow_target_unverified_after_action"
                        if after_follow_verified is False
                        else "target_identity_unverified_after_action"
                    ),
                    outcome_known=False,
                    halt=True,
                    mutation_attempted=mutation_attempted,
                    before_done=before_done,
                    after_done=None,
                )
            after_done = after.has_any(done_selectors)
            if not after_done:
                self.rate_limiter.record_cooldown(blocked=False)
                return self._result(
                    request=request,
                    status=ActionStatus.UNKNOWN,
                    reason="done_state_missing_after_action",
                    outcome_known=False,
                    halt=True,
                    mutation_attempted=mutation_attempted,
                    before_done=before_done,
                    after_done=False,
                )
            return self._result(
                request=request,
                status=ActionStatus.SUCCEEDED,
                reason="done_state_confirmed",
                outcome_known=True,
                halt=False,
                mutation_attempted=mutation_attempted,
                before_done=before_done,
                after_done=True,
            )
        except (
            AdbError,
            UiDocumentError,
            ManifestError,
            RateLimitStateError,
            OSError,
            ValueError,
        ) as exc:
            if mutation_attempted:
                try:
                    self.rate_limiter.record_cooldown(blocked=False)
                except (RateLimitStateError, OSError):
                    pass
                return self._result(
                    request=request,
                    status=ActionStatus.UNKNOWN,
                    reason=f"unconfirmed_after_mutation:{type(exc).__name__}",
                    outcome_known=False,
                    halt=True,
                    mutation_attempted=True,
                    before_done=before_done,
                )
            return self._result(
                request=request,
                status=ActionStatus.BLOCKED,
                reason=f"precondition_failed:{type(exc).__name__}",
                outcome_known=True,
                halt=True,
                mutation_attempted=False,
                before_done=before_done,
            )
