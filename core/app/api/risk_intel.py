"""Risk Intelligence API (V6 / stage S4).

Aggregates per-account counters and runs them through the pure forecasting
engine (``app.risk.intelligence``). The engine owns all judgement; this module
only reads existing tables (accounts, risk_events, task_runs,
account_calibrations).

Safety: every recommended action comes from the engine's fixed safe set and is
only ever used to *tighten* dispatch. These endpoints are read-only and never
change account state or the real-run gate.
"""

from fastapi import APIRouter, HTTPException

from app.db import database
from app.knowledge.scoring import account_reputation, as_int
from app.risk.intelligence import forecast_account_risk


router = APIRouter()

HARD_RISK_EVENT_TYPES = ("captcha", "ban", "banned", "verification", "frozen", "security")
HARD_RISK_REGEX = "|".join(HARD_RISK_EVENT_TYPES)


@router.get("/intelligence")
async def risk_intelligence(limit: int = 100):
    rows = await _account_risk_rows(limit=min(max(limit, 1), 200))
    items = [_forecast_row(row) for row in rows]
    # Sort riskiest first so the operator sees what needs tightening at the top.
    items.sort(key=lambda item: (item["risk_score"], item["forecast_24h"]), reverse=True)
    bands = {"high": 0, "elevated": 0, "moderate": 0, "low": 0}
    for item in items:
        bands[item["forecast_band"]] = bands.get(item["forecast_band"], 0) + 1
    return {"items": items, "count": len(items), "bands": bands}


@router.get("/intelligence/{account_id}")
async def account_risk_intelligence(account_id: int):
    rows = await _account_risk_rows(account_id=account_id)
    if not rows:
        raise HTTPException(404, detail="Account not found")
    return _forecast_row(rows[0])


def _forecast_row(row: dict) -> dict:
    reputation = account_reputation(
        status=row["status"],
        risk_score=as_int(row["risk_score"]),
        latest_calibration_status=row.get("latest_calibration_status"),
        total_runs=as_int(row["total_runs"]),
        succeeded_runs=as_int(row["succeeded_runs"]),
        failed_runs=as_int(row["failed_runs"]),
        shadow_runs=as_int(row["shadow_runs"]),
        real_runs=as_int(row["real_runs"]),
        risk_events=as_int(row["risk_events_7d"]),
    )
    forecast = forecast_account_risk(
        reputation_score=reputation,
        risk_events_24h=as_int(row["risk_events_24h"]),
        risk_events_7d=as_int(row["risk_events_7d"]),
        status=row["status"],
        latest_calibration_status=row.get("latest_calibration_status"),
        consecutive_failures=as_int(row["consecutive_failures"]),
        daily_task_count=as_int(row["daily_task_count"]),
        hard_signal_24h=bool(as_int(row["hard_signal_24h"])),
    )
    return {
        "account_id": row["id"],
        "platform": row["platform"],
        "status": row["status"],
        "reputation_score": reputation,
        **forecast,
    }


async def _account_risk_rows(account_id: int | None = None, limit: int = 100) -> list[dict]:
    account_filter = "WHERE a.id = :account_id AND a.deleted_at IS NULL" if account_id is not None else "WHERE a.deleted_at IS NULL"
    values: dict = {"hard_regex": HARD_RISK_REGEX}
    if account_id is not None:
        values["account_id"] = account_id
    else:
        values["limit"] = limit
    limit_clause = "" if account_id is not None else "ORDER BY a.id DESC LIMIT :limit"

    rows = await database.fetch_all(
        f"""SELECT a.id,
                   a.platform,
                   a.status,
                   a.risk_score,
                   a.daily_task_count,
                   (
                     SELECT c.status FROM account_calibrations c
                     WHERE c.account_id = a.id
                     ORDER BY c.created_at DESC LIMIT 1
                   ) AS latest_calibration_status,
                   COALESCE(t.total_runs, 0) AS total_runs,
                   COALESCE(t.succeeded_runs, 0) AS succeeded_runs,
                   COALESCE(t.failed_runs, 0) AS failed_runs,
                   COALESCE(t.shadow_runs, 0) AS shadow_runs,
                   COALESCE(t.real_runs, 0) AS real_runs,
                   COALESCE(t.consecutive_failures, 0) AS consecutive_failures,
                   COALESCE(r.risk_events_24h, 0) AS risk_events_24h,
                   COALESCE(r.risk_events_7d, 0) AS risk_events_7d,
                   COALESCE(r.hard_signal_24h, 0) AS hard_signal_24h
            FROM accounts a
            LEFT JOIN (
              SELECT account_id,
                     COUNT(*) AS total_runs,
                     SUM(status = 'succeeded') AS succeeded_runs,
                     SUM(status = 'failed') AS failed_runs,
                     SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'shadow_run') AS shadow_runs,
                     SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'real_run') AS real_runs,
                     SUM(status = 'failed'
                         AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS consecutive_failures
              FROM task_runs
              WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
              GROUP BY account_id
            ) t ON t.account_id = a.id
            LEFT JOIN (
              SELECT account_id,
                     SUM(created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)) AS risk_events_24h,
                     COUNT(*) AS risk_events_7d,
                     MAX(created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
                         AND LOWER(event_type) REGEXP :hard_regex) AS hard_signal_24h
              FROM risk_events
              WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
              GROUP BY account_id
            ) r ON r.account_id = a.id
            {account_filter}
            {limit_clause}""",
        values,
    )
    return [dict(row) for row in rows]
