from typing import Any

from fastapi import APIRouter, Body, HTTPException, status

router = APIRouter()


@router.post("/runs")
async def create_run(_: Any = Body(default=None)) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Run ingestion is not implemented yet.",
    )
