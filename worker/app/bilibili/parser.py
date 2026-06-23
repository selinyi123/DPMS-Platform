"""Parse Bilibili web-dynamic JSON into a structured lottery card.

Faithful Python re-implementation of LotteryAutoScript's
``parseDynamicCard`` (lib/core/searcher.js). Given one ``item`` from the
``x/polymer/web-dynamic/v1/feed/space`` (or ``.../detail``) response, it
extracts everything the executor needs to participate: who to follow
(``uid``), what to repost (``dynamic_id``), where to comment (``rid_str`` +
``chat_type``), and whether the dynamic actually carries a lottery.

Big-number safety: ``dynamic_id`` / ``rid_str`` are kept as the API's string
forms — they exceed JS/JSON safe-integer range and must never be coerced to int.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# DYNAMIC_TYPE_* -> numeric type used by the rest of the engine.
_TYPE_ENUM_TO_NUM = {
    "DYNAMIC_TYPE_FORWARD": 1,
    "DYNAMIC_TYPE_DRAW": 2,
    "DYNAMIC_TYPE_WORD": 4,
    "DYNAMIC_TYPE_AV": 8,
    "DYNAMIC_TYPE_ARTICLE": 64,
}

# DYNAMIC_TYPE_* -> the ``type`` param required by the reply/add (comment) API.
_TYPE_ENUM_TO_CHAT = {
    "DYNAMIC_TYPE_FORWARD": 17,
    "DYNAMIC_TYPE_DRAW": 11,
    "DYNAMIC_TYPE_WORD": 17,
    "DYNAMIC_TYPE_AV": 1,
    "DYNAMIC_TYPE_ARTICLE": 12,
}


@dataclass
class DynamicCard:
    uid: int = 0
    uname: str = ""
    is_liked: bool = False
    create_time: int = 0
    type: int = 0
    rid_str: str = ""
    chat_type: int = 0
    dynamic_id: str = ""
    ctrl: list[dict] = field(default_factory=list)
    has_official_lottery: bool = False
    description: str = ""
    reserve_id: int = 0
    reserve_lottery_text: str = ""
    is_charge_lottery: bool = False
    origin: Optional["DynamicCard"] = None


def _dig(d: Any, *path: str, default: Any = None) -> Any:
    """Safe nested ``dict.get`` chain (mirrors JS optional chaining)."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def parse_dynamic_card(item: dict | None) -> DynamicCard:
    """Parse a single dynamic ``item`` into a :class:`DynamicCard`."""
    obj = DynamicCard()
    if not isinstance(item, dict):
        return obj

    enum_type = item.get("type")
    obj.uid = _dig(item, "modules", "module_author", "mid", default=0) or 0
    obj.uname = _dig(item, "modules", "module_author", "name", default="") or ""
    obj.is_liked = bool(_dig(item, "modules", "module_stat", "like", "status", default=False))
    obj.create_time = _dig(item, "modules", "module_author", "pub_ts", default=0) or 0
    obj.type = _TYPE_ENUM_TO_NUM.get(enum_type, 0)
    obj.rid_str = _dig(item, "basic", "comment_id_str", default="") or ""
    obj.chat_type = _TYPE_ENUM_TO_CHAT.get(enum_type, 0)
    obj.dynamic_id = item.get("id_str") or ""

    obj.description = _dig(item, "modules", "module_dynamic", "major", "archive", "desc", default="") or ""
    rich_text_nodes = (
        _dig(item, "modules", "module_dynamic", "desc", "rich_text_nodes")
        or _dig(item, "modules", "module_dynamic", "major", "opus", "summary", "rich_text_nodes")
        or []
    )
    total_len = 0
    for node in rich_text_nodes:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        text = node.get("text") or ""
        if node_type == "RICH_TEXT_NODE_TYPE_AT":
            obj.ctrl.append(
                {"data": node.get("rid"), "location": total_len, "length": len(text), "type": 1}
            )
        if node_type == "RICH_TEXT_NODE_TYPE_LOTTERY":
            obj.has_official_lottery = True
        obj.description += node.get("orig_text") or ""
        total_len += len(text)

    obj.reserve_id = _dig(item, "modules", "module_dynamic", "additional", "reserve", "rid", default=0) or 0
    obj.reserve_lottery_text = _dig(
        item, "modules", "module_dynamic", "additional", "reserve", "title", default="未获取到"
    )
    if _dig(item, "modules", "module_dynamic", "additional", "type") == "ADDITIONAL_TYPE_UPOWER_LOTTERY":
        obj.is_charge_lottery = True

    if obj.type == 1:  # forward: recurse into the original dynamic
        obj.origin = parse_dynamic_card(item.get("orig"))

    return obj


def parse_feed(data: dict | None) -> list[DynamicCard]:
    """Parse a feed/space response into cards.

    Accepts either the full API envelope (``{"code":0,"data":{"items":[...]}}``)
    or the already-unwrapped ``data`` object, so callers can pass the raw
    response straight through.
    """
    if isinstance(data, dict) and "items" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
    items = _dig(data, "items", default=[]) or []
    return [parse_dynamic_card(it) for it in items if isinstance(it, dict)]


def looks_like_lottery(card: DynamicCard) -> bool:
    """Heuristic: does this card (or the dynamic it forwards) carry a lottery?

    Official 互动抽奖 (``has_official_lottery``) or a reserve lottery counts
    directly; otherwise the description must mention 抽奖/福利 — the keyword
    gate proper lives in the executor/config, this is just a fast pre-filter.
    """
    if card.has_official_lottery or card.reserve_id:
        return True
    if card.origin and (card.origin.has_official_lottery or card.origin.reserve_id):
        return True
    text = card.description + (card.origin.description if card.origin else "")
    return "抽奖" in text or "福利" in text or "转发" in text
