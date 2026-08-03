import json
from pathlib import Path

from app.adapter_config import platform_has_real_adapter
from app.platform_modules import (
    PlatformModuleUnavailableError,
    get_platform_module,
    registered_platforms,
)
from app.platform_modules.catalog import PLATFORM_MODULE_SPECS


def _manifest_candidates() -> list[Path]:
    current = Path(__file__).resolve()
    return [
        current.parents[1] / "shared" / "platforms.json",  # Docker: /app/shared
        current.parents[2] / "shared" / "platforms.json",  # local repo root/shared
    ]


def _load_platform_manifest() -> dict:
    for path in _manifest_candidates():
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)["platforms"]
    searched = ", ".join(str(path) for path in _manifest_candidates())
    raise RuntimeError(f"Platform manifest not found; searched: {searched}")


PLATFORMS = _load_platform_manifest()
_PLATFORM_MODULE_KEYS = set(registered_platforms())
if set(PLATFORMS) != _PLATFORM_MODULE_KEYS:
    raise RuntimeError(
        "Platform manifest/module registry mismatch: "
        f"manifest_only={sorted(set(PLATFORMS) - _PLATFORM_MODULE_KEYS)}, "
        f"module_only={sorted(_PLATFORM_MODULE_KEYS - set(PLATFORMS))}"
    )
MANUAL_ONLY_BLOCKERS = {
    platform: spec.real_run_blocker
    for platform, spec in PLATFORM_MODULE_SPECS.items()
    if not spec.real_run_supported and spec.real_run_blocker
}


def get_platform(platform: str) -> dict | None:
    # Preserve the public API's existing canonical lowercase identity rule;
    # registry lookup itself is normalized for internal dispatch helpers.
    if platform not in PLATFORM_MODULE_SPECS:
        return None
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return None
    try:
        platform_module = get_platform_module(platform)
    except PlatformModuleUnavailableError:
        spec = PLATFORM_MODULE_SPECS[platform]
        output = dict(cfg)
        output.update(
            {
                "action_adapter": False,
                "adapter_status": "module_unavailable",
                "execution_mode": "unavailable",
                "real_run_supported": False,
                "real_run_blocker": "platform_module_unavailable",
                "discovery_source_types": sorted(
                    spec.discovery_source_types
                ),
                "action_order": list(spec.action_order),
                "default_execution_path_id": (
                    spec.default_execution_path_id
                ),
                "execution_paths": [],
                "module_available": False,
            }
        )
        return output
    if platform_module is None or platform != platform_module.platform_id:
        return None
    output = dict(cfg)
    # The manifest owns login presentation only. Business capabilities are
    # copied from the immutable platform descriptor so one platform cannot
    # change another platform's discovery or execution policy.
    output.update(
        {
            "action_adapter": any(
                path.real_actions for path in platform_module.execution_paths
            )
            and platform_has_real_adapter(platform_module.platform_id),
            "adapter_status": platform_module.adapter_status,
            "execution_mode": platform_module.execution_mode,
            "real_run_supported": platform_module.real_run_supported,
            "real_run_blocker": platform_module.real_run_blocker,
            "discovery_source_types": sorted(
                platform_module.discovery_source_types
            ),
            "action_order": list(platform_module.action_order),
            "default_execution_path_id": (
                platform_module.default_execution_path_id
            ),
            "execution_paths": [
                {
                    "id": path.path_id,
                    "adapter_kind": path.adapter_kind,
                    "task_modes": sorted(path.task_modes),
                    "real_actions": path.real_actions,
                    "credential_kind": path.credential_kind,
                    "blocker": path.blocker,
                }
                for path in platform_module.execution_paths
            ],
            "module_available": True,
        }
    )
    return output


def get_platforms() -> dict:
    return {platform: get_platform(platform) for platform in registered_platforms()}
