"""Dispatch bounded Xiaohongshu read-only discovery to its browser Worker."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from app.db import redis
from shared.xiaohongshu_target_pursuit_streams import (
    XIAOHONGSHU_TARGET_PURSUIT_BROWSER_SOURCE_TYPES,
    XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH,
    XIAOHONGSHU_TARGET_PURSUIT_PROTOCOL_VERSION,
    XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
    validate_target_pursuit_stream_fields,
    xiaohongshu_target_pursuit_result_key,
)


XIAOHONGSHU_TARGET_PURSUIT_RESULT_TTL_SECONDS = 600
XIAOHONGSHU_TARGET_PURSUIT_WAIT_TIMEOUT_SECONDS = 90
XIAOHONGSHU_TARGET_PURSUIT_RESULT_POLL_SECONDS = 0.2
XIAOHONGSHU_TARGET_PURSUIT_RESULT_MAX_BYTES = 512 * 1024


class XiaohongshuTargetPursuitDispatchError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


async def dispatch_xiaohongshu_target_pursuit_scan(
    source_type: str,
    source_value: str,
    *,
    max_candidates: int = 20,
) -> dict:
    """Run one read-only browser scan and return its bounded evidence batch."""

    normalized_type = str(source_type or "").strip().casefold()
    normalized_value = str(source_value or "").strip()
    if normalized_type not in XIAOHONGSHU_TARGET_PURSUIT_BROWSER_SOURCE_TYPES:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_browser_source_unsupported"
        )
    if not normalized_value or len(normalized_value) > 256:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_source_value_invalid"
        )
    if (
        normalized_type == "keyword"
        and len(normalized_value)
        > XIAOHONGSHU_TARGET_PURSUIT_KEYWORD_MAX_LENGTH
    ):
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_source_value_invalid"
        )
    try:
        bounded_limit = int(max_candidates)
    except (TypeError, ValueError) as exc:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_limit_invalid"
        ) from exc
    if not 1 <= bounded_limit <= 50:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_limit_invalid"
        )

    request_id = str(uuid.uuid4())
    fields = validate_target_pursuit_stream_fields(
        {
            "protocol_version": str(
                XIAOHONGSHU_TARGET_PURSUIT_PROTOCOL_VERSION
            ),
            "request_id": request_id,
            "source_type": normalized_type,
            "source_value": normalized_value,
            "requested_at_ms": str(int(time.time() * 1000)),
            "max_candidates": str(bounded_limit),
        }
    )
    await redis.xadd(
        XIAOHONGSHU_TARGET_PURSUIT_STREAM_KEY,
        fields,
    )
    result = await _wait_for_target_pursuit_result(request_id)
    if result is None:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_result_timeout"
        )
    if str(result.get("request_id") or "") != request_id:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_result_binding_mismatch"
        )
    if result.get("status") != "completed":
        error_code = str(result.get("error_code") or "").strip()
        raise XiaohongshuTargetPursuitDispatchError(
            error_code
            if error_code.startswith("xiaohongshu_target_pursuit_")
            else "xiaohongshu_target_pursuit_scan_failed"
        )
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > bounded_limit:
        raise XiaohongshuTargetPursuitDispatchError(
            "xiaohongshu_target_pursuit_result_invalid"
        )
    return result


async def _wait_for_target_pursuit_result(
    request_id: str,
) -> dict | None:
    key = xiaohongshu_target_pursuit_result_key(request_id)
    deadline = (
        asyncio.get_running_loop().time()
        + XIAOHONGSHU_TARGET_PURSUIT_WAIT_TIMEOUT_SECONDS
    )
    while asyncio.get_running_loop().time() < deadline:
        raw = await redis.get(key)
        if raw is not None:
            await redis.delete(key)
            if isinstance(raw, bytes):
                if len(raw) > XIAOHONGSHU_TARGET_PURSUIT_RESULT_MAX_BYTES:
                    raise XiaohongshuTargetPursuitDispatchError(
                        "xiaohongshu_target_pursuit_result_too_large"
                    )
                raw = raw.decode("utf-8", errors="strict")
            elif len(str(raw).encode("utf-8")) > (
                XIAOHONGSHU_TARGET_PURSUIT_RESULT_MAX_BYTES
            ):
                raise XiaohongshuTargetPursuitDispatchError(
                    "xiaohongshu_target_pursuit_result_too_large"
                )
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise XiaohongshuTargetPursuitDispatchError(
                    "xiaohongshu_target_pursuit_result_invalid"
                ) from exc
            return decoded if isinstance(decoded, dict) else None
        await asyncio.sleep(
            XIAOHONGSHU_TARGET_PURSUIT_RESULT_POLL_SECONDS
        )
    return None


__all__ = (
    "XIAOHONGSHU_TARGET_PURSUIT_RESULT_MAX_BYTES",
    "XIAOHONGSHU_TARGET_PURSUIT_RESULT_TTL_SECONDS",
    "XiaohongshuTargetPursuitDispatchError",
    "dispatch_xiaohongshu_target_pursuit_scan",
)
