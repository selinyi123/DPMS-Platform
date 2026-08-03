from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

from .calibration import NodeSelector


class UiDocumentError(ValueError):
    """The UI hierarchy cannot be parsed safely."""


_BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_MAX_XML_CHARS = 5 * 1024 * 1024


@dataclass(frozen=True)
class UiNode:
    text: str
    resource_id: str
    content_desc: str
    bounds: tuple[int, int, int, int] | None
    enabled: bool
    clickable: bool

    def matches(self, selector: NodeSelector) -> bool:
        return (
            (selector.text is None or self.text == selector.text)
            and (selector.resource_id is None or self.resource_id == selector.resource_id)
            and (
                selector.content_desc is None
                or self.content_desc == selector.content_desc
            )
        )

    def tap_point(self) -> tuple[int, int]:
        if not self.enabled:
            raise UiDocumentError("matched UI node is disabled")
        if not self.clickable:
            raise UiDocumentError("matched UI node is not explicitly clickable")
        if self.bounds is None:
            raise UiDocumentError("matched UI node has no valid bounds")
        left, top, right, bottom = self.bounds
        if right <= left or bottom <= top:
            raise UiDocumentError("matched UI node has empty bounds")
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass(frozen=True)
class UiSnapshot:
    nodes: tuple[UiNode, ...]
    xml: str

    @classmethod
    def parse(cls, xml: str) -> "UiSnapshot":
        if not isinstance(xml, str) or not xml.strip():
            raise UiDocumentError("UI hierarchy XML is empty")
        if len(xml) > _MAX_XML_CHARS:
            raise UiDocumentError("UI hierarchy XML exceeds the safety limit")
        upper_prefix = xml[:2048].upper()
        if "<!DOCTYPE" in upper_prefix or "<!ENTITY" in upper_prefix:
            raise UiDocumentError("DTD and entity declarations are not accepted")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise UiDocumentError(f"invalid UI hierarchy XML: {exc}") from exc
        if root.tag != "hierarchy":
            raise UiDocumentError("UI hierarchy root must be <hierarchy>")

        nodes: list[UiNode] = []
        for element in root.iter("node"):
            raw_bounds = element.attrib.get("bounds", "")
            bounds_match = _BOUNDS_RE.fullmatch(raw_bounds)
            bounds = (
                tuple(int(value) for value in bounds_match.groups())
                if bounds_match
                else None
            )
            nodes.append(
                UiNode(
                    text=element.attrib.get("text", ""),
                    resource_id=element.attrib.get("resource-id", ""),
                    content_desc=element.attrib.get("content-desc", ""),
                    bounds=bounds,  # type: ignore[arg-type]
                    enabled=element.attrib.get("enabled", "false").lower() == "true",
                    clickable=element.attrib.get("clickable", "false").lower() == "true",
                )
            )
        if not nodes:
            raise UiDocumentError("UI hierarchy contains no nodes")
        return cls(nodes=tuple(nodes), xml=xml)

    def matching(self, selector: NodeSelector) -> tuple[UiNode, ...]:
        return tuple(node for node in self.nodes if node.matches(selector))

    def unique(self, selector: NodeSelector, *, purpose: str) -> UiNode:
        matches = self.matching(selector)
        if not matches:
            raise UiDocumentError(f"no exact node matches {purpose}")
        if len(matches) != 1:
            raise UiDocumentError(f"{purpose} is ambiguous ({len(matches)} exact matches)")
        return matches[0]

    def has_any(self, selectors: Iterable[NodeSelector]) -> bool:
        return any(self.matching(selector) for selector in selectors)

    def detect_risk(self, risk_texts: Iterable[str]) -> tuple[str, ...]:
        detected: list[str] = []
        visible_values = tuple(
            value
            for node in self.nodes
            for value in (node.text, node.content_desc)
            if value
        )
        for risk_text in risk_texts:
            if any(risk_text in value for value in visible_values):
                detected.append(risk_text)
        return tuple(detected)
