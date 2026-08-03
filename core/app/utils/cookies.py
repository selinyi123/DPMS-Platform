import json
from http.cookies import SimpleCookie

from shared.cookie_contracts import BILIBILI_API_UNIQUE_COOKIE_NAMES


PLATFORM_COOKIE_DOMAINS = {
    "bilibili": ".bilibili.com",
    "weibo": ".weibo.com",
    "douyin": ".douyin.com",
    "xiaohongshu": ".xiaohongshu.com",
}


def normalize_cookie_payload(platform: str, payload: str) -> str:
    cookies = parse_cookie_payload(platform, payload)
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def validate_required_cookies(cookies: list[dict], required_names: list[str]) -> None:
    required = set(required_names)
    counts: dict[str, int] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if name in required:
            counts[name] = counts.get(name, 0) + 1
    missing = sorted(required.difference(counts))
    if missing:
        raise ValueError(f"缺少平台必需 Cookie: {', '.join(missing)}")
    duplicated = sorted(
        name for name, count in counts.items() if count != 1
    )
    if duplicated:
        raise ValueError(
            "Duplicate required Cookie names are not allowed: "
            + ", ".join(duplicated)
        )


def validate_api_cookie_name_uniqueness(
    platform: str,
    cookies: list[dict],
) -> None:
    """Reject ambiguity that the platform API Cookie header cannot encode."""

    if str(platform or "").strip().casefold() != "bilibili":
        return
    counts: dict[str, int] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        if name in BILIBILI_API_UNIQUE_COOKIE_NAMES:
            counts[name] = counts.get(name, 0) + 1
    duplicated = sorted(
        name for name, count in counts.items() if count > 1
    )
    if duplicated:
        raise ValueError(
            "Duplicate Bilibili API Cookie names are not allowed: "
            + ", ".join(duplicated)
        )


def parse_cookie_payload(platform: str, payload: str) -> list[dict]:
    raw_text = (payload or "").strip()
    text = normalize_raw_cookie_text(raw_text)
    if not text:
        raise ValueError("Cookie 不能为空")

    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            cookie_data = data.get("cookies")
            if isinstance(cookie_data, dict):
                data = [
                    {"name": name, "value": value}
                    for name, value in cookie_data.items()
                ]
            elif cookie_data is not None:
                data = cookie_data
            elif data and all(
                isinstance(name, str)
                and isinstance(value, (str, int, float, bool))
                for name, value in data.items()
            ):
                # Browser helpers commonly export a direct name/value map.
                data = [
                    {"name": name, "value": value}
                    for name, value in data.items()
                ]
            else:
                data = []
        if not isinstance(data, list):
            raise ValueError("JSON Cookie 必须是数组，或包含 cookies 数组")
        cookies = [_normalize_cookie_item(platform, item) for item in data]
    else:
        cookies = _parse_tabular_cookie_payload(platform, raw_text)
        if cookies is None:
            parsed = SimpleCookie()
            parsed.load(text)
            cookies = [
                _normalize_cookie_item(platform, {"name": key, "value": morsel.value})
                for key, morsel in parsed.items()
            ]

    cookies = [cookie for cookie in cookies if cookie.get("name") and cookie.get("value") is not None]
    if not cookies:
        raise ValueError("未识别到有效 Cookie")
    return cookies


def _parse_tabular_cookie_payload(
    platform: str,
    payload: str,
) -> list[dict] | None:
    """Parse Netscape files and rows copied from browser cookie tables."""

    cookies: list[dict] = []
    saw_tabular_row = False
    for raw_line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
            http_only = True
        elif line.startswith("#"):
            continue
        if "\t" not in line:
            return None
        fields = line.split("\t")

        # Netscape cookie file:
        # domain, include_subdomains, path, secure, expires, name, value
        if (
            len(fields) >= 7
            and fields[1].strip().upper() in {"TRUE", "FALSE"}
            and fields[2].strip().startswith("/")
            and fields[3].strip().upper() in {"TRUE", "FALSE"}
        ):
            saw_tabular_row = True
            item = {
                "name": fields[5],
                "value": fields[6],
                "domain": fields[0],
                "path": fields[2],
                "secure": fields[3].strip().upper() == "TRUE",
                "httpOnly": http_only,
                "expires": fields[4],
            }
            cookies.append(_normalize_cookie_item(platform, item))
            continue

        # Chromium/Firefox cookie table copy:
        # name, value, domain, path, ... optional flags
        if (
            len(fields) >= 4
            and fields[0].strip()
            and fields[2].strip()
            and fields[3].strip().startswith("/")
        ):
            saw_tabular_row = True
            flag_fields = {field.strip().casefold() for field in fields[4:]}
            item = {
                "name": fields[0],
                "value": fields[1],
                "domain": fields[2],
                "path": fields[3],
                "secure": "true" in flag_fields or "secure" in flag_fields,
                "httpOnly": "httponly" in flag_fields,
            }
            cookies.append(_normalize_cookie_item(platform, item))
            continue
        return None
    return cookies if saw_tabular_row else None


def normalize_raw_cookie_text(payload: str) -> str:
    text = (payload or "").strip()
    if not text:
        return ""
    if text.startswith("[") or text.startswith("{"):
        return text

    lines = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        if line.lower().startswith("set-cookie:"):
            line = line.split(":", 1)[1].strip()
        lines.append(line)
    return "; ".join(lines)


def _normalize_cookie_item(platform: str, item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Cookie 项必须是对象")
    domain = item.get("domain") or PLATFORM_COOKIE_DOMAINS.get(platform, ".bilibili.com")
    path = item.get("path") or "/"
    cookie = {
        "name": str(item.get("name", "")).strip(),
        "value": str(item.get("value", "")),
        "domain": domain,
        "path": path,
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": bool(item.get("secure", True)),
    }
    if item.get("expires") not in (None, "", -1):
        try:
            cookie["expires"] = int(float(item["expires"]))
        except (TypeError, ValueError):
            pass
    if item.get("sameSite") in {"Strict", "Lax", "None"}:
        cookie["sameSite"] = item["sameSite"]
    return cookie
