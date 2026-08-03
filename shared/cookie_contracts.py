"""Cookie-name contracts shared by Core ingress and Worker execution."""

from __future__ import annotations


# These values jointly authenticate and authorize Bilibili API mutations.
# A raw Cookie header cannot preserve browser domain/path precedence, so the
# API execution path accepts exactly one value for every critical name.
BILIBILI_API_UNIQUE_COOKIE_NAMES = frozenset(
    {"SESSDATA", "DedeUserID", "bili_jct"}
)


__all__ = ("BILIBILI_API_UNIQUE_COOKIE_NAMES",)
