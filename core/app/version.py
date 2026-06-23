import os
from pathlib import Path


DEFAULT_PRODUCT_VERSION = "0.3.13"
API_TITLE = "DPMS Runtime Console"


def _version_file_candidates() -> list[Path]:
    current = Path(__file__).resolve()
    return [
        current.parents[1] / "VERSION.md",  # Docker: /app/VERSION.md
        current.parents[2] / "VERSION.md",  # local repo root/VERSION.md
    ]


def _read_version_file() -> str | None:
    for path in _version_file_candidates():
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Product Version:"):
                return line.split(":", 1)[1].strip()
    return None


PRODUCT_VERSION = os.getenv("DPMS_VERSION") or _read_version_file() or DEFAULT_PRODUCT_VERSION
