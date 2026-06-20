import unittest

from app.capacity.engine import (
    compute_capacity,
    isolation_violations,
    recommend_bindings,
)

LIMITS = {"bilibili": {"max_daily_per_account": 8, "min_spacing_min": 45, "max_per_window": 12}}


def acct(account_id, *, platform="bilibili", status="ready", proxy_id=None, fingerprint_id=None):
    return {
        "account_id": account_id,
        "platform": platform,
        "status": status,
        "proxy_id": proxy_id,
        "fingerprint_id": fingerprint_id,
    }


def proxy(proxy_id, *, status="active", health_score=100, in_cooldown=False):
    return {"proxy_id": proxy_id, "status": status, "health_score": health_score, "in_cooldown": in_cooldown}


class ComputeCapacityTests(unittest.TestCase):
    def test_sustainable_daily_is_safe_accounts_times_cap(self):
        accounts = [
            acct(1, proxy_id=10, fingerprint_id=100),
            acct(2, proxy_id=11, fingerprint_id=101),
            acct(3, status="cooling", proxy_id=12, fingerprint_id=102),
        ]
        proxies = [proxy(10), proxy(11), proxy(12)]
        cap = compute_capacity(accounts, proxies, limits=LIMITS)
        bili = cap["per_platform"]["bilibili"]
        self.assertEqual(bili["safe_accounts"], 2)  # account 3 is cooling
        self.assertEqual(bili["sustainable_daily"], 2 * 8)

    def test_no_healthy_bound_proxy_means_zero_sustainable(self):
        # Safety invariant (P0-3): ready accounts with no healthy proxy backing
        # them must NOT invent capacity. Isolation requires a healthy proxy per
        # account, so sustainable_daily is 0 here despite 2 safe accounts.
        accounts = [acct(1, proxy_id=None, fingerprint_id=100), acct(2, proxy_id=None, fingerprint_id=101)]
        proxies = []
        cap = compute_capacity(accounts, proxies, limits=LIMITS)
        bili = cap["per_platform"]["bilibili"]
        self.assertEqual(bili["safe_accounts"], 2)
        self.assertEqual(bili["healthy_bound_proxies"], 0)
        self.assertEqual(bili["sustainable_daily"], 0)

    def test_unhealthy_proxy_does_not_back_capacity(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=11, fingerprint_id=101)]
        proxies = [proxy(10, health_score=100), proxy(11, health_score=10)]  # 11 unhealthy
        cap = compute_capacity(accounts, proxies, limits=LIMITS)
        bili = cap["per_platform"]["bilibili"]
        self.assertEqual(bili["safe_accounts"], 2)
        self.assertEqual(bili["healthy_bound_proxies"], 1)
        # backing limited by healthy proxies -> 1 * 8
        self.assertEqual(bili["sustainable_daily"], 1 * 8)

    def test_proxy_pool_counts_free_healthy(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100)]
        proxies = [proxy(10), proxy(11), proxy(12, status="dead")]
        cap = compute_capacity(accounts, proxies, limits=LIMITS)
        pool = cap["proxy_pool"]
        self.assertEqual(pool["healthy"], 2)  # 10 and 11
        self.assertEqual(pool["free_healthy"], 1)  # 11 is healthy and unbound


class RecommendBindingsTests(unittest.TestCase):
    def test_idle_account_gets_a_free_healthy_proxy(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=None, fingerprint_id=101)]
        proxies = [proxy(10), proxy(20, health_score=95)]
        result = recommend_bindings(accounts, proxies)
        self.assertEqual(len(result["bindings"]), 1)
        self.assertEqual(result["bindings"][0]["account_id"], 2)
        self.assertEqual(result["bindings"][0]["recommended_proxy_id"], 20)
        self.assertEqual(result["unmet"], [])

    def test_never_recommends_an_already_bound_proxy(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=None, fingerprint_id=101)]
        proxies = [proxy(10)]  # only proxy is already bound to account 1
        result = recommend_bindings(accounts, proxies)
        self.assertEqual(result["bindings"], [])
        self.assertEqual(result["unmet"][0]["account_id"], 2)
        self.assertEqual(result["unmet"][0]["reason"], "no_free_healthy_proxy")


class IsolationViolationsTests(unittest.TestCase):
    def test_shared_proxy_is_flagged(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=10, fingerprint_id=101)]
        violations = isolation_violations(accounts)
        self.assertTrue(any(v["kind"] == "shared_proxy" and v["key"] == 10 for v in violations))

    def test_shared_fingerprint_is_flagged(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=11, fingerprint_id=100)]
        violations = isolation_violations(accounts)
        self.assertTrue(any(v["kind"] == "shared_fingerprint" and v["key"] == 100 for v in violations))

    def test_properly_isolated_fleet_has_no_violations(self):
        accounts = [acct(1, proxy_id=10, fingerprint_id=100), acct(2, proxy_id=11, fingerprint_id=101)]
        self.assertEqual(isolation_violations(accounts), [])


if __name__ == "__main__":
    unittest.main()
