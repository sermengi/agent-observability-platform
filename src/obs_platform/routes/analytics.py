from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import ColumnElement

from obs_platform.api.v1.schemas import OverviewAnalyticsResponse, RunCounts
from obs_platform.db.models import AgentRun
from obs_platform.telemetry.v1.enums import RunEventType, RunStatus

router = APIRouter()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    engine = cast(AsyncEngine, request.app.state.db_engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


class AnalyticsTimeRangeParams(BaseModel):
    started_after: datetime | None = None
    started_before: datetime | None = None


@router.get(
    "/analytics/overview",
    response_model=OverviewAnalyticsResponse,
    summary="Get overview analytics",
)
async def get_overview(
    params: Annotated[AnalyticsTimeRangeParams, Query()],
    session: AsyncSession = Depends(get_session),
) -> OverviewAnalyticsResponse:
    filters = _time_range_filters(params)
    aggregate_row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.coalesce(func.sum(AgentRun.usage_total_tokens), 0).label(
                    "tokens"
                ),
                func.coalesce(
                    func.sum(AgentRun.usage_total_estimated_cost_usd),
                    0.0,
                ).label("cost"),
                func.avg(AgentRun.execution_latency_ms).label("avg_latency"),
                func.percentile_cont(0.95)
                .within_group(AgentRun.execution_latency_ms)
                .filter(AgentRun.execution_latency_ms.is_not(None))
                .label("p95_latency"),
                func.count()
                .filter(AgentRun.event_type == RunEventType.RUN_FINAL.value)
                .label("terminal_count"),
                func.sum(
                    case(
                        (
                            (
                                AgentRun.event_type == RunEventType.RUN_FINAL.value
                            )
                            & (AgentRun.status == RunStatus.SUCCESS.value),
                            1,
                        ),
                        else_=0,
                    )
                ).label("success_count"),
            )
            .select_from(AgentRun)
            .where(*filters)
        )
    ).one()
    status_rows = await session.execute(
        select(AgentRun.status, func.count())
        .where(*filters)
        .group_by(AgentRun.status)
    )
    status_counts = {status: 0 for status in RunStatus}
    for status, count in status_rows:
        status_counts[RunStatus(status)] = count

    return OverviewAnalyticsResponse(
        runtime_success_rate=(
            aggregate_row.success_count / aggregate_row.terminal_count
            if aggregate_row.terminal_count > 0
            else None
        ),
        avg_latency_ms=(
            float(aggregate_row.avg_latency)
            if aggregate_row.avg_latency is not None
            else None
        ),
        p95_latency_ms=(
            float(aggregate_row.p95_latency)
            if aggregate_row.p95_latency is not None
            else None
        ),
        usage_total_tokens=aggregate_row.tokens,
        usage_total_estimated_cost_usd=aggregate_row.cost,
        run_counts=RunCounts(total=aggregate_row.total, by_status=status_counts),
    )


def _time_range_filters(
    params: AnalyticsTimeRangeParams,
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if params.started_after is not None:
        filters.append(AgentRun.started_at >= params.started_after)
    if params.started_before is not None:
        filters.append(AgentRun.started_at <= params.started_before)
    return filters
