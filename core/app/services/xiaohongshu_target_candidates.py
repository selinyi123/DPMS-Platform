"""Authoritative Xiaohongshu target-source and candidate review service."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from app.db import database
from app.repositories import xiaohongshu_target_candidates as repository
from app.services.rule_provenance import ensure_rule_snapshot
from app.utils.canonicalizer import canonicalize_platform_url
from app.utils.lottery_targets import validate_lottery_target


XIAOHONGSHU_TARGET_SOURCE_TYPES = frozenset(
    {"keyword", "author_profile", "offline_search_result"}
)
XIAOHONGSHU_BROWSER_SOURCE_TYPES = frozenset(
    {"keyword", "author_profile"}
)
XIAOHONGSHU_CANDIDATE_DECISIONS = frozenset(
    {"pending", "accepted", "skipped", "needs_review"}
)
MAX_INGEST_CANDIDATES = 1_000
MAX_INGEST_JSON_BYTES = 1_000_000
MAX_CANDIDATE_JSON_FIELD_BYTES = 256 * 1024
SOURCE_SCAN_STALE_AFTER_SECONDS = 180
MAX_KEYWORD_SOURCE_LENGTH = 64
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 100_000
SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_:-]{1,128}$")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
XIAOHONGSHU_NOTE_ID = re.compile(r"^[0-9a-fA-F]{24}$")
XIAOHONGSHU_PROFILE_ID = re.compile(r"^[0-9A-Za-z_-]{5,64}$")
XIAOHONGSHU_HOSTS = frozenset(
    {"xiaohongshu.com", "www.xiaohongshu.com"}
)
XIAOHONGSHU_SHORT_HOSTS = frozenset({"xhslink.com"})
SENSITIVE_JSON_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "csrf",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session",
        "sessionid",
        "token",
        "xsec_token",
    }
)
INLINE_SECRET_VALUE = re.compile(
    r"(?i)\b(?:authorization|cookie|credentials?|password|secret|"
    r"session(?:id)?|token|xsec_token)\s*[:=]\s*[^\s,;]+"
)


class XiaohongshuTargetCandidateError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        current_version: int | None = None,
    ):
        self.code = str(code)
        self.current_version = current_version
        super().__init__(self.code)


def normalize_source_type(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in XIAOHONGSHU_TARGET_SOURCE_TYPES:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_source_type_invalid"
        )
    return normalized


def _safe_https_parts(value: Any):
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not host
    ):
        return None
    return parsed, host


def _canonical_author_profile(value: Any) -> str | None:
    parsed_value = _safe_https_parts(value)
    if parsed_value is None:
        return None
    parsed, host = parsed_value
    parts = [part for part in parsed.path.split("/") if part]
    if (
        host not in XIAOHONGSHU_HOSTS
        or len(parts) != 3
        or parts[:2] != ["user", "profile"]
        or not XIAOHONGSHU_PROFILE_ID.fullmatch(parts[2])
    ):
        return None
    return (
        "https://www.xiaohongshu.com/user/profile/"
        f"{parts[2]}"
    )


def normalize_source_value(value: Any, source_type: str | None = None) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 256
        or CONTROL_CHARACTERS.search(normalized)
    ):
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_source_value_invalid"
        )
    if source_type == "keyword":
        normalized = " ".join(normalized.split())
        if not normalized or len(normalized) > MAX_KEYWORD_SOURCE_LENGTH:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_value_invalid"
            )
    elif source_type == "author_profile":
        canonical = _canonical_author_profile(normalized)
        if canonical is None:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_value_invalid"
            )
        normalized = canonical
    return normalized


def normalize_decision(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in XIAOHONGSHU_CANDIDATE_DECISIONS:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_decision_invalid"
        )
    return normalized


def _json_bytes(value: Any) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_json_invalid"
        ) from exc
    return len(encoded)


def validate_ingest_payload_size(source: Mapping[str, Any], candidates: list) -> None:
    if not 1 <= len(candidates) <= MAX_INGEST_CANDIDATES:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_batch_size_invalid"
        )
    if _json_bytes({"source": dict(source), "candidates": candidates}) > (
        MAX_INGEST_JSON_BYTES
    ):
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_payload_too_large"
        )


def _normalized_sensitive_key(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _sensitive_json_key(value: Any) -> bool:
    normalized = _normalized_sensitive_key(value)
    if normalized in SENSITIVE_JSON_KEY_PARTS:
        return True
    parts = normalized.split("_")
    return any(
        part in SENSITIVE_JSON_KEY_PARTS
        for part in parts
    )


def _sanitize_embedded_url(value: str) -> str:
    parsed_value = _safe_https_parts(value)
    if parsed_value is None:
        return value
    parsed, host = parsed_value
    if host not in XIAOHONGSHU_HOSTS | XIAOHONGSHU_SHORT_HOSTS:
        return value
    return urlunsplit(
        (
            "https",
            "www.xiaohongshu.com"
            if host in XIAOHONGSHU_HOSTS
            else host,
            parsed.path,
            "",
            "",
        )
    )


def _sanitize_json_value(
    value: Any,
    *,
    field: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> Any:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > MAX_JSON_DEPTH or counter[0] > MAX_JSON_NODES:
        raise XiaohongshuTargetCandidateError(
            f"xiaohongshu_target_candidate_{field}_invalid"
        )
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise XiaohongshuTargetCandidateError(
                    f"xiaohongshu_target_candidate_{field}_invalid"
                )
            if _sensitive_json_key(raw_key):
                raise XiaohongshuTargetCandidateError(
                    "xiaohongshu_target_candidate_sensitive_field_forbidden"
                )
            sanitized[raw_key] = _sanitize_json_value(
                raw_value,
                field=field,
                depth=depth + 1,
                counter=counter,
            )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_json_value(
                item,
                field=field,
                depth=depth + 1,
                counter=counter,
            )
            for item in value
        ]
    if isinstance(value, str):
        sanitized_url = _sanitize_embedded_url(value)
        if sanitized_url != value:
            return sanitized_url
        if INLINE_SECRET_VALUE.search(value):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_sensitive_field_forbidden"
            )
        return value
    return value


def _bounded_json_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise XiaohongshuTargetCandidateError(
            f"xiaohongshu_target_candidate_{field}_invalid"
        )
    sanitized = _sanitize_json_value(value, field=field)
    if _json_bytes(sanitized) > MAX_CANDIDATE_JSON_FIELD_BYTES:
        raise XiaohongshuTargetCandidateError(
            f"xiaohongshu_target_candidate_{field}_too_large"
        )
    return dict(sanitized)


def _safe_error_code(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or "").strip().casefold()
    if SAFE_ERROR_CODE.fullmatch(code):
        return code
    return (
        f"xiaohongshu_target_candidate_{type(exc).__name__}"
        .casefold()[:128]
    )


async def _analyze_candidate(
    source_type: str,
    source_value: str,
    evidence: dict[str, Any],
    *,
    observed_at: Any = None,
) -> dict[str, Any]:
    try:
        from app.services.xiaohongshu_target_pursuit import (
            analyze_candidate_evidence,
        )
    except ImportError:
        return {
            "version": 1,
            "platform": "xiaohongshu",
            "initial_decision": "needs_review",
            "confidence": 0,
            "reason_codes": ["candidate_analysis_unavailable"],
            "rule": {},
        }

    result = analyze_candidate_evidence(
        source_type,
        source_value,
        evidence,
        observed_at=observed_at,
    )
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_analysis_invalid"
        )
    return dict(result)


async def create_source(
    source_type: Any,
    source_value: Any,
    *,
    db=database,
) -> tuple[dict[str, Any], bool]:
    normalized_type = normalize_source_type(source_type)
    normalized_value = normalize_source_value(source_value, normalized_type)
    async with db.transaction():
        return await repository.create_or_get_source(
            normalized_type,
            normalized_value,
            db=db,
        )


async def resolve_source(
    source: Mapping[str, Any],
    *,
    create: bool,
    db=database,
) -> tuple[dict[str, Any], bool]:
    source_id = source.get("source_id")
    supplied_type = source.get("source_type")
    supplied_value = source.get("source_value")
    if source_id is not None:
        try:
            source_id = int(source_id)
        except (TypeError, ValueError) as exc:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_id_invalid"
            ) from exc
        if source_id <= 0:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_id_invalid"
            )
        row = await repository.get_source(source_id, db=db)
        if not row:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_not_found"
            )
        resolved = repository.serialize_source(row)
        if supplied_type is not None and (
            normalize_source_type(supplied_type) != resolved["source_type"]
        ):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_binding_mismatch"
            )
        if supplied_value is not None and (
            normalize_source_value(
                supplied_value,
                resolved["source_type"],
            )
            != resolved["source_value"]
        ):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_binding_mismatch"
            )
        return resolved, False

    normalized_type = normalize_source_type(supplied_type)
    normalized_value = normalize_source_value(
        supplied_value,
        normalized_type,
    )
    row = await repository.get_source_by_identity(
        normalized_type,
        normalized_value,
        db=db,
    )
    if row:
        return repository.serialize_source(row), False
    if not create:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_source_not_found"
        )
    return await create_source(
        normalized_type,
        normalized_value,
        db=db,
    )


async def list_sources(
    *,
    source_type: str | None,
    active: bool | None,
    limit: int,
    offset: int,
    db=database,
) -> dict[str, Any]:
    normalized_type = (
        normalize_source_type(source_type)
        if source_type is not None
        else None
    )
    return await repository.list_sources(
        source_type=normalized_type,
        active=active,
        limit=limit,
        offset=offset,
        db=db,
    )


async def set_source_active(
    source_id: int,
    *,
    expected_version: int,
    active: bool,
    db=database,
) -> dict[str, Any]:
    async with db.transaction():
        row = await repository.get_source(source_id, for_update=True, db=db)
        if not row:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_not_found"
            )
        current_version = int(row["version"])
        if current_version != int(expected_version):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_version_conflict",
                current_version=current_version,
            )
        updated = await repository.update_source_active(
            source_id,
            expected_version=current_version,
            active=active,
            db=db,
        )
        if not updated:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_version_conflict",
                current_version=current_version,
            )
        current = await repository.get_source(source_id, db=db)
        return repository.serialize_source(current)


async def begin_source_scan(
    source: Mapping[str, Any],
    *,
    db=database,
) -> dict[str, Any]:
    # Reject an explicitly non-browser source before create=True can persist a
    # new source row for a request that can never be dispatched.
    if source.get("source_id") is None:
        requested_type = normalize_source_type(source.get("source_type"))
        if requested_type not in XIAOHONGSHU_BROWSER_SOURCE_TYPES:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_not_browser_scannable"
            )
    resolved, _created = await resolve_source(source, create=True, db=db)
    if resolved["source_type"] not in XIAOHONGSHU_BROWSER_SOURCE_TYPES:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_source_not_browser_scannable"
        )
    async with db.transaction():
        row = await repository.get_source(
            int(resolved["id"]),
            for_update=True,
            db=db,
        )
        if not row:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_not_found"
            )
        if not bool(row["active"]):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_inactive"
            )
        recovered_stale_scan = False
        if str(row["status"]) == "scanning":
            recovered_stale_scan = await repository.source_scan_is_stale(
                int(row["id"]),
                stale_after_seconds=SOURCE_SCAN_STALE_AFTER_SECONDS,
                db=db,
            )
            if not recovered_stale_scan:
                raise XiaohongshuTargetCandidateError(
                    "xiaohongshu_target_source_scan_in_progress",
                    current_version=int(row["version"]),
                )
        expected_version = int(row["version"])
        updated = await repository.update_source_scan_state(
            int(row["id"]),
            expected_version=expected_version,
            status="scanning",
            error_code=(
                "xiaohongshu_target_stale_scan_recovered"
                if recovered_stale_scan
                else None
            ),
            completed=False,
            db=db,
        )
        if not updated:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_source_version_conflict",
                current_version=expected_version,
            )
        current = await repository.get_source(int(row["id"]), db=db)
        return repository.serialize_source(current)


async def finish_source_scan(
    source_id: int,
    *,
    scan_version: int,
    succeeded: bool,
    error_code: str | None = None,
    db=database,
) -> dict[str, Any] | None:
    normalized_error = None
    if error_code:
        candidate = str(error_code).strip().casefold()
        normalized_error = (
            candidate
            if SAFE_ERROR_CODE.fullmatch(candidate)
            else "xiaohongshu_target_scan_failed"
        )
    async with db.transaction():
        updated = await repository.update_source_scan_state(
            int(source_id),
            expected_version=int(scan_version),
            status="succeeded" if succeeded else "failed",
            error_code=normalized_error,
            completed=True,
            db=db,
        )
        if not updated:
            return None
        current = await repository.get_source(int(source_id), db=db)
        return repository.serialize_source(current)


async def _validate_tracked_source(
    tracked_source_id: Any,
    *,
    db=database,
) -> int | None:
    if tracked_source_id is None:
        return None
    try:
        normalized = int(tracked_source_id)
    except (TypeError, ValueError) as exc:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_tracked_source_id_invalid"
        ) from exc
    if normalized <= 0:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_tracked_source_id_invalid"
        )
    row = await repository.get_tracked_source(normalized, db=db)
    if not row or str(row["platform"]).strip().casefold() != "xiaohongshu":
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_tracked_source_binding_invalid"
        )
    return normalized


def _normalize_candidate_raw_url(value: Any) -> str:
    parsed_value = _safe_https_parts(value)
    if parsed_value is None:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_url_invalid"
        )
    parsed, host = parsed_value
    parts = [part for part in parsed.path.split("/") if part]
    note_id = None
    if host in XIAOHONGSHU_HOSTS:
        if (
            len(parts) == 2
            and parts[0] == "explore"
            and XIAOHONGSHU_NOTE_ID.fullmatch(parts[1])
        ):
            note_id = parts[1]
        elif (
            len(parts) == 3
            and parts[:2] == ["discovery", "item"]
            and XIAOHONGSHU_NOTE_ID.fullmatch(parts[2])
        ):
            note_id = parts[2]
        if note_id is None:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_url_invalid"
            )
        return (
            "https://www.xiaohongshu.com/explore/"
            f"{note_id.casefold()}"
        )
    if host in XIAOHONGSHU_SHORT_HOSTS and parts:
        return urlunsplit(("https", host, parsed.path, "", ""))
    raise XiaohongshuTargetCandidateError(
        "xiaohongshu_target_candidate_url_invalid"
    )


async def _prepare_candidate(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_invalid"
        )
    supplied_raw_url = str(candidate.get("raw_url") or "").strip()
    if not supplied_raw_url or len(supplied_raw_url) > 512:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_url_invalid"
        )
    raw_url = _normalize_candidate_raw_url(supplied_raw_url)
    target = validate_lottery_target("xiaohongshu", raw_url)
    if not target.valid:
        raise XiaohongshuTargetCandidateError(
            target.reason or "xiaohongshu_target_candidate_url_invalid"
        )
    try:
        canonical_url = await canonicalize_platform_url(
            "xiaohongshu",
            raw_url,
        )
    except Exception as exc:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_canonicalization_failed"
        ) from exc
    if not canonical_url or len(canonical_url) > 512:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_canonical_url_invalid"
        )

    title_value = candidate.get("title")
    title = str(title_value).strip() if title_value is not None else None
    if title is not None and len(title) > 256:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_title_invalid"
        )
    if title == "":
        title = None
    evidence = _bounded_json_object(candidate.get("evidence"), "evidence")
    # Bind every analysis to the sanitized top-level target selected by Core.
    # Imported evidence cannot redirect classification to a different note.
    evidence["raw_url"] = raw_url
    if _json_bytes(evidence) > MAX_CANDIDATE_JSON_FIELD_BYTES:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_evidence_too_large"
        )
    supplied_rule = _bounded_json_object(candidate.get("rule"), "rule")
    supplied_classification = _bounded_json_object(
        candidate.get("classification"),
        "classification",
    )
    analysis = await _analyze_candidate(
        str(source["source_type"]),
        str(source["source_value"]),
        evidence,
        observed_at=candidate.get("observed_at")
        or evidence.get("observed_at"),
    )
    analysis_rule = analysis.get("rule")
    rule = (
        dict(analysis_rule)
        if isinstance(analysis_rule, dict)
        else {}
    )
    classification = {
        key: value
        for key, value in analysis.items()
        if key != "rule"
    }
    if supplied_rule or supplied_classification:
        ignored = []
        if supplied_rule:
            ignored.append("rule")
        if supplied_classification:
            ignored.append("classification")
        classification["ignored_untrusted_projection_fields"] = ignored
    rule = _bounded_json_object(rule, "rule")
    classification = _bounded_json_object(
        classification,
        "classification",
    )
    initial_decision = str(
        analysis.get("initial_decision")
        or "needs_review"
    ).strip().casefold()
    if initial_decision not in {"pending", "needs_review"}:
        initial_decision = "needs_review"
        classification["initial_decision"] = initial_decision
        reasons = classification.get("reason_codes")
        reason_codes = list(reasons) if isinstance(reasons, list) else []
        if "candidate_initial_decision_invalid" not in reason_codes:
            reason_codes.append("candidate_initial_decision_invalid")
        classification["reason_codes"] = reason_codes
    try:
        value_score = int(candidate.get("value_score") or 0)
    except (TypeError, ValueError) as exc:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_value_score_invalid"
        ) from exc
    if not 0 <= value_score <= 100:
        raise XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_value_score_invalid"
        )

    return {
        "raw_url": raw_url,
        "canonical_url": canonical_url,
        "title": title,
        "evidence": evidence,
        "rule": rule,
        "classification": classification,
        "initial_decision": initial_decision,
        "published_at": candidate.get("published_at"),
        "value_score": value_score,
        "expires_at": candidate.get("expires_at"),
    }


async def ingest_candidates(
    source_spec: Mapping[str, Any],
    candidates: list[Mapping[str, Any]],
    *,
    db=database,
) -> dict[str, Any]:
    validate_ingest_payload_size(source_spec, list(candidates))
    source, _created_source = await resolve_source(
        source_spec,
        create=True,
        db=db,
    )
    tracked_source_id = await _validate_tracked_source(
        source_spec.get("tracked_source_id"),
        db=db,
    )
    created_count = 0
    updated_count = 0
    items: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        try:
            prepared = await _prepare_candidate(source, candidate)
        except XiaohongshuTargetCandidateError as exc:
            invalid.append({"index": index, "error": exc.code})
            continue
        async with db.transaction():
            snapshot, created = await repository.upsert_candidate_observation(
                source=source,
                tracked_source_id=tracked_source_id,
                db=db,
                **prepared,
            )
        items.append(snapshot)
        if created:
            created_count += 1
        else:
            updated_count += 1
    return {
        "status": "ingested",
        "source": source,
        "received": len(candidates),
        "created_count": created_count,
        "updated_count": updated_count,
        "invalid_count": len(invalid),
        "items": items,
        "invalid": invalid[:100],
    }


async def list_candidates(
    *,
    decision_status: str | None,
    source_type: str | None,
    limit: int,
    offset: int,
    db=database,
) -> dict[str, Any]:
    normalized_decision = (
        normalize_decision(decision_status)
        if decision_status is not None
        else None
    )
    normalized_source = (
        normalize_source_type(source_type)
        if source_type is not None
        else None
    )
    return await repository.list_candidates(
        decision_status=normalized_decision,
        source_type=normalized_source,
        limit=limit,
        offset=offset,
        db=db,
    )


def _rule_text(candidate: Mapping[str, Any]) -> str:
    classification = candidate.get("classification")
    if isinstance(classification, dict):
        snapshots = classification.get("content_snapshots")
        if isinstance(snapshots, dict):
            trusted_texts: list[str] = []
            for name in ("body", "expanded_body", "pinned_comment"):
                snapshot = snapshots.get(name)
                if not isinstance(snapshot, dict) or not snapshot.get("trusted"):
                    continue
                value = snapshot.get("text")
                text = value.strip() if isinstance(value, str) else ""
                if text and text not in trusted_texts:
                    trusted_texts.append(text)
            if trusted_texts:
                return "\n\n".join(trusted_texts)
    rule = candidate.get("rule")
    if isinstance(rule, dict):
        for key in ("rule_text", "text", "raw_text", "summary"):
            value = rule.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if rule:
            return json.dumps(
                rule,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
    evidence = candidate.get("evidence")
    if isinstance(evidence, dict):
        for key in ("expanded_text", "body_text", "card_text"):
            value = evidence.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _draft_action_plan(candidate: Mapping[str, Any]) -> dict[str, Any]:
    rule = candidate.get("rule")
    plan = {}
    if isinstance(rule, dict) and isinstance(rule.get("action_plan"), dict):
        plan = dict(rule["action_plan"])
    classification = candidate.get("classification")
    if isinstance(classification, dict):
        plan["target_pursuit_review_snapshot"] = {
            key: classification[key]
            for key in (
                "version",
                "target",
                "author",
                "content_snapshots",
                "activity_window",
                "prizes",
                "complex_conditions",
                "reason_codes",
                "review_reason_codes",
            )
            if key in classification
        }
    if isinstance(rule, dict):
        plan["target_pursuit_rule_projection"] = dict(rule)
    plan["review_required"] = True
    plan["executable"] = False
    plan["source"] = "xiaohongshu_target_candidate"
    for field in (
        "rule_snapshot_id",
        "rule_hash",
        "plan_hash",
        "reviewed_by",
        "rule_complete_confirmed",
    ):
        plan.pop(field, None)
    return plan


async def decide_candidate(
    candidate_id: int,
    *,
    expected_version: int,
    decision_status: str,
    decision_reason: str | None,
    actor_id: str,
    db=database,
) -> dict[str, Any]:
    target_status = normalize_decision(decision_status)
    normalized_reason = (
        str(decision_reason).strip()[:512]
        if decision_reason is not None
        else None
    )
    async with db.transaction():
        row = await repository.get_candidate(
            candidate_id,
            for_update=True,
            db=db,
        )
        if not row:
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_not_found"
            )
        current_version = int(row["version"])
        if current_version != int(expected_version):
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_version_conflict",
                current_version=current_version,
            )
        current_status = str(row["decision_status"])
        if current_status == "accepted" and target_status != "accepted":
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_acceptance_is_terminal",
                current_version=current_version,
            )
        current_reason = (
            str(row["decision_reason"])
            if row["decision_reason"] is not None
            else None
        )
        if current_status == target_status and current_reason == normalized_reason:
            snapshot = await repository.get_candidate_snapshot(
                candidate_id,
                db=db,
            )
            return snapshot

        accepted_lottery_id = (
            int(row["accepted_lottery_id"])
            if row["accepted_lottery_id"] is not None
            else None
        )
        if target_status == "accepted" and accepted_lottery_id is None:
            candidate = repository.serialize_candidate(row)
            source_hit = await repository.latest_candidate_source_hit(
                candidate_id,
                db=db,
            )
            if not source_hit:
                raise XiaohongshuTargetCandidateError(
                    "xiaohongshu_target_candidate_source_missing"
                )
            rule_text = _rule_text(candidate)
            lottery = await repository.create_or_get_lottery_for_candidate(
                row,
                source_hit=source_hit,
                rule_text=rule_text,
                action_plan=_draft_action_plan(candidate),
                db=db,
            )
            accepted_lottery_id = int(lottery["id"])
            await ensure_rule_snapshot(
                lottery,
                rule_text,
                complete=False,
                source_kind=str(source_hit["source_type"]),
                source_locator=str(source_hit["source_value"]),
                fetch_method="xiaohongshu_target_candidate_accept",
                allow_existing_complete=False,
                db=db,
            )
        elif target_status != "accepted":
            accepted_lottery_id = None

        updated = await repository.persist_candidate_decision(
            candidate_id,
            expected_version=current_version,
            decision_status=target_status,
            decision_reason=normalized_reason,
            accepted_lottery_id=accepted_lottery_id,
            actor_id=str(actor_id or "")[:128] or "unknown-operator",
            db=db,
        )
        if not updated:
            latest = await repository.get_candidate(candidate_id, db=db)
            raise XiaohongshuTargetCandidateError(
                "xiaohongshu_target_candidate_version_conflict",
                current_version=(
                    int(latest["version"]) if latest else current_version
                ),
            )
        snapshot = await repository.get_candidate_snapshot(
            candidate_id,
            db=db,
        )
        if snapshot is None:
            raise RuntimeError(
                "xiaohongshu_target_candidate_decision_not_visible"
            )
        return snapshot


__all__ = (
    "MAX_CANDIDATE_JSON_FIELD_BYTES",
    "MAX_INGEST_CANDIDATES",
    "MAX_INGEST_JSON_BYTES",
    "SOURCE_SCAN_STALE_AFTER_SECONDS",
    "XIAOHONGSHU_BROWSER_SOURCE_TYPES",
    "XIAOHONGSHU_CANDIDATE_DECISIONS",
    "XIAOHONGSHU_TARGET_SOURCE_TYPES",
    "XiaohongshuTargetCandidateError",
    "begin_source_scan",
    "create_source",
    "decide_candidate",
    "finish_source_scan",
    "ingest_candidates",
    "list_candidates",
    "list_sources",
    "normalize_decision",
    "normalize_source_type",
    "resolve_source",
    "set_source_active",
    "validate_ingest_payload_size",
)
