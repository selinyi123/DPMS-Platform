"""Pure capacity logic for the DPMS Capacity Runtime (V11 / stage S10).

No database or framework dependency. Where V10 (scheduling) answers *when*
work can run, V11 models the *supply side*: how many safe accounts and healthy
proxies each platform has, the sustainable daily ceiling that implies, and —
respecting one-account-one-proxy isolation — which idle proxies could be bound
to accounts that still need one.

Safety boundary (non-negotiable):

- Account isolation (1 account : 1 proxy : 1 fingerprint) is a first-class
  invariant. ``isolation_violations`` *detects and reports* shared proxies or
  fingerprints; ``recommend_bindings`` only ever proposes pairing an idle
  account with an *unbound* healthy proxy and never suggests a shared binding.
  This module proposes; it never mutates a binding.
- ``sustainable_daily`` is a compliance ceiling (safe accounts × the V10 daily
  cap), capped further by healthy proxy supply — it is a "do not exceed", not a
  throughput target.
"""

from __future__ import annotations

from app.scheduling.engine import DEFAULT_RATE_LIMIT, PLATFORM_RATE_LIMITS

# A proxy is only counted toward sustainable supply when it is active and
# healthy enough; degraded/dead or cooling proxies do not add capacity.
HEALTHY_PROXY_MIN_SCORE = 60.0
READY_STATUS = "ready"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_healthy_proxy(proxy: dict) -> bool:
    if proxy.get("status") not in (None, "active"):
        return False
    if proxy.get("in_cooldown"):
        return False
    try:
        return float(proxy.get("health_score", 0)) >= HEALTHY_PROXY_MIN_SCORE
    except (TypeError, ValueError):
        return False


def _max_daily_for(platform, limits: dict) -> int:
    return _as_int((limits.get(platform) or DEFAULT_RATE_LIMIT).get("max_daily_per_account"))


def compute_capacity(
    accounts: list[dict],
    proxies: list[dict],
    *,
    limits: dict | None = None,
) -> dict:
    """Aggregate the supply side per platform plus a global proxy pool summary.

    ``accounts``: ``[{account_id, platform, status, proxy_id, fingerprint_id}]``.
    ``proxies``: ``[{proxy_id, status, health_score, in_cooldown}]`` (global —
    proxies are platform-agnostic and bound to a platform only via an account).
    """
    limit_table = limits or PLATFORM_RATE_LIMITS
    healthy_proxy_ids = {p.get("proxy_id") for p in proxies if _is_healthy_proxy(p)}
    bound_proxy_ids = {a.get("proxy_id") for a in accounts if a.get("proxy_id") is not None}

    per_platform: dict[str, dict] = {}
    for account in accounts:
        platform = account.get("platform")
        bucket = per_platform.setdefault(platform, {
            "total_accounts": 0,
            "safe_accounts": 0,
            "healthy_bound_proxies": 0,
            "sustainable_daily": 0,
            "max_daily_per_account": _max_daily_for(platform, limit_table),
        })
        bucket["total_accounts"] += 1
        if account.get("status") == READY_STATUS:
            bucket["safe_accounts"] += 1
            if account.get("proxy_id") in healthy_proxy_ids:
                bucket["healthy_bound_proxies"] += 1

    for platform, bucket in per_platform.items():
        max_daily = bucket["max_daily_per_account"]
        # Under the one-account-one-proxy isolation invariant, an account with
        # no healthy proxy backing it cannot sustainably run. Sustainable supply
        # is therefore the scarcer of safe accounts and the healthy proxies
        # actually bound to them — and with zero healthy bound proxies it is
        # zero. (Never fall back to the bare account count: that would invent
        # capacity the isolation model forbids and mislead V10/V12/V13.)
        backing = min(bucket["safe_accounts"], bucket["healthy_bound_proxies"])
        bucket["sustainable_daily"] = backing * max_daily

    free_healthy = sorted(healthy_proxy_ids - bound_proxy_ids, key=lambda x: (x is None, x))
    return {
        "per_platform": per_platform,
        "proxy_pool": {
            "total": len(proxies),
            "healthy": len(healthy_proxy_ids),
            "bound": len(healthy_proxy_ids & bound_proxy_ids),
            "free_healthy": len(free_healthy),
        },
    }


def recommend_bindings(accounts: list[dict], proxies: list[dict]) -> dict:
    """Propose pairing idle ready accounts with unbound healthy proxies.

    One proxy per account, never shared. Returns ``{bindings, unmet}`` where
    ``unmet`` lists ready accounts still lacking a proxy once the free healthy
    pool is exhausted.
    """
    bound_proxy_ids = {a.get("proxy_id") for a in accounts if a.get("proxy_id") is not None}
    free_healthy = sorted(
        (p for p in proxies if _is_healthy_proxy(p) and p.get("proxy_id") not in bound_proxy_ids),
        key=lambda p: (-float(p.get("health_score", 0) or 0), _as_int(p.get("proxy_id"))),
    )
    needy = sorted(
        (a for a in accounts if a.get("status") == READY_STATUS and a.get("proxy_id") is None),
        key=lambda a: _as_int(a.get("account_id")),
    )

    bindings: list[dict] = []
    unmet: list[dict] = []
    pool = list(free_healthy)
    for account in needy:
        if pool:
            proxy = pool.pop(0)
            bindings.append({
                "account_id": account.get("account_id"),
                "platform": account.get("platform"),
                "recommended_proxy_id": proxy.get("proxy_id"),
                "health_score": proxy.get("health_score"),
            })
        else:
            unmet.append({
                "account_id": account.get("account_id"),
                "platform": account.get("platform"),
                "reason": "no_free_healthy_proxy",
            })
    return {"bindings": bindings, "unmet": unmet}


def isolation_violations(accounts: list[dict]) -> list[dict]:
    """Detect accounts that share a proxy or a fingerprint (should be empty).

    A non-empty result is an isolation risk an operator must resolve: under the
    project's safety model every account must have its own proxy and
    fingerprint. The engine only reports; it never auto-resolves.
    """
    by_proxy: dict[object, list] = {}
    by_fingerprint: dict[object, list] = {}
    for account in accounts:
        if account.get("proxy_id") is not None:
            by_proxy.setdefault(account["proxy_id"], []).append(account.get("account_id"))
        if account.get("fingerprint_id") is not None:
            by_fingerprint.setdefault(account["fingerprint_id"], []).append(account.get("account_id"))

    violations: list[dict] = []
    for proxy_id, ids in by_proxy.items():
        if len(ids) > 1:
            violations.append({
                "kind": "shared_proxy",
                "key": proxy_id,
                "account_ids": sorted(ids, key=lambda x: _as_int(x)),
                "detail": f"{len(ids)} accounts share proxy {proxy_id}; each account must have its own proxy.",
            })
    for fingerprint_id, ids in by_fingerprint.items():
        if len(ids) > 1:
            violations.append({
                "kind": "shared_fingerprint",
                "key": fingerprint_id,
                "account_ids": sorted(ids, key=lambda x: _as_int(x)),
                "detail": f"{len(ids)} accounts share fingerprint {fingerprint_id}; each account must have its own fingerprint.",
            })
    return violations
