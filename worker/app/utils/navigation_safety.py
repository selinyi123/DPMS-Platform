import re
from urllib.parse import urlparse


WEIBO_MBLOGID_PATTERN = re.compile(r"(?=.*[A-Za-z])[A-Za-z0-9]{6,16}")
WEIBO_MID_PATTERN = re.compile(r"[1-9][0-9]{0,18}", re.ASCII)
WEIBO_MAX_STATUS_ID = (1 << 63) - 1
XIAOHONGSHU_NOTE_PATTERN = re.compile(r"[0-9a-fA-F]{24}")
BILIBILI_ARTICLE_PATTERN = re.compile(r"cv[0-9]+", re.ASCII)
BILIBILI_DYNAMIC_ID_PATTERN = re.compile(r"(?:opus_)?[0-9]{1,20}", re.ASCII)
BILIBILI_DYNAMIC_PATH_ID_PATTERN = re.compile(r"[0-9]{1,20}", re.ASCII)
BILIBILI_VIDEO_ID_PATTERN = re.compile(
    r"(?:BV[0-9A-Za-z]+|av[0-9]+)",
    re.IGNORECASE | re.ASCII,
)
WEIBO_UID_PATTERN = re.compile(r"[0-9]{1,20}", re.ASCII)
DOUYIN_VIDEO_ID_PATTERN = re.compile(r"[0-9]{8,32}", re.ASCII)
DOUYIN_NOTE_ID_PATTERN = re.compile(r"[0-9]{19}", re.ASCII)


PLATFORM_ALLOWED_NAVIGATION_HOSTS = {
    "bilibili": {"b23.tv", "t.bilibili.com", "bilibili.com", "www.bilibili.com"},
    "weibo": {"t.cn", "m.weibo.cn", "weibo.com", "www.weibo.com"},
    "xiaohongshu": {"xhslink.com", "xiaohongshu.com", "www.xiaohongshu.com"},
    "douyin": {"v.douyin.com", "douyin.com", "www.douyin.com", "www.iesdouyin.com"},
}


def validated_platform_navigation_url(platform: str, value: str) -> str:
    """Allow only HTTPS top-level targets owned by the selected platform."""
    target = str(value or "").strip()
    parsed = urlparse(target)
    if parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None:
        raise ValueError("platform_navigation_target_not_allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("platform_navigation_target_not_allowed") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    allowed_hosts = PLATFORM_ALLOWED_NAVIGATION_HOSTS.get(str(platform or "").strip().lower(), set())
    if port not in (None, 443) or host not in allowed_hosts:
        raise ValueError("platform_navigation_target_not_allowed")
    return target


def validated_platform_content_url(platform: str, value: str, canonical_uri: str) -> str:
    """Require the final HTTPS page to represent the bound canonical content.

    A platform host allowlist alone is insufficient: a login challenge or a
    redirect to the platform home page (or to a different post) is still on an
    allowed host.  Keep this parser deliberately aligned with Core's
    canonicalizer and fail closed for any unknown resource form.
    """
    target = validated_platform_navigation_url(platform, value)
    normalized_platform = str(platform or "").strip().lower()
    resource_type, canonical_id = _parse_canonical_content_uri(
        normalized_platform,
        canonical_uri,
    )
    final_identity = _content_identity_from_https_url(normalized_platform, target)
    if final_identity is None:
        raise ValueError("platform_navigation_content_identity_mismatch")
    final_type, final_id = final_identity
    if resource_type != final_type or _normalized_content_id(
        normalized_platform,
        resource_type,
        canonical_id,
    ) != _normalized_content_id(normalized_platform, final_type, final_id):
        raise ValueError("platform_navigation_content_identity_mismatch")
    return target


def validated_platform_canonical_uri(platform: str, canonical_uri: str) -> str:
    """Validate that a task carries a supported canonical content identity."""
    normalized_platform = str(platform or "").strip().lower()
    canonical = str(canonical_uri or "").strip()
    _parse_canonical_content_uri(normalized_platform, canonical)
    return canonical


def _parse_canonical_content_uri(platform: str, canonical_uri: str) -> tuple[str, str]:
    parsed = urlparse(str(canonical_uri or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("platform_navigation_canonical_identity_invalid") from exc
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme.lower() != "canonical"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or (parsed.hostname or "").rstrip(".").lower() != platform
        or len(path_parts) != 2
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    resource_type, resource_id = path_parts
    allowed_types = {
        "bilibili": {"dynamic", "video", "article"},
        "weibo": {"status"},
        "xiaohongshu": {"note"},
        "douyin": {"video", "note"},
    }
    if resource_type not in allowed_types.get(platform, set()) or not resource_id:
        raise ValueError("platform_navigation_canonical_identity_invalid")
    if platform == "weibo" and resource_type == "status" and not _is_weibo_status_id(
        resource_id
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    if (
        platform == "bilibili"
        and resource_type == "article"
        and not BILIBILI_ARTICLE_PATTERN.fullmatch(resource_id)
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    if platform == "bilibili" and (
        (
            resource_type == "dynamic"
            and not BILIBILI_DYNAMIC_ID_PATTERN.fullmatch(resource_id)
        )
        or (
            resource_type == "video"
            and not BILIBILI_VIDEO_ID_PATTERN.fullmatch(resource_id)
        )
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    if (
        platform == "xiaohongshu"
        and resource_type == "note"
        and not XIAOHONGSHU_NOTE_PATTERN.fullmatch(resource_id)
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    if platform == "douyin" and (
        (
            resource_type == "video"
            and not DOUYIN_VIDEO_ID_PATTERN.fullmatch(resource_id)
        )
        or (
            resource_type == "note"
            and not DOUYIN_NOTE_ID_PATTERN.fullmatch(resource_id)
        )
    ):
        raise ValueError("platform_navigation_canonical_identity_invalid")
    return resource_type, resource_id


def _content_identity_from_https_url(platform: str, value: str) -> tuple[str, str] | None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if platform == "bilibili":
        if host == "t.bilibili.com":
            if len(parts) == 1 and BILIBILI_DYNAMIC_PATH_ID_PATTERN.fullmatch(parts[0]):
                return "dynamic", parts[0]
            if (
                len(parts) == 2
                and parts[0] == "opus"
                and BILIBILI_DYNAMIC_PATH_ID_PATTERN.fullmatch(parts[1])
            ):
                return "dynamic", parts[1]
        if host in {"bilibili.com", "www.bilibili.com"} and len(parts) == 2:
            if (
                parts[0] == "opus"
                and BILIBILI_DYNAMIC_PATH_ID_PATTERN.fullmatch(parts[1])
            ):
                return "dynamic", parts[1]
            if parts[0] == "video" and BILIBILI_VIDEO_ID_PATTERN.fullmatch(parts[1]):
                return "video", parts[1]
            if parts[0] == "read" and BILIBILI_ARTICLE_PATTERN.fullmatch(parts[1]):
                return "article", parts[1]
        return None

    if platform == "weibo":
        if (
            host == "m.weibo.cn"
            and len(parts) == 2
            and parts[0] in {"status", "detail"}
            and _is_weibo_status_id(parts[1])
        ):
            return "status", parts[1]
        if host in {"weibo.com", "www.weibo.com"} and len(parts) == 2:
            if (
                (parts[0] == "detail" or WEIBO_UID_PATTERN.fullmatch(parts[0]))
                and _is_weibo_status_id(parts[1])
            ):
                return "status", parts[1]
        return None

    if platform == "xiaohongshu":
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            if (
                len(parts) == 2
                and parts[0] == "explore"
                and XIAOHONGSHU_NOTE_PATTERN.fullmatch(parts[1])
            ):
                return "note", parts[1]
            if (
                len(parts) == 3
                and parts[:2] == ["discovery", "item"]
                and XIAOHONGSHU_NOTE_PATTERN.fullmatch(parts[2])
            ):
                return "note", parts[2]
        return None

    if platform == "douyin":
        if (
            host in {"douyin.com", "www.douyin.com"}
            and len(parts) == 2
            and parts[0] == "video"
            and DOUYIN_VIDEO_ID_PATTERN.fullmatch(parts[1])
        ):
            return "video", parts[1]
        if (
            host == "www.iesdouyin.com"
            and len(parts) == 3
            and parts[:2] == ["share", "video"]
            and DOUYIN_VIDEO_ID_PATTERN.fullmatch(parts[2])
        ):
            return "video", parts[2]
        if (
            host == "www.douyin.com"
            and len(parts) == 2
            and parts[0] == "note"
            and DOUYIN_NOTE_ID_PATTERN.fullmatch(parts[1])
        ):
            return "note", parts[1]
        return None

    return None


def _normalized_content_id(platform: str, resource_type: str, value: str) -> str:
    normalized = str(value or "").strip()
    if platform == "bilibili" and resource_type == "dynamic" and normalized.startswith("opus_"):
        return normalized[len("opus_") :]
    if platform == "xiaohongshu" and resource_type == "note":
        return normalized.lower()
    return normalized


def _is_weibo_status_id(value: str) -> bool:
    return bool(
        (
            WEIBO_MID_PATTERN.fullmatch(value)
            and int(value) <= WEIBO_MAX_STATUS_ID
        )
        or WEIBO_MBLOGID_PATTERN.fullmatch(value)
    )


async def install_main_frame_navigation_guard(
    page,
    platform: str,
    canonical_uri: str | None = None,
) -> None:
    """Abort disallowed top-level redirects before the browser sends them.

    Before the initial target resolves, a platform-host guard permits reviewed
    short links. Once the final content identity is known, callers install a
    second guard with ``canonical_uri`` so another post on the same host is
    rejected before it can replace the bound page.
    """
    main_frame = page.main_frame

    async def guard(route):
        request = route.request
        is_main_navigation = request.is_navigation_request() and request.frame == main_frame
        if is_main_navigation:
            try:
                if canonical_uri is None:
                    validated_platform_navigation_url(platform, request.url)
                else:
                    validated_platform_content_url(platform, request.url, canonical_uri)
            except ValueError:
                await route.abort()
                return
        await route.continue_()

    await page.route("**/*", guard)
