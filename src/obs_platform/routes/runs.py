from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import ColumnElement

from obs_platform.api.v1.schemas import RunListResponse
from obs_platform.db.models import AgentRun, LLMCall
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.telemetry.v1.enums import RunStatus
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


class RunListParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    status: RunStatus | None = None
    scenario_id: str | None = None
    agent_version: str | None = None
    model: str | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None


@router.get(
    "/runs",
    response_model=RunListResponse,
    summary="List runs",
)
async def list_runs(
    params: Annotated[RunListParams, Query()],
    session: AsyncSession = Depends(get_session),
) -> RunListResponse:
    filters = _run_list_filters(params)
    total = await session.scalar(
        select(func.count()).select_from(AgentRun).where(*filters)
    )
    result = await session.scalars(
        select(AgentRun)
        .where(*filters)
        .order_by(AgentRun.started_at.desc())
        .limit(params.limit)
        .offset(params.offset)
    )

    return RunListResponse(
        items=list(result),
        total=cast(int, total),
        limit=params.limit,
        offset=params.offset,
    )


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


def _run_list_filters(params: RunListParams) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if params.status is not None:
        filters.append(AgentRun.status == params.status.value)
    if params.scenario_id is not None:
        filters.append(AgentRun.scenario_id == params.scenario_id)
    if params.agent_version is not None:
        filters.append(AgentRun.agent_version == params.agent_version)
    if params.model is not None:
        filters.append(
            exists()
            .where(LLMCall.run_id == AgentRun.run_id)
            .where(LLMCall.model == params.model)
        )
    if params.started_after is not None:
        filters.append(AgentRun.started_at >= params.started_after)
    if params.started_before is not None:
        filters.append(AgentRun.started_at <= params.started_before)
    return filters
