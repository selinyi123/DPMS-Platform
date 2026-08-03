"""Operator-reviewed, read-only Xiaohongshu target pursuit APIs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.db import database
from app.event_store.service import record_event
from app.security import audit_event, require_min_role
from app.services import xiaohongshu_target_candidates as candidate_service


router = APIRouter()


SourceType = Literal[
    "keyword",
    "author_profile",
    "offline_search_result",
]
DecisionStatus = Literal[
    "pending",
    "accepted",
    "skipped",
    "needs_review",
]


class XiaohongshuTargetSourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: int | None = Field(default=None, gt=0)
    source_type: SourceType | None = None
    source_value: str | None = Field(default=None, min_length=1, max_length=256)
    tracked_source_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_source_binding(self):
        if self.source_id is None and (
            self.source_type is None or self.source_value is None
        ):
            raise ValueError(
                "source_id or source_type/source_value is required"
            )
        return self


class XiaohongshuTargetSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_value: str = Field(min_length=1, max_length=256)


class XiaohongshuTargetSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    active: bool


class XiaohongshuTargetCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_url: str = Field(min_length=8, max_length=512)
    title: str | None = Field(default=None, max_length=256)
    evidence: dict[str, Any] = Field(default_factory=dict)
    rule: dict[str, Any] = Field(default_factory=dict)
    classification: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None
    published_at: datetime | None = None
    value_score: int = Field(default=0, ge=0, le=100)
    expires_at: datetime | None = None


class XiaohongshuTargetCandidateIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: XiaohongshuTargetSourceReference
    candidates: list[XiaohongshuTargetCandidateInput] = Field(
        min_length=1,
        max_length=candidate_service.MAX_INGEST_CANDIDATES,
    )


class XiaohongshuTargetScanRequest(XiaohongshuTargetSourceReference):
    max_candidates: int = Field(default=20, ge=1, le=50)


class XiaohongshuTargetDecisionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    decision_status: DecisionStatus = Field(
        validation_alias=AliasChoices("decision_status", "status")
    )
    decision_reason: str | None = Field(
        default=None,
        max_length=512,
        validation_alias=AliasChoices("decision_reason", "reason"),
    )


def _source_dict(value: XiaohongshuTargetSourceReference) -> dict[str, Any]:
    return value.model_dump(exclude_none=True)


def _candidate_dict(value: XiaohongshuTargetCandidateInput) -> dict[str, Any]:
    return value.model_dump(exclude_none=True)


def _raise_candidate_error(exc: candidate_service.XiaohongshuTargetCandidateError):
    code = exc.code
    if code.endswith("_not_found"):
        status_code = 404
    elif (
        code.endswith("_version_conflict")
        or code.endswith("_scan_in_progress")
        or code.endswith("_acceptance_is_terminal")
    ):
        status_code = 409
    elif (
        code.endswith("_payload_too_large")
        or code.endswith("_batch_size_invalid")
    ):
        status_code = 413
    else:
        status_code = 400
    detail: dict[str, Any] = {"code": code}
    if exc.current_version is not None:
        detail["current_version"] = int(exc.current_version)
    raise HTTPException(status_code=status_code, detail=detail) from exc


def _raise_scan_error(exc: BaseException):
    code = str(getattr(exc, "code", "") or "").strip()
    if not code:
        code = "xiaohongshu_target_pursuit_scan_failed"
    if (
        code.endswith("_source_unsupported")
        or code.endswith("_source_value_invalid")
        or code.endswith("_limit_invalid")
    ):
        status_code = 400
    elif code.endswith("_result_timeout"):
        status_code = 504
    elif code.endswith("_ready_account_required"):
        status_code = 409
    else:
        status_code = 502
    raise HTTPException(
        status_code=status_code,
        detail={"code": code},
    ) from exc


def _scan_candidate_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise candidate_service.XiaohongshuTargetCandidateError(
            "xiaohongshu_target_candidate_evidence_invalid"
        )
    raw_url = value.get("raw_url")
    title = value.get("title")
    observed_at = value.get("observed_at")
    return {
        "raw_url": raw_url,
        "title": title,
        # Persist the complete bounded Worker observation as source evidence.
        # The analyzer derives authoritative rule/classification projections.
        "evidence": dict(value),
        "rule": {},
        "classification": {},
        "observed_at": observed_at,
    }


@router.get("/sources")
async def list_target_sources(
    request: Request,
    source_type: SourceType | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
):
    require_min_role(request, "viewer")
    try:
        return await candidate_service.list_sources(
            source_type=source_type,
            active=active,
            limit=limit,
            offset=offset,
        )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)


@router.post("/sources")
async def create_target_source(
    data: XiaohongshuTargetSourceCreate,
    request: Request,
):
    actor = require_min_role(request, "operator")
    try:
        async with database.transaction():
            source, created = await candidate_service.create_source(
                data.source_type,
                data.source_value,
            )
            await audit_event(
                request,
                action="xiaohongshu_target_source.create",
                resource_type="xiaohongshu_target_source",
                resource_id=source["id"],
                result="created" if created else "exists",
                risk_level="low",
                detail={"source_type": source["source_type"]},
            )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)
    await record_event(
        aggregate="xiaohongshu_target_source",
        aggregate_id=source["id"],
        event_type=(
            "XiaohongshuTargetSourceCreated"
            if created
            else "XiaohongshuTargetSourceObserved"
        ),
        payload={"source_type": source["source_type"]},
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {
        "status": "created" if created else "exists",
        "source": source,
    }


@router.put("/sources/{source_id}")
async def update_target_source(
    source_id: int,
    data: XiaohongshuTargetSourceUpdate,
    request: Request,
):
    actor = require_min_role(request, "operator")
    try:
        async with database.transaction():
            source = await candidate_service.set_source_active(
                source_id,
                expected_version=data.expected_version,
                active=data.active,
            )
            await audit_event(
                request,
                action="xiaohongshu_target_source.update",
                resource_type="xiaohongshu_target_source",
                resource_id=source_id,
                result="updated",
                risk_level="medium",
                detail={"active": bool(data.active)},
            )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)
    await record_event(
        aggregate="xiaohongshu_target_source",
        aggregate_id=source_id,
        event_type="XiaohongshuTargetSourceUpdated",
        payload={
            "active": bool(data.active),
            "version": source["version"],
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return {"status": "updated", "source": source}


@router.get("/candidates")
async def list_target_candidates(
    request: Request,
    decision_status: DecisionStatus | None = None,
    source_type: SourceType | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
):
    require_min_role(request, "viewer")
    try:
        return await candidate_service.list_candidates(
            decision_status=decision_status,
            source_type=source_type,
            limit=limit,
            offset=offset,
        )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/ingest")
async def ingest_target_candidates(
    data: XiaohongshuTargetCandidateIngest,
    request: Request,
):
    actor = require_min_role(request, "operator")
    source_spec = _source_dict(data.source)
    candidates = [_candidate_dict(candidate) for candidate in data.candidates]
    try:
        result = await candidate_service.ingest_candidates(
            source_spec,
            candidates,
        )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)
    await audit_event(
        request,
        action="xiaohongshu_target_candidate.ingest",
        resource_type="xiaohongshu_target_source",
        resource_id=result["source"]["id"],
        result="ingested",
        risk_level="low",
        detail={
            "received": result["received"],
            "created_count": result["created_count"],
            "updated_count": result["updated_count"],
            "invalid_count": result["invalid_count"],
        },
    )
    await record_event(
        aggregate="xiaohongshu_target_source",
        aggregate_id=result["source"]["id"],
        event_type="XiaohongshuTargetCandidatesIngested",
        payload={
            "received": result["received"],
            "created_count": result["created_count"],
            "updated_count": result["updated_count"],
            "invalid_count": result["invalid_count"],
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.post("/scan")
async def scan_target_source(
    data: XiaohongshuTargetScanRequest,
    request: Request,
):
    actor = require_min_role(request, "operator")
    source_spec = _source_dict(data)
    source_spec.pop("max_candidates", None)
    try:
        source = await candidate_service.begin_source_scan(source_spec)
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)

    scan_version = int(source["version"])
    try:
        from app.services.xiaohongshu_target_pursuit_requests import (
            dispatch_xiaohongshu_target_pursuit_scan,
        )

        scan_result = await dispatch_xiaohongshu_target_pursuit_scan(
            source["source_type"],
            source["source_value"],
            max_candidates=data.max_candidates,
        )
        raw_candidates = scan_result.get("candidates")
        if not isinstance(raw_candidates, list):
            raise candidate_service.XiaohongshuTargetCandidateError(
                "xiaohongshu_target_pursuit_result_invalid"
            )
        if raw_candidates:
            ingest_result = await candidate_service.ingest_candidates(
                {
                    "source_id": source["id"],
                    "tracked_source_id": data.tracked_source_id,
                },
                [_scan_candidate_payload(item) for item in raw_candidates],
            )
        else:
            ingest_result = {
                "status": "ingested",
                "source": source,
                "received": 0,
                "created_count": 0,
                "updated_count": 0,
                "invalid_count": 0,
                "items": [],
                "invalid": [],
            }
    except asyncio.CancelledError:
        await asyncio.shield(
            candidate_service.finish_source_scan(
                int(source["id"]),
                scan_version=scan_version,
                succeeded=False,
                error_code="xiaohongshu_target_pursuit_scan_cancelled",
            )
        )
        raise
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        await candidate_service.finish_source_scan(
            int(source["id"]),
            scan_version=scan_version,
            succeeded=False,
            error_code=exc.code,
        )
        if exc.code in {
            "xiaohongshu_target_pursuit_result_invalid",
            "xiaohongshu_target_candidate_evidence_invalid",
        }:
            _raise_scan_error(exc)
        _raise_candidate_error(exc)
    except Exception as exc:
        error_code = str(getattr(exc, "code", "") or "").strip() or (
            "xiaohongshu_target_pursuit_scan_failed"
        )
        await candidate_service.finish_source_scan(
            int(source["id"]),
            scan_version=scan_version,
            succeeded=False,
            error_code=error_code,
        )
        _raise_scan_error(exc)

    finished_source = await candidate_service.finish_source_scan(
        int(source["id"]),
        scan_version=scan_version,
        succeeded=True,
    )
    if finished_source is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "xiaohongshu_target_source_scan_completion_conflict"
                )
            },
        )
    scan_metadata = {
        key: value
        for key, value in scan_result.items()
        if key != "candidates"
    }
    result = {
        **ingest_result,
        "status": "scanned",
        "source": finished_source,
        "scan": scan_metadata,
    }
    await audit_event(
        request,
        action="xiaohongshu_target_source.scan",
        resource_type="xiaohongshu_target_source",
        resource_id=source["id"],
        result="scanned",
        risk_level="medium",
        detail={
            "source_type": source["source_type"],
            "received": result["received"],
            "created_count": result["created_count"],
            "updated_count": result["updated_count"],
            "invalid_count": result["invalid_count"],
        },
    )
    await record_event(
        aggregate="xiaohongshu_target_source",
        aggregate_id=source["id"],
        event_type="XiaohongshuTargetSourceScanned",
        payload={
            "source_type": source["source_type"],
            "received": result["received"],
            "created_count": result["created_count"],
            "updated_count": result["updated_count"],
            "invalid_count": result["invalid_count"],
        },
        correlation_id=scan_metadata.get("request_id"),
        actor_type="operator",
        actor_id=actor["actor_id"],
    )
    return result


@router.put("/candidates/{candidate_id}/decision")
async def update_candidate_decision(
    candidate_id: int,
    data: XiaohongshuTargetDecisionUpdate,
    request: Request,
):
    actor = require_min_role(request, "operator")
    try:
        async with database.transaction():
            candidate = await candidate_service.decide_candidate(
                candidate_id,
                expected_version=data.expected_version,
                decision_status=data.decision_status,
                decision_reason=data.decision_reason,
                actor_id=actor["actor_id"],
            )
            await audit_event(
                request,
                action="xiaohongshu_target_candidate.decision",
                resource_type="xiaohongshu_target_candidate",
                resource_id=candidate_id,
                result=candidate["decision_status"],
                risk_level=(
                    "high"
                    if candidate["decision_status"] == "accepted"
                    else "medium"
                ),
                detail={
                    "decision_status": candidate["decision_status"],
                    "accepted_lottery_id": candidate[
                        "accepted_lottery_id"
                    ],
                    "version": candidate["version"],
                },
            )
    except candidate_service.XiaohongshuTargetCandidateError as exc:
        _raise_candidate_error(exc)
    await record_event(
        aggregate="xiaohongshu_target_candidate",
        aggregate_id=candidate_id,
        event_type="XiaohongshuTargetCandidateDecisionUpdated",
        payload={
            "decision_status": candidate["decision_status"],
            "accepted_lottery_id": candidate["accepted_lottery_id"],
            "version": candidate["version"],
        },
        actor_type="operator",
        actor_id=actor["actor_id"],
        critical=candidate["decision_status"] == "accepted",
    )
    return {"status": "updated", "candidate": candidate}
