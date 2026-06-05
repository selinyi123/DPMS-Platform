from fastapi import APIRouter, HTTPException

from app.db import database
from app.event_store.service import normalize_event_row


router = APIRouter()


@router.get("/")
async def list_events(
    aggregate: str | None = None,
    aggregate_id: str | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    limit: int = 100,
):
    filters = []
    values = {"limit": max(1, min(limit, 500))}
    if aggregate:
        filters.append("aggregate = :aggregate")
        values["aggregate"] = aggregate
    if aggregate_id:
        filters.append("aggregate_id = :aggregate_id")
        values["aggregate_id"] = str(aggregate_id)
    if event_type:
        filters.append("event_type = :event_type")
        values["event_type"] = event_type
    if correlation_id:
        filters.append("correlation_id = :correlation_id")
        values["correlation_id"] = correlation_id

    query = "SELECT * FROM events"
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY occurred_at DESC, id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values)
    return [normalize_event_row(row) for row in rows]


@router.get("/accounts/{account_id}")
async def list_account_events(account_id: int, limit: int = 100):
    return await list_events(aggregate="account", aggregate_id=str(account_id), limit=limit)


@router.get("/lotteries/{lottery_id}")
async def list_lottery_events(lottery_id: int, limit: int = 100):
    return await list_events(aggregate="lottery", aggregate_id=str(lottery_id), limit=limit)


@router.get("/tasks/{task_id}")
async def list_task_events(task_id: str, limit: int = 100):
    return await list_events(aggregate="task", aggregate_id=task_id, limit=limit)


@router.get("/{event_id}")
async def get_event(event_id: str):
    row = await database.fetch_one("SELECT * FROM events WHERE id = :id", {"id": event_id})
    if not row:
        raise HTTPException(404, detail="Event not found")
    return normalize_event_row(row)
