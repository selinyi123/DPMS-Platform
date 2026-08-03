#!/usr/bin/env python3
"""Compare platform contracts exposed by Core, Worker, and Frontend.

This is a verifier, not an authoritative business manifest.  Every value is
read from the platform-owned runtime descriptors. Core and Worker target-kind
probes remain behavioral until both runtimes expose equivalent descriptor data.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("bilibili", "weibo", "xiaohongshu", "douyin")
MODES = ("dry_run", "shadow_run", "real_run")
MENTION_IDENTITY_PROBES = (
    "@Alice",
    "@ＡＬＩＣＥ",
    "@ß",
    "@SS",
    "@ẞ",
    "@Σ",
    "@ς",
    "@ı",
    "@i",
    "@İ",
)

# Behavioral probes, not a central capability registry. Bilibili article is
# included to preserve the explicitly supported compatibility contract.
TARGET_KIND_PROBES = {
    "bilibili": (
        {
            "kind": "dynamic",
            "url": "https://t.bilibili.com/1234567890",
            "canonical_uri": "canonical://bilibili/dynamic/1234567890",
        },
        {
            "kind": "video",
            "url": "https://www.bilibili.com/video/BV1ABC123",
            "canonical_uri": "canonical://bilibili/video/BV1ABC123",
        },
        {
            "kind": "article",
            "url": "https://www.bilibili.com/read/cv123456",
            "canonical_uri": "canonical://bilibili/article/cv123456",
        },
    ),
    "weibo": (
        {
            "kind": "status",
            "url": "https://weibo.com/detail/PCAGRFqKj",
            "canonical_uri": "canonical://weibo/status/PCAGRFqKj",
        },
    ),
    "xiaohongshu": (
        {
            "kind": "note",
            "url": "https://www.xiaohongshu.com/explore/64f0123456789abcdef01234",
            "canonical_uri": "canonical://xiaohongshu/note/64f0123456789abcdef01234",
        },
    ),
    "douyin": (
        {
            "kind": "video",
            "url": "https://www.douyin.com/video/7300000000000000000",
            "canonical_uri": "canonical://douyin/video/7300000000000000000",
        },
        {
            "kind": "note",
            "url": "https://www.douyin.com/note/7300000000000000001",
            "canonical_uri": "canonical://douyin/note/7300000000000000001",
        },
    ),
}


CORE_EXPORTER = r"""
import json
import os
from urllib.parse import urlparse

from app.action_plan import _mention_identity_key
from app.platform_modules import get_platform_modules

probes = json.loads(os.environ["DPMS_PLATFORM_TARGET_PROBES"])
mention_probes = json.loads(os.environ["DPMS_MENTION_IDENTITY_PROBES"])
result = {}
for platform, module in get_platform_modules().items():
    path_map = module.execution_path_map
    effective_credentials = {}
    for stored_path in path_map:
        for mode in ("dry_run", "shadow_run", "real_run"):
            account_path = module.account_execution_path_for_dispatch(
                task_mode=mode,
                stored_execution_path=stored_path,
            ) or stored_path
            runtime_path = path_map.get(account_path)
            key = f"{stored_path}:{mode}"
            effective_credentials[key] = (
                [runtime_path.credential_kind]
                if runtime_path is not None
                and mode in runtime_path.task_modes
                and runtime_path.credential_kind
                else []
            )
    target_kinds = []
    for probe in probes[platform]:
        parsed = urlparse(probe["url"])
        validation = module.validate_parsed_target(
            parsed,
            (parsed.hostname or "").rstrip(".").lower(),
        )
        if validation.valid and validation.kind not in target_kinds:
            target_kinds.append(validation.kind)
    result[platform] = {
        "actions": list(module.action_order),
        "source_types": sorted(module.discovery_source_types),
        "paths": sorted(
            [
                {
                    "id": path.path_id,
                    "credential_kind": path.credential_kind,
                    "modes": sorted(path.task_modes),
                }
                for path in module.execution_paths
            ],
            key=lambda item: item["id"],
        ),
        "default_path": module.default_execution_path_id,
        "effective_credentials": effective_credentials,
        "target_kinds": sorted(target_kinds),
        "real_target_kinds": sorted(
            (module.strategy_real_target_kinds or frozenset(target_kinds))
            if module.real_run_supported
            else ()
        ),
        "short_link_limit": module.target_import_short_link_limit,
        "short_link_error": module.target_import_short_link_error,
        "short_link_hosts": sorted(module.target_import_short_link_hosts),
    }
result["weibo"]["mention_identity_keys"] = [
    _mention_identity_key(value) for value in mention_probes
]
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""


WORKER_EXPORTER = r"""
import json
import os
import sys
import types

# Platform descriptors only need the Page type at import time.  This import-only
# shim keeps the parity verifier independent of Playwright and performs no I/O.
if "playwright.async_api" not in sys.modules:
    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.Page = object
    playwright.async_api = async_api
    sys.modules["playwright"] = playwright
    sys.modules["playwright.async_api"] = async_api

if "httpx" not in sys.modules:
    httpx = types.ModuleType("httpx")
    httpx.AsyncBaseTransport = object
    httpx.AsyncClient = object
    httpx.Response = object
    httpx.TransportError = type("TransportError", (Exception,), {})
    sys.modules["httpx"] = httpx

from app.platform_modules.base import PlatformRoutingError
from app.platform_modules.registry import PLATFORM_MODULES
from app.action_plan import _mention_identity_key
from app.utils.navigation_safety import validated_platform_canonical_uri

probes = json.loads(os.environ["DPMS_PLATFORM_TARGET_PROBES"])
mention_probes = json.loads(os.environ["DPMS_MENTION_IDENTITY_PROBES"])
result = {}
for platform, module in PLATFORM_MODULES.items():
    effective_credentials = {}
    for stored_path in module.execution_paths:
        for mode in ("dry_run", "shadow_run", "real_run"):
            key = f"{stored_path.path_id}:{mode}"
            try:
                runtime_path, _executor = module.route(
                    mode,
                    {"execution_path_id": stored_path.path_id},
                )
            except PlatformRoutingError:
                effective_credentials[key] = []
            else:
                effective_credentials[key] = [runtime_path.credential_kind]
    target_kinds = []
    for probe in probes[platform]:
        try:
            validated_platform_canonical_uri(platform, probe["canonical_uri"])
        except ValueError:
            continue
        if probe["kind"] not in target_kinds:
            target_kinds.append(probe["kind"])
    result[platform] = {
        "actions": list(module.action_order),
        "paths": sorted(
            [
                {
                    "id": path.path_id,
                    "credential_kind": path.credential_kind,
                    "modes": sorted(path.supported_modes),
                }
                for path in module.execution_paths
            ],
            key=lambda item: item["id"],
        ),
        "default_path": module.default_execution_path,
        "effective_credentials": effective_credentials,
        "target_kinds": sorted(target_kinds),
        "real_target_kinds": sorted(module.real_target_kinds),
    }
result["weibo"]["mention_identity_keys"] = [
    _mention_identity_key(value) for value in mention_probes
]
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""


def frontend_exporter(
    module_url: str,
    compatibility_url: str,
    import_policies_url: str,
) -> str:
    return f"""
import * as registry from {json.dumps(module_url)};
import {{ mentionIdentityKey }} from {json.dumps(compatibility_url)};
import {{ PLATFORM_IMPORT_POLICIES }} from {json.dumps(import_policies_url)};
const modes = {json.dumps(MODES)};
const mentionProbes = {json.dumps(MENTION_IDENTITY_PROBES, ensure_ascii=False)};
const result = {{}};
const loadResult = await registry.loadRegisteredPlatformModules();
if (Object.keys(loadResult.failures).length) {{
  throw new Error(
    `frontend platform modules unavailable: ${{
      Object.keys(loadResult.failures).sort().join(",")
    }}`,
  );
}}
for (const module of registry.registeredPlatformModules()) {{
  const effectiveCredentials = {{}};
  for (const path of module.executionPaths) {{
    for (const mode of modes) {{
      const key = `${{path}}:${{mode}}`;
      effectiveCredentials[key] = registry.platformAccountCredentialKinds(
        module.id,
        mode,
        path,
      );
    }}
  }}
  result[module.id] = {{
    actions: [...module.actions],
    source_types: [...module.discoverySourceTypes].sort(),
    path_ids: [...module.executionPaths].sort(),
    default_path: module.defaultExecutionPathId,
    effective_credentials: effectiveCredentials,
    target_kinds: Array.isArray(module.targetKinds) ? [...module.targetKinds].sort() : null,
    real_target_kinds: Array.isArray(module.realTargetKinds) ? [...module.realTargetKinds].sort() : null,
    short_link_limit: PLATFORM_IMPORT_POLICIES[module.id].shortLinkLimit,
    short_link_error: PLATFORM_IMPORT_POLICIES[module.id].shortLinkErrorCode,
    short_link_hosts: [...PLATFORM_IMPORT_POLICIES[module.id].shortLinkHosts].sort(),
  }};
}}
result.weibo.mention_identity_keys = mentionProbes.map(mentionIdentityKey);
console.log(JSON.stringify(result));
"""


def run_json(command: list[str], *, env: dict[str, str] | None = None) -> dict:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"contract exporter failed ({completed.returncode}): "
            f"{' '.join(command[:3])}: {detail}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"contract exporter returned no JSON: {command[:3]}")
    return json.loads(lines[-1])


def python_environment(runtime_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(runtime_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["DPMS_PLATFORM_TARGET_PROBES"] = json.dumps(TARGET_KIND_PROBES)
    env["DPMS_MENTION_IDENTITY_PROBES"] = json.dumps(MENTION_IDENTITY_PROBES)
    return env


def resolve_node(explicit: str | None) -> str:
    requested = explicit or os.environ.get("NODE_BINARY")
    candidate = (shutil.which(requested) if requested else None) or requested
    candidate = candidate or shutil.which("node")
    if not candidate:
        raise RuntimeError(
            "Node.js was not found; pass --node or set NODE_BINARY to run "
            "Frontend parity checks"
        )
    resolved = Path(candidate).resolve()
    if not resolved.is_file():
        raise RuntimeError(f"Node.js executable does not exist: {resolved}")
    return str(resolved)


def compare_contracts(
    core: dict[str, Any],
    worker: dict[str, Any],
    frontend: dict[str, Any],
    *,
    strict_target_kinds: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    runtime_platforms = {
        "core": sorted(core),
        "worker": sorted(worker),
        "frontend": sorted(frontend),
    }
    if len({tuple(value) for value in runtime_platforms.values()}) != 1:
        errors.append({"platform": "*", "field": "platforms", **runtime_platforms})

    for platform in PLATFORMS:
        if any(platform not in runtime for runtime in (core, worker, frontend)):
            continue
        triplets = {
            "actions": {
                "core": core[platform]["actions"],
                "worker": worker[platform]["actions"],
                "frontend": frontend[platform]["actions"],
            },
            "default_path": {
                "core": core[platform]["default_path"],
                "worker": worker[platform]["default_path"],
                "frontend": frontend[platform]["default_path"],
            },
            "path_ids": {
                "core": sorted(path["id"] for path in core[platform]["paths"]),
                "worker": sorted(path["id"] for path in worker[platform]["paths"]),
                "frontend": frontend[platform]["path_ids"],
            },
            "effective_credentials": {
                "core": core[platform]["effective_credentials"],
                "worker": worker[platform]["effective_credentials"],
                "frontend": frontend[platform]["effective_credentials"],
            },
        }
        if platform == "weibo":
            triplets["mention_identity_keys"] = {
                "core": core[platform]["mention_identity_keys"],
                "worker": worker[platform]["mention_identity_keys"],
                "frontend": frontend[platform]["mention_identity_keys"],
            }
        for field, values in triplets.items():
            if len({json.dumps(value, sort_keys=True) for value in values.values()}) != 1:
                errors.append({"platform": platform, "field": field, **values})

        if core[platform]["paths"] != worker[platform]["paths"]:
            errors.append(
                {
                    "platform": platform,
                    "field": "path_modes_and_credential_kinds",
                    "core": core[platform]["paths"],
                    "worker": worker[platform]["paths"],
                }
            )
        if core[platform]["source_types"] != frontend[platform]["source_types"]:
            errors.append(
                {
                    "platform": platform,
                    "field": "source_types",
                    "core": core[platform]["source_types"],
                    "frontend": frontend[platform]["source_types"],
                }
            )

        for field in (
            "short_link_limit",
            "short_link_error",
            "short_link_hosts",
        ):
            if core[platform][field] != frontend[platform][field]:
                errors.append(
                    {
                        "platform": platform,
                        "field": field,
                        "core": core[platform][field],
                        "frontend": frontend[platform][field],
                    }
                )

        target_values = {
            "core": core[platform]["target_kinds"],
            "worker": worker[platform]["target_kinds"],
            "frontend": frontend[platform]["target_kinds"],
        }
        target_issue = {
            "platform": platform,
            "field": "target_kinds",
            **target_values,
        }
        if len(
            {json.dumps(value, sort_keys=True) for value in target_values.values()}
        ) != 1:
            (errors if strict_target_kinds else warnings).append(target_issue)

        real_target_values = {
            "core": core[platform]["real_target_kinds"],
            "worker": worker[platform]["real_target_kinds"],
            "frontend": frontend[platform]["real_target_kinds"],
        }
        if len(
            {
                json.dumps(value, sort_keys=True)
                for value in real_target_values.values()
            }
        ) != 1:
            errors.append(
                {
                    "platform": platform,
                    "field": "real_target_kinds",
                    **real_target_values,
                }
            )

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Path to the Node.js executable")
    parser.add_argument(
        "--strict-target-kinds",
        action="store_true",
        help="Treat implicit/mismatched target-kind contracts as errors",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    args = parser.parse_args()

    try:
        node = resolve_node(args.node)
        core = run_json(
            [sys.executable, "-c", CORE_EXPORTER],
            env=python_environment(ROOT / "core"),
        )
        worker = run_json(
            [sys.executable, "-c", WORKER_EXPORTER],
            env=python_environment(ROOT / "worker"),
        )
        frontend = run_json(
            [
                node,
                "--input-type=module",
                "--eval",
                frontend_exporter(
                    (ROOT / "frontend" / "src" / "platforms" / "index.js").as_uri(),
                    (ROOT / "frontend" / "src" / "lotteryCompatibility.js").as_uri(),
                    (
                        ROOT
                        / "frontend"
                        / "src"
                        / "platforms"
                        / "importPolicies.js"
                    ).as_uri(),
                ),
            ]
        )
        errors, warnings = compare_contracts(
            core,
            worker,
            frontend,
            strict_target_kinds=args.strict_target_kinds,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"platform contract parity check unavailable: {exc}", file=sys.stderr)
        return 2

    report = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_platforms": list(PLATFORMS),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for issue in errors:
            print(f"ERROR {issue['platform']} {issue['field']}: {json.dumps(issue, ensure_ascii=False, sort_keys=True)}")
        for issue in warnings:
            print(f"WARN  {issue['platform']} {issue['field']}: {json.dumps(issue, ensure_ascii=False, sort_keys=True)}")
        print(
            f"platform contract parity: errors={len(errors)} "
            f"warnings={len(warnings)} platforms={len(PLATFORMS)}"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
