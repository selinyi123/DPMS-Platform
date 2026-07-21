import json
from pathlib import Path

from app.adapter_config import platform_has_real_adapter


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


def get_platform(platform: str) -> dict | None:
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return None
    output = dict(cfg)
    output["action_adapter"] = False
    output["adapter_status"] = "calibration_required"
    if platform == "xiaohongshu":
        output.update(
            {
                "adapter_status": "manual_assisted_only",
                "execution_mode": "manual_assisted",
                "real_run_supported": False,
                "real_run_blocker": "xiaohongshu_no_official_interaction_api",
            }
        )
        return output
    # Native API engines and complete selector configs both count as real-action
    # adapters; Bilibili currently uses the API path, other platforms use
    # selector calibration.
    if platform_has_real_adapter(platform):
        output["action_adapter"] = True
        output["adapter_status"] = "configured"
    return output


def get_platforms() -> dict:
    return {platform: get_platform(platform) for platform in PLATFORMS}
