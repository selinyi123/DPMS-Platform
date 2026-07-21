from urllib.parse import urlparse


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
        "douyin": {"video"},
    }
    if resource_type not in allowed_types.get(platform, set()) or not resource_id:
        raise ValueError("platform_navigation_canonical_identity_invalid")
    return resource_type, resource_id


def _content_identity_from_https_url(platform: str, value: str) -> tuple[str, str] | None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if platform == "bilibili":
        if host == "t.bilibili.com":
            if len(parts) == 1:
                return "dynamic", parts[0]
            if len(parts) == 2 and parts[0] == "opus":
                return "dynamic", parts[1]
        if host in {"bilibili.com", "www.bilibili.com"} and len(parts) == 2:
            if parts[0] == "opus":
                return "dynamic", parts[1]
            if parts[0] == "video":
                return "video", parts[1]
            if parts[0] == "read":
                return "article", parts[1]
        return None

    if platform == "weibo":
        if host == "m.weibo.cn" and len(parts) == 2 and parts[0] in {"status", "detail"}:
            return "status", parts[1]
        if host in {"weibo.com", "www.weibo.com"} and len(parts) == 2:
            if parts[0] == "detail" or parts[0].isdigit():
                return "status", parts[1]
        return None

    if platform == "xiaohongshu":
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            if len(parts) == 2 and parts[0] == "explore":
                return "note", parts[1]
            if len(parts) == 3 and parts[:2] == ["discovery", "item"]:
                return "note", parts[2]
        return None

    if platform == "douyin":
        if (
            host in {"douyin.com", "www.douyin.com"}
            and len(parts) == 2
            and parts[0] == "video"
            and parts[1].isdigit()
        ):
            return "video", parts[1]
        if (
            host == "www.iesdouyin.com"
            and len(parts) == 3
            and parts[:2] == ["share", "video"]
            and parts[2].isdigit()
        ):
            return "video", parts[2]
        return None

    return None


def _normalized_content_id(platform: str, resource_type: str, value: str) -> str:
    normalized = str(value or "").strip()
    if platform == "bilibili" and resource_type == "dynamic" and normalized.startswith("opus_"):
        return normalized[len("opus_") :]
    if platform == "xiaohongshu" and resource_type == "note":
        return normalized.lower()
    return normalized


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
