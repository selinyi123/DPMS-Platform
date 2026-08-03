"""Shared exception identities for platform-owned execution paths."""

from __future__ import annotations


class BilibiliForwardedTargetRequiresReview(RuntimeError):
    """The API target differs from the dynamic that the operator reviewed."""


class BilibiliActionSettlementFailed(RuntimeError):
    """A known API result could not be durably recorded locally."""

    quarantine_account = True

    def __init__(self, action: str, action_result, cause: BaseException) -> None:
        self.action = action
        self.action_result = action_result
        self.reason = "confirmed_result_persistence_failed"
        super().__init__(
            "bilibili_action_settlement_failed:"
            f"{action}:{type(cause).__name__}"
        )


class ExternalActionOutcomeUnknown(RuntimeError):
    """A remote mutation may have happened but was not durably settled."""

    quarantine_account = True

    def __init__(self, platform: str, action: str, cause: BaseException) -> None:
        self.platform = str(platform or "unknown").strip().lower() or "unknown"
        self.action = str(action or "unknown").strip().lower() or "unknown"
        self.reason = f"{self.platform}_{self.action}_outcome_unknown"
        super().__init__(
            "external_action_outcome_unknown:"
            f"{self.platform}:{self.action}:{type(cause).__name__}"
        )


__all__ = (
    "BilibiliActionSettlementFailed",
    "BilibiliForwardedTargetRequiresReview",
    "ExternalActionOutcomeUnknown",
)
