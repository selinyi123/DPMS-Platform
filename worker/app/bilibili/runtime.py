from __future__ import annotations

import re
from urllib.parse import urlparse

from .errors import CodeResult, Outcome
from .parser import DynamicCard, parse_dynamic_card


DPMS_TO_API_ACTION = {
    "followed": "follow",
    "liked": "like",
    "commented": "comment",
    "reposted": "repost",
}
API_TO_DPMS_PHASE = {api: phase for phase, api in DPMS_TO_API_ACTION.items()}
ACCOUNT_COOLING_OUTCOMES = {Outcome.CAPTCHA, Outcome.LIMIT, Outcome.RISK}
LOGIN_REQUIRED_OUTCOMES = {Outcome.AUTH}
BILIBILI_DYNAMIC_ID_PATTERN = re.compile(r"[0-9]{1,20}", re.ASCII)
BILIBILI_DIRECT_DYNAMIC_ID_PATTERN = re.compile(r"[0-9]{10,20}", re.ASCII)


class BilibiliRuntimeError(ValueError):
    pass


def dpms_phases_to_api_actions(phases: list[str]) -> list[str]:
    actions = [DPMS_TO_API_ACTION[phase] for phase in phases if phase in DPMS_TO_API_ACTION]
    if not actions:
        raise BilibiliRuntimeError("bilibili_action_plan_has_no_supported_actions")
    return actions


def extract_bilibili_dynamic_id(*values: str | None) -> str:
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        if BILIBILI_DIRECT_DYNAMIC_ID_PATTERN.fullmatch(raw):
            return raw

        try:
            parsed = urlparse(raw)
            port = parsed.port
            hostname = parsed.hostname
        except ValueError:
            continue
        if (
            parsed.scheme.lower() == "canonical"
            and hostname == "bilibili"
            and parsed.username is None
            and parsed.password is None
            and port is None
            and not parsed.query
            and not parsed.fragment
        ):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "dynamic":
                resource_id = parts[1]
                if resource_id.startswith("opus_"):
                    resource_id = resource_id[len("opus_") :]
                if BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(resource_id):
                    return resource_id
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        if port not in (None, 443):
            continue
        host = (hostname or "").rstrip(".").lower()
        parts = [part for part in parsed.path.split("/") if part]
        if host == "t.bilibili.com":
            if len(parts) == 1 and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(parts[0]):
                return parts[0]
            if (
                len(parts) == 2
                and parts[0] == "opus"
                and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(parts[1])
            ):
                return parts[1]
        if host in {"bilibili.com", "www.bilibili.com"}:
            if (
                len(parts) == 2
                and parts[0] == "opus"
                and BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(parts[1])
            ):
                return parts[1]
    raise BilibiliRuntimeError("bilibili_dynamic_target_required")


def parse_detail_card(payload: dict, requested_dynamic_id: str) -> DynamicCard:
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get("item") if isinstance(data, dict) else None
    card = parse_dynamic_card(item)
    if card.dynamic_id and requested_dynamic_id and card.dynamic_id != requested_dynamic_id:
        raise BilibiliRuntimeError("bilibili_dynamic_detail_target_mismatch")
    if not card.dynamic_id:
        raise BilibiliRuntimeError("bilibili_dynamic_detail_unparseable")
    return card


def execution_target(card: DynamicCard) -> DynamicCard:
    if card.type == 1 and card.origin and card.origin.dynamic_id:
        return card.origin
    return card


def validate_card_for_actions(card: DynamicCard, actions: list[str]) -> None:
    target = execution_target(card)
    missing = []
    if "follow" in actions and not target.uid:
        missing.append("uid")
    if any(action in actions for action in ("like", "repost")) and not target.dynamic_id:
        missing.append("dynamic_id")
    if "comment" in actions and (not target.rid_str or not target.chat_type):
        missing.append("comment_target")
    if missing:
        raise BilibiliRuntimeError(f"bilibili_dynamic_missing_fields:{','.join(sorted(set(missing)))}")


def account_status_for_results(results: dict[str, CodeResult]) -> tuple[str, str] | None:
    for phase, result in results.items():
        if result.outcome in LOGIN_REQUIRED_OUTCOMES:
            return "login_required", f"bilibili_{phase}_{result.outcome.value}"
        if result.outcome in ACCOUNT_COOLING_OUTCOMES:
            return "cooling", f"bilibili_{phase}_{result.outcome.value}"
    return None
