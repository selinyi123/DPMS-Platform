"""Advisory probability model for the DPMS Learning Runtime (V6.5 / stage S5).

A deliberately transparent linear-logistic scorer: a fixed bias plus named
per-feature weights, passed through a sigmoid. It is **not** trained on hidden
data; the weights are a documented, hand-set baseline so every prediction is
fully explainable and reproducible. The API records the model version with each
prediction so outputs stay interpretable as the model evolves.

Safety boundary (non-negotiable):

- The output is an *advisory* probability and ranking signal only. It must
  never relax or substitute for the real-run evidence gate, the circuit
  breaker, or admin confirmation.
- Every prediction exposes its per-feature contributions so an operator (or an
  audit) can replay exactly why a number was produced.
"""

from __future__ import annotations

import math

MODEL_VERSION = "lm-1.0"

# logit = BIAS + sum(WEIGHTS[f] * features[f]). Positive weights raise the
# predicted success probability, the single negative weight (recent_risk)
# lowers it. Chosen to be conservative: the bias is strongly negative so a
# cold-start target reads as low probability rather than a coin flip.
BIAS = -2.2
WEIGHTS = {
    "value": 1.1,
    "win_rate": 2.4,
    "knowledge_confidence": 0.9,
    "account_reputation": 1.3,
    "validation_depth": 1.6,
    "recent_risk": -1.8,
}


def predict_success_probability(features: dict) -> dict:
    """Score a feature vector into an advisory success probability.

    ``features`` is the ``features`` dict from
    ``app.learning.feature_store.extract_features``. Returns the probability,
    the raw logit, and per-feature contributions (in logit space) so the
    prediction can be explained and replayed.
    """
    contributions = {}
    logit = BIAS
    for name, weight in WEIGHTS.items():
        value = float(features.get(name, 0.0) or 0.0)
        contribution = weight * value
        contributions[name] = round(contribution, 4)
        logit += contribution
    probability = _sigmoid(logit)
    return {
        "model_version": MODEL_VERSION,
        "probability": round(probability, 4),
        "logit": round(logit, 4),
        "bias": BIAS,
        "contributions": contributions,
        "advisory": True,
        "confidence_band": _band(probability),
    }


def explain_prediction(features: dict) -> dict:
    """Return the prediction plus the contributions sorted by influence.

    Convenience wrapper for the explain surface so the UI can show the top
    drivers without re-sorting.
    """
    prediction = predict_success_probability(features)
    ordered = sorted(
        prediction["contributions"].items(),
        key=lambda item: abs(item[1]),
        reverse=True,
    )
    prediction["top_drivers"] = [{"feature": name, "contribution": value} for name, value in ordered]
    return prediction


def _sigmoid(x: float) -> float:
    # Guard against overflow for extreme logits.
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _band(probability: float) -> str:
    if probability >= 0.5:
        return "high"
    if probability >= 0.25:
        return "moderate"
    if probability >= 0.1:
        return "low"
    return "very_low"
