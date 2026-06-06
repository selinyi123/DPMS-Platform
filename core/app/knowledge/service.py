from datetime import datetime, timedelta
from typing import Any

from app.db import database
from app.platforms import get_platforms


async def build_knowledge_summary(window_days: int = 30) -> dict[str, Any]:
    window_days = max(1, min(int(window_days or 30), 365))
    cutoff = datetime.utcnow() - timedelta(days=window_days)

    platform_profiles = await build_platform_profiles(cutoff)
    account_profiles = await build_account_profiles(cutoff)
    risk_profile = await build_risk_profile(cutoff)
    lottery_profile = await build_lottery_profile(cutoff)
    event_profile = await build_event_profile(cutoff)
    summary = build_data_maturity(
        platform_profiles=platform_profiles,
        account_profiles=account_profiles,
        risk_profile=risk_profile,
        lottery_profile=lottery_profile,
        event_profile=event_profile,
    )
    learning_gaps = build_learning_gaps(
        summary=summary,
        platform_profiles=platform_profiles,
        account_profiles=account_profiles,
        risk_profile=risk_profile,
        lottery_profile=lottery_profile,
    )

    return {
        "runtime": "Knowledge Runtime",
        "version": "4.5-mvp",
        "window_days": window_days,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "data_sources": [
            "events",
            "lotteries",
            "task_runs",
            "risk_events",
            "accounts",
            "account_calibrations",
            "adapter_calibrations",
        ],
        "summary": summary,
        "platform_profiles": platform_profiles,
        "account_profiles": account_profiles,
        "risk_profile": risk_profile,
        "lottery_profile": lottery_profile,
        "event_profile": event_profile,
        "learning_gaps": learning_gaps,
    }


async def build_platform_profiles(cutoff: datetime) -> list[dict[str, Any]]:
    rows = await database.fetch_all(
        """SELECT p.platform,
                  COALESCE(a.accounts_total, 0) AS accounts_total,
                  COALESCE(a.ready_accounts, 0) AS ready_accounts,
                  COALESCE(a.calibrated_ready_accounts, 0) AS calibrated_ready_accounts,
                  COALESCE(l.total_lotteries, 0) AS total_lotteries,
                  COALESCE(l.pending_lotteries, 0) AS pending_lotteries,
                  COALESCE(l.claimed_lotteries, 0) AS claimed_lotteries,
                  COALESCE(l.participated_lotteries, 0) AS participated_lotteries,
                  COALESCE(l.won_lotteries, 0) AS won_lotteries,
                  COALESCE(l.lost_lotteries, 0) AS lost_lotteries,
                  COALESCE(l.high_value_lotteries, 0) AS high_value_lotteries,
                  COALESCE(l.avg_value_score, 0) AS avg_value_score,
                  l.latest_lottery_at,
                  COALESCE(t.total_runs, 0) AS total_runs,
                  COALESCE(t.succeeded_runs, 0) AS succeeded_runs,
                  COALESCE(t.failed_runs, 0) AS failed_runs,
                  COALESCE(t.dry_success, 0) AS dry_success,
                  COALESCE(t.shadow_success, 0) AS shadow_success,
                  COALESCE(t.real_success, 0) AS real_success,
                  t.latest_run_at,
                  COALESCE(r.risk_events, 0) AS risk_events,
                  r.latest_risk_at,
                  COALESCE(e.event_count, 0) AS event_count,
                  e.latest_event_at,
                  COALESCE(probe.succeeded_probes, 0) AS succeeded_probes,
                  probe.latest_probe_at
           FROM (
             SELECT platform FROM lotteries
             UNION SELECT platform FROM accounts
             UNION SELECT platform FROM adapter_calibrations
             UNION SELECT aggregate_id AS platform FROM events WHERE aggregate = 'platform'
           ) p
           LEFT JOIN (
             SELECT a.platform,
                    COUNT(*) AS accounts_total,
                    SUM(a.status = 'ready') AS ready_accounts,
                    SUM(a.status = 'ready'
                        AND OCTET_LENGTH(a.encrypted_credential) > 0
                        AND (
                          SELECT c.status
                          FROM account_calibrations c
                          WHERE c.account_id = a.id
                          ORDER BY c.created_at DESC
                          LIMIT 1
                        ) = 'succeeded') AS calibrated_ready_accounts
             FROM accounts a
             GROUP BY a.platform
           ) a ON a.platform = p.platform
           LEFT JOIN (
             SELECT platform,
                    COUNT(*) AS total_lotteries,
                    SUM(status = 'pending') AS pending_lotteries,
                    SUM(status = 'claimed') AS claimed_lotteries,
                    SUM(status = 'participated') AS participated_lotteries,
                    SUM(status = 'won') AS won_lotteries,
                    SUM(status = 'lost') AS lost_lotteries,
                    SUM(value_score >= 70) AS high_value_lotteries,
                    AVG(value_score) AS avg_value_score,
                    MAX(extracted_at) AS latest_lottery_at
             FROM lotteries
             WHERE extracted_at >= :cutoff
             GROUP BY platform
           ) l ON l.platform = p.platform
           LEFT JOIN (
             SELECT l.platform,
                    COUNT(*) AS total_runs,
                    SUM(tr.status = 'succeeded') AS succeeded_runs,
                    SUM(tr.status = 'failed') AS failed_runs,
                    SUM(COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'dry_run'
                        AND tr.status = 'succeeded') AS dry_success,
                    SUM(COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'shadow_run'
                        AND tr.status = 'succeeded') AS shadow_success,
                    SUM(COALESCE(tr.task_mode, IF(tr.dry_run = 1, 'dry_run', 'real_run')) = 'real_run'
                        AND tr.status = 'succeeded') AS real_success,
                    MAX(tr.created_at) AS latest_run_at
             FROM task_runs tr
             JOIN lotteries l ON l.id = tr.lottery_id
             WHERE tr.created_at >= :cutoff
             GROUP BY l.platform
           ) t ON t.platform = p.platform
           LEFT JOIN (
             SELECT a.platform,
                    COUNT(*) AS risk_events,
                    MAX(r.created_at) AS latest_risk_at
             FROM risk_events r
             JOIN accounts a ON a.id = r.account_id
             WHERE r.created_at >= :cutoff
             GROUP BY a.platform
           ) r ON r.platform = p.platform
           LEFT JOIN (
             SELECT aggregate_id AS platform,
                    COUNT(*) AS event_count,
                    MAX(occurred_at) AS latest_event_at
             FROM events
             WHERE aggregate = 'platform'
               AND occurred_at >= :cutoff
             GROUP BY aggregate_id
           ) e ON e.platform = p.platform
           LEFT JOIN (
             SELECT platform,
                    SUM(status = 'succeeded') AS succeeded_probes,
                    MAX(created_at) AS latest_probe_at
             FROM adapter_calibrations
             WHERE created_at >= :cutoff
             GROUP BY platform
           ) probe ON probe.platform = p.platform
           ORDER BY p.platform""",
        {"cutoff": cutoff},
    )

    profiles_by_platform = {}
    for row in rows:
        item = dict(row)
        platform = item["platform"]
        labels = get_platforms().get(platform, {})
        won = as_int(item["won_lotteries"])
        lost = as_int(item["lost_lotteries"])
        total_runs = as_int(item["total_runs"])
        succeeded_runs = as_int(item["succeeded_runs"])
        total_lotteries = as_int(item["total_lotteries"])
        risk_events = as_int(item["risk_events"])
        confidence = confidence_score(
            total_lotteries=total_lotteries,
            result_labels=won + lost,
            task_runs=total_runs,
            shadow_success=as_int(item["shadow_success"]),
            real_success=as_int(item["real_success"]),
            event_count=as_int(item["event_count"]),
        )
        profiles_by_platform[platform] = {
            "platform": platform,
            "label": labels.get("label", platform),
            "accounts_total": as_int(item["accounts_total"]),
            "ready_accounts": as_int(item["ready_accounts"]),
            "calibrated_ready_accounts": as_int(item["calibrated_ready_accounts"]),
            "total_lotteries": total_lotteries,
            "pending_lotteries": as_int(item["pending_lotteries"]),
            "claimed_lotteries": as_int(item["claimed_lotteries"]),
            "participated_lotteries": as_int(item["participated_lotteries"]),
            "won_lotteries": won,
            "lost_lotteries": lost,
            "high_value_lotteries": as_int(item["high_value_lotteries"]),
            "avg_value_score": round(as_float(item["avg_value_score"]), 2),
            "win_rate": ratio(won, won + lost),
            "total_runs": total_runs,
            "succeeded_runs": succeeded_runs,
            "failed_runs": as_int(item["failed_runs"]),
            "task_success_rate": ratio(succeeded_runs, total_runs),
            "dry_success": as_int(item["dry_success"]),
            "shadow_success": as_int(item["shadow_success"]),
            "real_success": as_int(item["real_success"]),
            "risk_events": risk_events,
            "event_count": as_int(item["event_count"]),
            "succeeded_probes": as_int(item["succeeded_probes"]),
            "latest_lottery_at": item["latest_lottery_at"],
            "latest_run_at": item["latest_run_at"],
            "latest_risk_at": item["latest_risk_at"],
            "latest_event_at": item["latest_event_at"],
            "latest_probe_at": item["latest_probe_at"],
            "knowledge_confidence": confidence,
        }

    for platform, cfg in get_platforms().items():
        if platform not in profiles_by_platform:
            profiles_by_platform[platform] = {
                "platform": platform,
                "label": cfg.get("label", platform),
                "accounts_total": 0,
                "ready_accounts": 0,
                "calibrated_ready_accounts": 0,
                "total_lotteries": 0,
                "pending_lotteries": 0,
                "claimed_lotteries": 0,
                "participated_lotteries": 0,
                "won_lotteries": 0,
                "lost_lotteries": 0,
                "high_value_lotteries": 0,
                "avg_value_score": 0,
                "win_rate": None,
                "total_runs": 0,
                "succeeded_runs": 0,
                "failed_runs": 0,
                "task_success_rate": None,
                "dry_success": 0,
                "shadow_success": 0,
                "real_success": 0,
                "risk_events": 0,
                "event_count": 0,
                "succeeded_probes": 0,
                "latest_lottery_at": None,
                "latest_run_at": None,
                "latest_risk_at": None,
                "latest_event_at": None,
                "latest_probe_at": None,
                "knowledge_confidence": 0,
            }

    return sorted(
        profiles_by_platform.values(),
        key=lambda item: (item["knowledge_confidence"], item["total_lotteries"], item["total_runs"]),
        reverse=True,
    )


async def build_account_profiles(cutoff: datetime) -> list[dict[str, Any]]:
    rows = await database.fetch_all(
        """SELECT a.id,
                  a.platform,
                  a.status,
                  a.risk_score,
                  a.daily_task_count,
                  a.last_active_at,
                  a.created_at,
                  (
                    SELECT c.status
                    FROM account_calibrations c
                    WHERE c.account_id = a.id
                    ORDER BY c.created_at DESC
                    LIMIT 1
                  ) AS latest_calibration_status,
                  COALESCE(t.total_runs, 0) AS total_runs,
                  COALESCE(t.succeeded_runs, 0) AS succeeded_runs,
                  COALESCE(t.failed_runs, 0) AS failed_runs,
                  COALESCE(t.dry_runs, 0) AS dry_runs,
                  COALESCE(t.shadow_runs, 0) AS shadow_runs,
                  COALESCE(t.real_runs, 0) AS real_runs,
                  t.latest_run_at,
                  COALESCE(r.risk_events, 0) AS risk_events,
                  r.latest_risk_at,
                  (
                    SELECT r2.event_type
                    FROM risk_events r2
                    WHERE r2.account_id = a.id
                    ORDER BY r2.created_at DESC
                    LIMIT 1
                  ) AS latest_risk_type
           FROM accounts a
           LEFT JOIN (
             SELECT account_id,
                    COUNT(*) AS total_runs,
                    SUM(status = 'succeeded') AS succeeded_runs,
                    SUM(status = 'failed') AS failed_runs,
                    SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'dry_run') AS dry_runs,
                    SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'shadow_run') AS shadow_runs,
                    SUM(COALESCE(task_mode, IF(dry_run = 1, 'dry_run', 'real_run')) = 'real_run') AS real_runs,
                    MAX(created_at) AS latest_run_at
             FROM task_runs
             WHERE created_at >= :cutoff
             GROUP BY account_id
           ) t ON t.account_id = a.id
           LEFT JOIN (
             SELECT account_id,
                    COUNT(*) AS risk_events,
                    MAX(created_at) AS latest_risk_at
             FROM risk_events
             WHERE created_at >= :cutoff
             GROUP BY account_id
           ) r ON r.account_id = a.id
           ORDER BY a.id DESC""",
        {"cutoff": cutoff},
    )

    profiles = []
    for row in rows:
        item = dict(row)
        total_runs = as_int(item["total_runs"])
        succeeded_runs = as_int(item["succeeded_runs"])
        failed_runs = as_int(item["failed_runs"])
        risk_events = as_int(item["risk_events"])
        reputation = account_reputation(
            status=item["status"],
            risk_score=as_int(item["risk_score"]),
            latest_calibration_status=item.get("latest_calibration_status"),
            total_runs=total_runs,
            succeeded_runs=succeeded_runs,
            failed_runs=failed_runs,
            shadow_runs=as_int(item["shadow_runs"]),
            real_runs=as_int(item["real_runs"]),
            risk_events=risk_events,
        )
        profiles.append(
            {
                "account_id": item["id"],
                "platform": item["platform"],
                "status": item["status"],
                "risk_score": as_int(item["risk_score"]),
                "daily_task_count": as_int(item["daily_task_count"]),
                "latest_calibration_status": item["latest_calibration_status"],
                "total_runs": total_runs,
                "succeeded_runs": succeeded_runs,
                "failed_runs": failed_runs,
                "dry_runs": as_int(item["dry_runs"]),
                "shadow_runs": as_int(item["shadow_runs"]),
                "real_runs": as_int(item["real_runs"]),
                "task_success_rate": ratio(succeeded_runs, total_runs),
                "risk_events": risk_events,
                "latest_risk_type": item["latest_risk_type"],
                "latest_run_at": item["latest_run_at"],
                "latest_risk_at": item["latest_risk_at"],
                "last_active_at": item["last_active_at"],
                "created_at": item["created_at"],
                "reputation_score": reputation,
            }
        )
    return sorted(profiles, key=lambda item: item["reputation_score"], reverse=True)


async def build_risk_profile(cutoff: datetime) -> dict[str, Any]:
    type_rows = await database.fetch_all(
        """SELECT event_type,
                  COUNT(*) AS count,
                  COUNT(DISTINCT account_id) AS accounts,
                  MAX(created_at) AS latest_at
           FROM risk_events
           WHERE created_at >= :cutoff
           GROUP BY event_type
           ORDER BY count DESC, event_type ASC""",
        {"cutoff": cutoff},
    )
    platform_rows = await database.fetch_all(
        """SELECT a.platform,
                  COUNT(*) AS count,
                  COUNT(DISTINCT r.account_id) AS accounts,
                  MAX(r.created_at) AS latest_at
           FROM risk_events r
           JOIN accounts a ON a.id = r.account_id
           WHERE r.created_at >= :cutoff
           GROUP BY a.platform
           ORDER BY count DESC, a.platform ASC""",
        {"cutoff": cutoff},
    )
    return {
        "by_type": [
            {
                "event_type": row["event_type"],
                "count": as_int(row["count"]),
                "accounts": as_int(row["accounts"]),
                "latest_at": row["latest_at"],
            }
            for row in type_rows
        ],
        "by_platform": [
            {
                "platform": row["platform"],
                "count": as_int(row["count"]),
                "accounts": as_int(row["accounts"]),
                "latest_at": row["latest_at"],
            }
            for row in platform_rows
        ],
    }


async def build_lottery_profile(cutoff: datetime) -> dict[str, Any]:
    band_rows = await database.fetch_all(
        """SELECT CASE
                    WHEN value_score >= 70 THEN 'high'
                    WHEN value_score >= 40 THEN 'medium'
                    ELSE 'low'
                  END AS value_band,
                  COUNT(*) AS sample,
                  SUM(status = 'pending') AS pending,
                  SUM(status = 'won') AS won,
                  SUM(status = 'lost') AS lost,
                  SUM(status = 'participated') AS participated,
                  AVG(value_score) AS avg_value_score
           FROM lotteries
           WHERE extracted_at >= :cutoff
           GROUP BY value_band
           ORDER BY FIELD(value_band, 'high', 'medium', 'low')""",
        {"cutoff": cutoff},
    )
    source_rows = await database.fetch_all(
        """SELECT source_type,
                  COUNT(*) AS sample,
                  SUM(status = 'won') AS won,
                  SUM(status = 'lost') AS lost,
                  AVG(value_score) AS avg_value_score
           FROM lotteries
           WHERE extracted_at >= :cutoff
           GROUP BY source_type
           ORDER BY sample DESC
           LIMIT 10""",
        {"cutoff": cutoff},
    )
    return {
        "by_value_band": [
            {
                "value_band": row["value_band"],
                "sample": as_int(row["sample"]),
                "pending": as_int(row["pending"]),
                "won": as_int(row["won"]),
                "lost": as_int(row["lost"]),
                "participated": as_int(row["participated"]),
                "avg_value_score": round(as_float(row["avg_value_score"]), 2),
                "win_rate": ratio(as_int(row["won"]), as_int(row["won"]) + as_int(row["lost"])),
            }
            for row in band_rows
        ],
        "by_source_type": [
            {
                "source_type": row["source_type"],
                "sample": as_int(row["sample"]),
                "won": as_int(row["won"]),
                "lost": as_int(row["lost"]),
                "avg_value_score": round(as_float(row["avg_value_score"]), 2),
                "win_rate": ratio(as_int(row["won"]), as_int(row["won"]) + as_int(row["lost"])),
            }
            for row in source_rows
        ],
    }


async def build_event_profile(cutoff: datetime) -> dict[str, Any]:
    rows = await database.fetch_all(
        """SELECT aggregate,
                  event_type,
                  COUNT(*) AS count,
                  MAX(occurred_at) AS latest_at
           FROM events
           WHERE occurred_at >= :cutoff
           GROUP BY aggregate, event_type
           ORDER BY count DESC, latest_at DESC
           LIMIT 30""",
        {"cutoff": cutoff},
    )
    total = await database.fetch_one(
        "SELECT COUNT(*) AS count FROM events WHERE occurred_at >= :cutoff",
        {"cutoff": cutoff},
    )
    return {
        "total_events": as_int(total["count"] if total else 0),
        "top_event_types": [
            {
                "aggregate": row["aggregate"],
                "event_type": row["event_type"],
                "count": as_int(row["count"]),
                "latest_at": row["latest_at"],
            }
            for row in rows
        ],
    }


def build_data_maturity(
    *,
    platform_profiles: list[dict[str, Any]],
    account_profiles: list[dict[str, Any]],
    risk_profile: dict[str, Any],
    lottery_profile: dict[str, Any],
    event_profile: dict[str, Any],
) -> dict[str, Any]:
    total_events = as_int(event_profile.get("total_events"))
    total_lotteries = sum(item["total_lotteries"] for item in platform_profiles)
    total_runs = sum(item["total_runs"] for item in platform_profiles)
    result_labels = sum(item["won_lotteries"] + item["lost_lotteries"] for item in platform_profiles)
    shadow_success = sum(item["shadow_success"] for item in platform_profiles)
    real_success = sum(item["real_success"] for item in platform_profiles)
    risk_events = sum(item["count"] for item in risk_profile.get("by_type", []))
    platforms_with_data = sum(1 for item in platform_profiles if item["total_lotteries"] or item["total_runs"] or item["event_count"])
    score = clamp(
        min(total_events, 30)
        + min(result_labels * 6, 20)
        + min(total_runs * 3, 20)
        + min(shadow_success * 6, 15)
        + min(platforms_with_data * 4, 10)
        + min(risk_events * 2, 5),
        0,
        100,
    )
    if score >= 70:
        level = "learning_ready"
    elif score >= 40:
        level = "usable"
    elif score >= 15:
        level = "warming"
    else:
        level = "cold_start"
    return {
        "data_maturity_score": score,
        "data_maturity_level": level,
        "total_events": total_events,
        "total_lotteries": total_lotteries,
        "total_runs": total_runs,
        "result_labels": result_labels,
        "shadow_success": shadow_success,
        "real_success": real_success,
        "risk_events": risk_events,
        "platforms_with_data": platforms_with_data,
        "accounts_profiled": len(account_profiles),
        "value_bands_profiled": len(lottery_profile.get("by_value_band", [])),
    }


def build_learning_gaps(
    *,
    summary: dict[str, Any],
    platform_profiles: list[dict[str, Any]],
    account_profiles: list[dict[str, Any]],
    risk_profile: dict[str, Any],
    lottery_profile: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps = []
    if summary["total_events"] == 0:
        gaps.append(gap("event_memory_empty", "P0", "Event Store has no recent events", "Run discovery, account calibration, dry-run, or target import so Knowledge Runtime can build history."))
    if summary["result_labels"] < 5:
        gaps.append(gap("result_labels_low", "P1", "Lottery result labels are too sparse", "Mark won/lost results after each known outcome to train future win-rate estimates."))
    if summary["shadow_success"] < 3:
        gaps.append(gap("shadow_evidence_low", "P1", "Shadow-run evidence is still thin", "Collect several successful shadow-runs before relying on real-run recommendations."))
    if not account_profiles:
        gaps.append(gap("account_profiles_empty", "P0", "No account profiles can be built", "Add accounts through QR login or Cookie import, then calibrate them."))
    cold_platforms = [
        item["platform"]
        for item in platform_profiles
        if item["total_lotteries"] < 3 and item["total_runs"] < 3
    ]
    if cold_platforms:
        gaps.append(gap("platform_samples_low", "P2", "Some platforms have low sample counts", "Import or discover more targets before comparing platform-level win or risk rates.", {"platforms": cold_platforms[:8]}))
    if summary["risk_events"] == 0:
        gaps.append(gap("risk_observations_empty", "P2", "Risk model has no recent observations", "This can mean the system is quiet or safe; keep risk event recording enabled for captcha, rate-limit, review, and ban signals."))
    if not lottery_profile.get("by_value_band"):
        gaps.append(gap("lottery_value_profile_empty", "P2", "No value-band lottery profile exists", "Import targets with value_score so the strategy layer can compare expected value bands."))
    return gaps[:8]


def gap(code: str, priority: str, title: str, detail: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "priority": priority,
        "title": title,
        "detail": detail,
        "evidence": evidence or {},
    }


def account_reputation(
    *,
    status: str,
    risk_score: int,
    latest_calibration_status: str | None,
    total_runs: int,
    succeeded_runs: int,
    failed_runs: int,
    shadow_runs: int,
    real_runs: int,
    risk_events: int,
) -> int:
    score = 70
    score += min(succeeded_runs * 2, 12)
    score += min(shadow_runs * 2, 8)
    score += min(real_runs * 3, 9)
    score += 8 if latest_calibration_status == "succeeded" else 0
    score -= min(failed_runs * 6, 24)
    score -= min(risk_events * 12, 36)
    score -= min(risk_score, 40)
    if status in {"frozen", "banned"}:
        score -= 35
    elif status in {"login_required", "cooling"}:
        score -= 18
    elif status == "ready" and total_runs == 0:
        score += 4
    return clamp(score, 0, 100)


def confidence_score(
    *,
    total_lotteries: int,
    result_labels: int,
    task_runs: int,
    shadow_success: int,
    real_success: int,
    event_count: int,
) -> int:
    return clamp(
        min(total_lotteries * 3, 24)
        + min(result_labels * 8, 24)
        + min(task_runs * 4, 20)
        + min(shadow_success * 6, 18)
        + min(real_success * 7, 14)
        + min(event_count * 2, 10),
        0,
        100,
    )


def ratio(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def clamp(value: int | float, low: int, high: int) -> int:
    return int(max(low, min(high, round(value))))
