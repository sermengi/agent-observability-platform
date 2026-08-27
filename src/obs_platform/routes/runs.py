from collections.abc import AsyncIterator
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.telemetry.v1.models import ExtendedRunEvent

router = APIRouter()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    engine = cast(AsyncEngine, request.app.state.db_engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


class IngestRunResponse(BaseModel):
    run_id: str
    event_type: str
    status: str


@router.post("/runs")
async def create_run(
    event: ExtendedRunEvent,
    session: AsyncSession = Depends(get_session),
) -> IngestRunResponse:
    result = await ingest_run_event(session, event)
    return IngestRunResponse(
        run_id=result.run_id,
        event_type=result.event_type,
        status=result.status,
    )
