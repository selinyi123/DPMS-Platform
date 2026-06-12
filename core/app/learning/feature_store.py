"""Pure feature extraction for the DPMS Learning Runtime (V6.5 / stage S5).

No database or framework dependency. Turns plain knowledge/strategy counters
into a named, normalized, versioned feature vector that the probability model
(``app.learning.model``) consumes. Keeping extraction separate and pure means
every feature is explainable and the same inputs always yield the same vector
(decisions are replayable).

Safety: features describe *targets and accounts*; they are inputs to an
advisory probability only. Nothing here feeds the real-run gate.
"""

from __future__ import annotations

# Bump when the feature definitions change so stored predictions remain
# interpretable against the feature set that produced them.
FEATURE_SET_VERSION = "fs-1.0"

# Human-readable descriptions, surfaced by the API so operators can see what
# each feature means rather than reverse-engineering it.
FEATURE_DESCRIPTIONS = {
    "value": "Normalized target value score (0-100 -> 0-1).",
    "win_rate": "Observed platform win rate, 0 when unknown.",
    "knowledge_confidence": "Confidence in the platform knowledge sample (0-100 -> 0-1).",
    "account_reputation": "Recommended account reputation (0-100 -> 0-1).",
    "validation_depth": "Dry/shadow validation progress for the target (0-1).",
    "recent_risk": "Recent platform risk pressure, higher = riskier (0-1).",
}


def extract_features(
    *,
    value_score: int,
    win_rate: float | None,
    knowledge_confidence: int,
    account_reputation: int,
    dry_success: int,
    shadow_success: int,
    recent_platform_risk: int,
) -> dict:
    """Build the normalized feature vector for one target/account pairing.

    Returns ``{"version", "features", "raw"}`` where ``features`` are all in
    [0, 1] and ``raw`` preserves the pre-normalization inputs for auditing.
    """
    features = {
        "value": _unit(value_score / 100),
        "win_rate": _unit(float(win_rate) if win_rate is not None else 0.0),
        "knowledge_confidence": _unit(knowledge_confidence / 100),
        "account_reputation": _unit(account_reputation / 100),
        "validation_depth": _validation_depth(dry_success, shadow_success),
        "recent_risk": _unit(min(recent_platform_risk, 10) / 10),
    }
    return {
        "version": FEATURE_SET_VERSION,
        "features": {key: round(value, 4) for key, value in features.items()},
        "raw": {
            "value_score": value_score,
            "win_rate": win_rate,
            "knowledge_confidence": knowledge_confidence,
            "account_reputation": account_reputation,
            "dry_success": dry_success,
            "shadow_success": shadow_success,
            "recent_platform_risk": recent_platform_risk,
        },
    }


def _validation_depth(dry_success: int, shadow_success: int) -> float:
    # Shadow evidence is worth more than dry evidence; saturates so a single
    # noisy success does not dominate.
    depth = min(max(dry_success, 0), 3) * 0.12 + min(max(shadow_success, 0), 3) * 0.21
    return _unit(depth)


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))
