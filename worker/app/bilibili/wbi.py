"""Bilibili wbi request signing.

LotteryAutoScript does **not** sign requests, which the audit flagged as the
single biggest long-term-stability risk: modern Bilibili web endpoints
(notably ``x/polymer/web-dynamic/v1/feed/space`` and ``web-interface/card``)
increasingly reject unsigned GETs with ``-403`` / ``-352`` or return empty
data. This module adds the signing the reference script lacks.

Algorithm (the well-documented public wbi scheme — not from any GPL source):

1. Fetch ``x/web-interface/nav``; it returns ``data.wbi_img.img_url`` and
   ``sub_url``. The key is the file stem of each URL.
2. ``mixin_key`` = the 64-char ``img_key + sub_key`` re-ordered by a fixed
   permutation table, truncated to 32 chars.
3. For a request, add ``wts`` (unix seconds), sort params by key, strip the
   characters ``!'()*`` from values, urlencode, and
   ``w_rid = md5(query + mixin_key)``.

Pure functions; fully unit-testable offline against the canonical key vector.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Mapping

# Canonical wbi permutation table (公开算法).
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_FILTER_CHARS = "!'()*"


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """Derive the 32-char mixin key from the nav img/sub keys."""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def key_from_url(url: str) -> str:
    """``https://i0.hdslb.com/bfs/wbi/<key>.png`` -> ``<key>``."""
    return url.rsplit("/", 1)[-1].split(".", 1)[0]


def sign(
    params: Mapping[str, object],
    img_key: str,
    sub_key: str,
    *,
    wts: int | None = None,
) -> dict[str, object]:
    """Return ``params`` plus ``wts`` and the computed ``w_rid``.

    ``wts`` is injectable so the signature is deterministic in tests.
    """
    mixin_key = get_mixin_key(img_key, sub_key)
    if wts is None:
        wts = int(time.time())

    signed: dict[str, object] = {str(k): v for k, v in params.items()}
    signed["wts"] = wts

    items = []
    for k in sorted(signed):
        v = "".join(c for c in str(signed[k]) if c not in _FILTER_CHARS)
        items.append((k, v))
    query = urllib.parse.urlencode(items)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed
