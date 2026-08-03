"""Official Weibo OAuth execution primitives.

Browser selectors remain observation-only.  State-changing calls live in this
package so OAuth capability evidence and official receipts can be validated
without mixing them with Cookie-based browser sessions.
"""

from .capabilities import (
    WeiboOAuthCapabilityError,
    build_weibo_oauth_capability_attestation,
    validate_weibo_oauth_capability_attestation,
)


def __getattr__(name: str):
    """Keep capability-only imports from eagerly loading the HTTP client."""

    if name == "WeiboApiClient":
        from .client import WeiboApiClient

        return WeiboApiClient
    raise AttributeError(name)

__all__ = [
    "WeiboApiClient",
    "WeiboOAuthCapabilityError",
    "build_weibo_oauth_capability_attestation",
    "validate_weibo_oauth_capability_attestation",
]
