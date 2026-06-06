from fastapi import APIRouter, Query

from app.knowledge.service import build_knowledge_summary


router = APIRouter()


@router.get("/summary")
async def knowledge_summary(window_days: int = Query(30, ge=1, le=365)):
    return await build_knowledge_summary(window_days=window_days)
