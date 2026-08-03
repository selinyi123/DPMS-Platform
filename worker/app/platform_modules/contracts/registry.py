"""Lazy registry for platform-owned Action Plan contracts."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator, Mapping

from .base import LEGACY_FALLBACK_CONTRACT, PlatformActionPlanContract


_CONTRACT_SPECS = {
    "bilibili": (
        f"{__package__}.bilibili",
        "BILIBILI_ACTION_PLAN_CONTRACT",
    ),
    "weibo": (f"{__package__}.weibo", "WEIBO_ACTION_PLAN_CONTRACT"),
    "xiaohongshu": (
        f"{__package__}.xiaohongshu",
        "XIAOHONGSHU_ACTION_PLAN_CONTRACT",
    ),
    "douyin": (f"{__package__}.douyin", "DOUYIN_ACTION_PLAN_CONTRACT"),
}


class PlatformActionPlanContractUnavailableError(RuntimeError):
    def __init__(self, platform: str) -> None:
        self.platform = str(platform or "").strip().casefold()
        super().__init__(
            f"platform_action_plan_contract_unavailable:"
            f"{self.platform or 'missing'}"
        )


class LazyActionPlanContracts(Mapping[str, PlatformActionPlanContract]):
    """Import exactly the contract requested by an untrusted task plan."""

    def __init__(self) -> None:
        self._contracts: dict[str, PlatformActionPlanContract] = {}
        self._failures: dict[str, BaseException] = {}
        self._lock = threading.RLock()

    def __getitem__(self, platform: str) -> PlatformActionPlanContract:
        key = str(platform or "").strip().casefold()
        spec = _CONTRACT_SPECS.get(key)
        if spec is None:
            raise KeyError(key)
        with self._lock:
            cached = self._contracts.get(key)
            if cached is not None:
                return cached
            if key in self._failures:
                raise PlatformActionPlanContractUnavailableError(
                    key
                ) from self._failures[key]
            try:
                module_name, export_name = spec
                contract = getattr(
                    importlib.import_module(module_name),
                    export_name,
                )
                if (
                    not isinstance(contract, PlatformActionPlanContract)
                    or contract.platform_id != key
                ):
                    raise TypeError(
                        f"platform_action_plan_contract_invalid:{key}"
                    )
            except Exception as exc:
                self._failures[key] = exc
                raise PlatformActionPlanContractUnavailableError(
                    key
                ) from exc
            self._contracts[key] = contract
            return contract

    def __iter__(self) -> Iterator[str]:
        return iter(_CONTRACT_SPECS)

    def __len__(self) -> int:
        return len(_CONTRACT_SPECS)


ACTION_PLAN_CONTRACTS: Mapping[str, PlatformActionPlanContract] = (
    LazyActionPlanContracts()
)


def action_plan_contract_for(platform: str) -> PlatformActionPlanContract:
    key = str(platform or "").strip().casefold()
    if key not in ACTION_PLAN_CONTRACTS:
        return LEGACY_FALLBACK_CONTRACT
    return ACTION_PLAN_CONTRACTS[key]
