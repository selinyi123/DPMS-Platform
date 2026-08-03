"""Fail when a production container source uses a floating image tag."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMAGE_FILES = (
    ROOT / "core" / "Dockerfile",
    ROOT / "worker" / "Dockerfile",
    ROOT / "docker" / "mysql" / "Dockerfile",
    ROOT / "docker-compose.yml",
)
PIN_RE = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)", re.IGNORECASE)
FROM_RE = re.compile(r"^\s*FROM\s+(?:--[^\s]+\s+)*(\S+)", re.IGNORECASE)
IMAGE_RE = re.compile(r"^\s*image:\s*(\S+)", re.IGNORECASE)


def main() -> int:
    violations: list[str] = []
    for path in IMAGE_FILES:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            match = FROM_RE.match(line) or IMAGE_RE.match(line)
            if not match:
                continue
            image = match.group(1)
            if not PIN_RE.search(image):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}:{image}")
    if violations:
        print("unpinned_container_images", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"container_images_pinned:{len(IMAGE_FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
