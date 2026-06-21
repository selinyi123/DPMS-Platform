import ipaddress
import socket
from urllib.parse import urlsplit


BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


def assert_public_http_url(value: str, *, require_https: bool = True, resolve_dns: bool = False) -> None:
    """Validate outbound URLs before the runtime connects to operator-supplied hosts."""
    parsed = urlsplit(value or "")
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme not in allowed_schemes:
        raise ValueError("URL must use HTTPS" if require_https else "URL must use HTTP or HTTPS")
    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host")
    if host.lower() in BLOCKED_HOSTNAMES:
        raise ValueError("URL host is not allowed")
    if _is_blocked_ip_literal(host):
        raise ValueError("URL host must not be private, loopback, link-local, or reserved")
    if resolve_dns:
        for ip in _resolve_host(host):
            if _is_blocked_ip(ip):
                raise ValueError("URL resolves to a private, loopback, link-local, or reserved address")


def _resolve_host(hostname: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("URL host could not be resolved") from exc
    return {info[4][0] for info in infos if info and info[4]}


def _is_blocked_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return _is_blocked_ip(hostname)


def _is_blocked_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
