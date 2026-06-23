"""Resolve a Bilibili lottery target URL to a dynamic id.

Discovery / import stores the lottery as a URL (t.bilibili.com/<id>,
.../opus/<id>, etc.). The direct-API engine works on the numeric dynamic id, so
this extracts it. b23.tv short links are NOT resolved here (that needs a network
round-trip) — callers should expand them first.
"""

from __future__ import annotations

import re

# /opus/<id>, t.bilibili.com/<id>, /dynamic/<id>, or a bare long number.
_DYID_RE = re.compile(r"(?:/opus/|t\.bilibili\.com/|/dynamic/)(\d{10,})|(\d{12,})")


def extract_dynamic_id(value: str | None) -> str | None:
    if not value:
        return None
    m = _DYID_RE.search(value.strip())
    if not m:
        return None
    return m.group(1) or m.group(2)
