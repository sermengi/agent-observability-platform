from typing import Any

from fastapi import APIRouter, HTTPException, status

router = APIRouter()


@router.post("/runs")
async def create_run(_: dict[str, Any]) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Run ingestion is not implemented yet.",
    )
