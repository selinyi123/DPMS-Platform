
from datetime import datetime, timedelta, timezone

from app.db import database, redis
from app.event_store.service import record_event

from app.utils.log import structured_log

from app.services.state_machine import AccountStatus, transition_account



SLIDING_WINDOW_MINUTES = 5

SLIDING_WINDOW_MAX = 5

DAILY_LIMIT_DEFAULT = 30



async def check_action_rate(account_id: int) -> bool:

    key = f"risk_window:{account_id}"

    now = datetime.now().timestamp()

    window_start = now - SLIDING_WINDOW_MINUTES * 60



    pipe = redis.pipeline()

    pipe.zadd(key, {str(now): now})

    pipe.zremrangebyscore(key, 0, window_start)

    pipe.zcard(key)

    pipe.expire(key, SLIDING_WINDOW_MINUTES * 60 * 2)

    _, _, count, _ = await pipe.execute()



    if count > SLIDING_WINDOW_MAX:

        structured_log("warning", "sliding_window_exceeded", account_id=account_id, count=count)

        acc = await database.fetch_one("SELECT id, version FROM accounts WHERE id = :id", {"id": account_id})

        if acc:

            try:

                await transition_account(account_id, acc["version"], AccountStatus.COOLING)
                await record_event(
                    aggregate="account",
                    aggregate_id=account_id,
                    event_type="AccountCooling",
                    payload={"reason": "sliding_window_exceeded", "count": count},
                )

            except Exception as e:
                structured_log("warning", "cooling_transition_failed", account_id=account_id, exception=e)
        await record_event(
            aggregate="risk",
            aggregate_id=f"account:{account_id}",
            event_type="RateLimited",
            payload={"account_id": account_id, "count": count, "window_minutes": SLIDING_WINDOW_MINUTES},
        )

        return False

    return True



async def check_daily_limit(account_id: int, limit: int = DAILY_LIMIT_DEFAULT) -> bool:

    key = f"daily_limit:{account_id}"

    count = await redis.get(key)

    if count is None:

        return True

    return int(count) < limit



async def incr_daily_count(account_id: int):

    key = f"daily_limit:{account_id}"

    await redis.incr(key)

    await redis.expire(key, 86400)



async def check_all_accounts_health(cooldown_minutes: int = 15, stale_execution_minutes: int = 10):
    cooldown_minutes = max(1, min(int(cooldown_minutes), 1440))
    stale_execution_minutes = max(1, min(int(stale_execution_minutes), 120))
    # Use one UTC snapshot for the whole pass. Besides keeping count/list/update
    # decisions on the same boundary, direct comparisons let MySQL use the
    # account/time indexes instead of evaluating TIMESTAMPDIFF for every row.
    evaluation_now = datetime.now(timezone.utc).replace(tzinfo=None)
    cooldown_cutoff = evaluation_now - timedelta(minutes=cooldown_minutes)
    stale_execution_cutoff = evaluation_now - timedelta(
        minutes=stale_execution_minutes
    )

    missing_credentials = await database.fetch_one(
        """SELECT COUNT(*) AS cnt FROM accounts
           WHERE status = 'ready' AND OCTET_LENGTH(encrypted_credential) = 0"""
    )
    missing_credential_accounts = await database.fetch_all(
        """SELECT id FROM accounts
           WHERE status = 'ready' AND OCTET_LENGTH(encrypted_credential) = 0
           LIMIT 500"""
    )
    await database.execute(
        """UPDATE accounts
           SET status = 'login_required', updated_at = NOW(), version = version + 1
           WHERE status = 'ready' AND OCTET_LENGTH(encrypted_credential) = 0"""
    )

    stale_executing = await database.fetch_one(
        """SELECT COUNT(*) AS cnt FROM accounts
           WHERE status = 'executing'
              AND updated_at <= :stale_execution_cutoff""",
        {"stale_execution_cutoff": stale_execution_cutoff},
    )
    stale_executing_accounts = await database.fetch_all(
        """SELECT id FROM accounts
           WHERE status = 'executing'
              AND updated_at <= :stale_execution_cutoff
           LIMIT 500""",
        {"stale_execution_cutoff": stale_execution_cutoff},
    )
    await database.execute(
        """INSERT INTO risk_events (account_id, event_type, detail)
           SELECT id, 'stale_execution',
                  JSON_OBJECT('reason', 'execution_timeout', 'minutes', :minutes)
           FROM accounts
           WHERE status = 'executing'
              AND updated_at <= :stale_execution_cutoff""",
        {
            "minutes": stale_execution_minutes,
            "stale_execution_cutoff": stale_execution_cutoff,
        },
    )
    await database.execute(
        """UPDATE accounts
           SET status = 'cooling', updated_at = NOW(), version = version + 1
           WHERE status = 'executing'
              AND updated_at <= :stale_execution_cutoff""",
        {"stale_execution_cutoff": stale_execution_cutoff},
    )

    cooling_ready = await database.fetch_one(
        """SELECT COUNT(*) AS cnt FROM accounts a
           LEFT JOIN proxies p ON a.proxy_id = p.id
           WHERE a.status = 'cooling'
              AND OCTET_LENGTH(a.encrypted_credential) > 0
              AND a.updated_at <= :cooldown_cutoff
              AND (a.proxy_id IS NULL OR p.cooldown_until IS NULL OR p.cooldown_until < NOW())
              AND NOT EXISTS (
                SELECT 1 FROM risk_events r
                WHERE r.account_id = a.id
                  AND r.created_at > :cooldown_cutoff
              )""",
        {"cooldown_cutoff": cooldown_cutoff},
    )
    cooling_ready_accounts = await database.fetch_all(
        """SELECT a.id FROM accounts a
           LEFT JOIN proxies p ON a.proxy_id = p.id
           WHERE a.status = 'cooling'
              AND OCTET_LENGTH(a.encrypted_credential) > 0
              AND a.updated_at <= :cooldown_cutoff
              AND (a.proxy_id IS NULL OR p.cooldown_until IS NULL OR p.cooldown_until < NOW())
              AND NOT EXISTS (
                SELECT 1 FROM risk_events r
                WHERE r.account_id = a.id
                  AND r.created_at > :cooldown_cutoff
              )
           LIMIT 500""",
        {"cooldown_cutoff": cooldown_cutoff},
    )
    await database.execute(
        """UPDATE accounts a
           LEFT JOIN proxies p ON a.proxy_id = p.id
           SET a.status = 'ready', a.updated_at = NOW(), a.version = a.version + 1
           WHERE a.status = 'cooling'
              AND OCTET_LENGTH(a.encrypted_credential) > 0
              AND a.updated_at <= :cooldown_cutoff
              AND (a.proxy_id IS NULL OR p.cooldown_until IS NULL OR p.cooldown_until < NOW())
              AND NOT EXISTS (
                SELECT 1 FROM risk_events r
                WHERE r.account_id = a.id
                  AND r.created_at > :cooldown_cutoff
              )""",
        {"cooldown_cutoff": cooldown_cutoff},
    )

    for row in missing_credential_accounts:
        await record_event(
            aggregate="account",
            aggregate_id=row["id"],
            event_type="AccountLoginRequired",
            payload={"reason": "missing_credential", "source": "health_recheck"},
        )
    for row in stale_executing_accounts:
        await record_event(
            aggregate="account",
            aggregate_id=row["id"],
            event_type="AccountCooling",
            payload={"reason": "stale_execution", "minutes": stale_execution_minutes, "source": "health_recheck"},
        )
        await record_event(
            aggregate="risk",
            aggregate_id=f"account:{row['id']}",
            event_type="RiskDetected",
            payload={"account_id": row["id"], "event_type": "stale_execution", "minutes": stale_execution_minutes},
        )
    for row in cooling_ready_accounts:
        await record_event(
            aggregate="account",
            aggregate_id=row["id"],
            event_type="AccountRestored",
            payload={"from": "cooling", "to": "ready", "cooldown_minutes": cooldown_minutes, "source": "health_recheck"},
        )

    await record_event(
        aggregate="system",
        aggregate_id="account_health",
        event_type="AccountHealthRecheckCompleted",
        payload={
            "cooldown_minutes": cooldown_minutes,
            "stale_execution_minutes": stale_execution_minutes,
            "ready_without_credential_moved_to_login_required": missing_credentials["cnt"],
            "stale_executing_moved_to_cooling": stale_executing["cnt"],
            "cooling_accounts_restored": cooling_ready["cnt"],
        },
    )

    return {
        "cooldown_minutes": cooldown_minutes,
        "stale_execution_minutes": stale_execution_minutes,
        "ready_without_credential_moved_to_login_required": missing_credentials["cnt"],
        "stale_executing_moved_to_cooling": stale_executing["cnt"],
        "cooling_accounts_restored": cooling_ready["cnt"],
        "login_required_auto_restored": 0,
        "frozen_auto_restored": 0,
    }
