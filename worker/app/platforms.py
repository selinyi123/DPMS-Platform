import json
from pathlib import Path


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
    return dict(cfg) if cfg else None
