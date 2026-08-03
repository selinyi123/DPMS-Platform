from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ALLOWED_DOUYIN_PACKAGES = frozenset({"com.ss.android.ugc.aweme"})
SUPPORTED_ACTIONS = frozenset({"follow", "like", "comment", "favorite"})
COMMENT_PLACEHOLDER = "$comment"
TARGET_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MANDATORY_RISK_TEXTS = (
    "验证码",
    "安全验证",
    "人脸",
    "滑块",
    "操作频繁",
    "账号异常",
)


class ManifestError(ValueError):
    """A calibration manifest is missing required fail-closed evidence."""


@dataclass(frozen=True)
class NodeSelector:
    """Exact uiautomator node selector.

    Every supplied field is compared with equality.  Wildcards, regular
    expressions, partial matching and coordinate fallbacks are intentionally
    unsupported.
    """

    text: str | None = None
    resource_id: str | None = None
    content_desc: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, location: str) -> "NodeSelector":
        allowed = {"text", "resource_id", "content_desc"}
        unknown = set(raw) - allowed
        if unknown:
            raise ManifestError(f"{location} has unsupported selector fields: {sorted(unknown)}")
        values: dict[str, str | None] = {}
        for key in allowed:
            value = raw.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                raise ManifestError(f"{location}.{key} must be a non-empty string")
            values[key] = value
        if all(value is None for value in values.values()):
            raise ManifestError(f"{location} must select by text, resource_id or content_desc")
        return cls(**values)

    def resolve(self, *, comment: str | None = None) -> "NodeSelector":
        def replace(value: str | None) -> str | None:
            if value != COMMENT_PLACEHOLDER:
                return value
            if not comment:
                raise ManifestError("$comment selector requires a non-empty comment")
            return comment

        return NodeSelector(
            text=replace(self.text),
            resource_id=replace(self.resource_id),
            content_desc=replace(self.content_desc),
        )


@dataclass(frozen=True)
class ActionCalibration:
    trigger: NodeSelector
    done: tuple[NodeSelector, ...]
    typed: NodeSelector | None = None
    submit: NodeSelector | None = None


@dataclass(frozen=True)
class TargetCalibration:
    """Exact, reviewed identity markers for one Douyin target page.

    The caller supplies only the pre-bound target hash and author handle.  It
    cannot supply selectors at request time.  Every selector below must match
    exactly one UI node for the identity check to pass.
    """

    markers: tuple[NodeSelector, ...]
    author_handle: str
    follow_markers: tuple[NodeSelector, ...]


@dataclass(frozen=True)
class CalibrationManifest:
    version: int
    package: str
    risk_texts: tuple[str, ...]
    actions: Mapping[str, ActionCalibration]
    target_markers: Mapping[str, TargetCalibration]
    settle_seconds: float = 1.0

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationManifest":
        manifest_path = Path(path)
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"unable to read calibration manifest: {exc}") from exc
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: Any) -> "CalibrationManifest":
        if not isinstance(raw, dict):
            raise ManifestError("calibration manifest must be a JSON object")
        allowed = {
            "version",
            "package",
            "risk_texts",
            "actions",
            "target_markers",
            "settle_seconds",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ManifestError(f"unsupported manifest fields: {sorted(unknown)}")
        if raw.get("version") != 1:
            raise ManifestError("calibration manifest version must be 1")

        package = raw.get("package")
        if package not in ALLOWED_DOUYIN_PACKAGES:
            raise ManifestError("manifest package must be the supported Douyin package")

        risk_texts_raw = raw.get("risk_texts")
        if not isinstance(risk_texts_raw, list) or not risk_texts_raw:
            raise ManifestError("risk_texts must be a non-empty list")
        risk_texts: list[str] = []
        for index, value in enumerate(risk_texts_raw):
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"risk_texts[{index}] must be a non-empty string")
            risk_texts.append(value.strip())

        settle_seconds = raw.get("settle_seconds", 1.0)
        if not isinstance(settle_seconds, (int, float)) or not (0 <= settle_seconds <= 30):
            raise ManifestError("settle_seconds must be between 0 and 30")

        actions_raw = raw.get("actions")
        if not isinstance(actions_raw, dict):
            raise ManifestError("actions must be an object")
        if set(actions_raw) != SUPPORTED_ACTIONS:
            raise ManifestError("actions must define exactly follow, like, comment and favorite")

        actions: dict[str, ActionCalibration] = {}
        for action_name in sorted(SUPPORTED_ACTIONS):
            action_raw = actions_raw[action_name]
            if not isinstance(action_raw, dict):
                raise ManifestError(f"actions.{action_name} must be an object")
            action_allowed = {"trigger", "done", "typed", "submit"}
            action_unknown = set(action_raw) - action_allowed
            if action_unknown:
                raise ManifestError(
                    f"actions.{action_name} has unsupported fields: {sorted(action_unknown)}"
                )
            trigger_raw = action_raw.get("trigger")
            if not isinstance(trigger_raw, dict):
                raise ManifestError(f"actions.{action_name}.trigger must be an object")
            trigger = NodeSelector.from_mapping(
                trigger_raw, location=f"actions.{action_name}.trigger"
            )
            done_raw = action_raw.get("done")
            if not isinstance(done_raw, list) or not done_raw:
                raise ManifestError(f"actions.{action_name}.done must be a non-empty list")
            done: list[NodeSelector] = []
            for index, selector_raw in enumerate(done_raw):
                if not isinstance(selector_raw, dict):
                    raise ManifestError(
                        f"actions.{action_name}.done[{index}] must be an object"
                    )
                done.append(
                    NodeSelector.from_mapping(
                        selector_raw,
                        location=f"actions.{action_name}.done[{index}]",
                    )
                )

            typed = None
            submit = None
            if action_name == "comment":
                typed_raw = action_raw.get("typed")
                submit_raw = action_raw.get("submit")
                if not isinstance(typed_raw, dict) or not isinstance(submit_raw, dict):
                    raise ManifestError("comment requires exact typed and submit selectors")
                typed = NodeSelector.from_mapping(
                    typed_raw, location="actions.comment.typed"
                )
                submit = NodeSelector.from_mapping(
                    submit_raw, location="actions.comment.submit"
                )
                if COMMENT_PLACEHOLDER not in (
                    typed.text,
                    typed.resource_id,
                    typed.content_desc,
                ):
                    raise ManifestError("actions.comment.typed must contain $comment")
                if not any(
                    COMMENT_PLACEHOLDER
                    in (selector.text, selector.resource_id, selector.content_desc)
                    for selector in done
                ):
                    raise ManifestError("actions.comment.done must contain $comment")
            elif "typed" in action_raw or "submit" in action_raw:
                raise ManifestError(f"actions.{action_name} cannot define typed or submit")

            actions[action_name] = ActionCalibration(
                trigger=trigger,
                done=tuple(done),
                typed=typed,
                submit=submit,
            )

        target_markers_raw = raw.get("target_markers", {})
        if not isinstance(target_markers_raw, dict):
            raise ManifestError("target_markers must be an object")
        target_markers: dict[str, TargetCalibration] = {}
        for target_hash, target_raw in target_markers_raw.items():
            if not isinstance(target_hash, str) or not TARGET_HASH_RE.fullmatch(
                target_hash
            ):
                raise ManifestError(
                    "target_markers keys must be lowercase 64-character hex hashes"
                )
            if not isinstance(target_raw, dict):
                raise ManifestError(f"target_markers.{target_hash} must be an object")
            target_allowed = {"markers", "author_handle", "follow_markers"}
            target_unknown = set(target_raw) - target_allowed
            if target_unknown:
                raise ManifestError(
                    f"target_markers.{target_hash} has unsupported fields: "
                    f"{sorted(target_unknown)}"
                )

            def parse_marker_list(field: str) -> tuple[NodeSelector, ...]:
                values = target_raw.get(field)
                if not isinstance(values, list) or not values:
                    raise ManifestError(
                        f"target_markers.{target_hash}.{field} must be a non-empty list"
                    )
                parsed: list[NodeSelector] = []
                for index, selector_raw in enumerate(values):
                    if not isinstance(selector_raw, dict):
                        raise ManifestError(
                            f"target_markers.{target_hash}.{field}[{index}] "
                            "must be an object"
                        )
                    parsed.append(
                        NodeSelector.from_mapping(
                            selector_raw,
                            location=(
                                f"target_markers.{target_hash}.{field}[{index}]"
                            ),
                        )
                    )
                return tuple(parsed)

            author_handle = target_raw.get("author_handle")
            if (
                not isinstance(author_handle, str)
                or not author_handle
                or author_handle != author_handle.strip()
                or len(author_handle) > 200
            ):
                raise ManifestError(
                    f"target_markers.{target_hash}.author_handle must be an exact "
                    "non-empty string of at most 200 characters"
                )
            target_markers[target_hash] = TargetCalibration(
                markers=parse_marker_list("markers"),
                author_handle=author_handle,
                follow_markers=parse_marker_list("follow_markers"),
            )

        return cls(
            version=1,
            package=package,
            risk_texts=tuple(dict.fromkeys((*MANDATORY_RISK_TEXTS, *risk_texts))),
            actions=actions,
            target_markers=target_markers,
            settle_seconds=float(settle_seconds),
        )
