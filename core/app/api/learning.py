"""Learning Runtime API (V6.5 / stage S5).

Exposes the advisory probability model: a transparent "model card" and
per-target predictions built from existing knowledge/strategy counters. The
math lives entirely in the pure modules ``app.learning.feature_store`` and
``app.learning.model``; this layer only gathers inputs.

Safety: predictions are advisory and explainable. They never relax the
real-run gate, the circuit breaker, or admin confirmation. These endpoints are
read-only.
"""

from fastapi import APIRouter

from app.api.lotteries import (
    STRATEGY_TARGET_METRICS_SQL,
    load_strategy_account_recommendations,
    load_strategy_platform_knowledge,
)
from app.db import database
from app.knowledge.scoring import as_int
from app.learning.feature_store import (
    FEATURE_DESCRIPTIONS,
    FEATURE_SET_VERSION,
    extract_features,
)
from app.learning.model import BIAS, MODEL_VERSION, WEIGHTS, explain_prediction
from app.strategy.engine import empty_platform_knowledge, first_or_none


router = APIRouter()


@router.get("/model")
async def model_card():
    """Return the model definition so its behavior is fully transparent."""
    return {
        "model_version": MODEL_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "type": "linear_logistic_advisory",
        "bias": BIAS,
        "weights": WEIGHTS,
        "feature_descriptions": FEATURE_DESCRIPTIONS,
        "advisory_only": True,
        "notes": "Hand-set baseline weights; never relaxes the real-run gate. Predictions are advisory.",
    }


@router.get("/predictions")
async def target_predictions(limit: int = 25):
    platform_knowledge = await load_strategy_platform_knowledge()
    account_recommendations = await load_strategy_account_recommendations()
    rows = await database.fetch_all(
        f"""SELECT l.*,
                  {STRATEGY_TARGET_METRICS_SQL}
           FROM lotteries l
           WHERE l.status IN ('pending','claimed')
           ORDER BY l.value_score DESC, l.id ASC
           LIMIT :limit""",
        {"limit": min(max(limit, 1), 100)},
    )

    items = []
    for row in rows:
        item = dict(row)
        platform = item["platform"]
        knowledge = platform_knowledge.get(platform) or empty_platform_knowledge(platform)
        recommended_account = first_or_none(account_recommendations.get(platform, []))
        account_reputation = recommended_account["reputation_score"] if recommended_account else 0
        feature_payload = extract_features(
            value_score=as_int(item["value_score"]),
            win_rate=knowledge.get("win_rate"),
            knowledge_confidence=as_int(knowledge.get("knowledge_confidence", 0)),
            account_reputation=account_reputation,
            dry_success=as_int(item.get("dry_success")),
            shadow_success=as_int(item.get("shadow_success")),
            recent_platform_risk=as_int(item.get("recent_platform_risk")),
        )
        prediction = explain_prediction(feature_payload["features"])
        items.append(
            {
                "lottery_id": item["id"],
                "platform": platform,
                "value_score": item["value_score"],
                "features": feature_payload["features"],
                "feature_version": feature_payload["version"],
                "probability": prediction["probability"],
                "confidence_band": prediction["confidence_band"],
                "model_version": prediction["model_version"],
                "top_drivers": prediction["top_drivers"],
                "advisory": True,
            }
        )

    items.sort(key=lambda entry: entry["probability"], reverse=True)
    return {"items": items, "count": len(items), "model_version": MODEL_VERSION}
