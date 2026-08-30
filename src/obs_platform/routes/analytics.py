from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from obs_platform.api.deps import get_session
from obs_platform.api.v1.schemas import (
    CallTypeUsageBreakdown,
    FailureAnalyticsResponse,
    FailureRunCounts,
    FailureSeverityBreakdown,
    FailureTypeBreakdown,
    ModelUsageBreakdown,
    OverviewAnalyticsResponse,
    RunCounts,
    ToolAnalyticsResponse,
    ToolStats,
    UsageAnalyticsResponse,
    UsageTotals,
)
from obs_platform.db.models import AgentRun, LLMCall, RunFailure, ToolCall
from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    LLMCallType,
    RunEventType,
    RunStatus,
)

router = APIRouter()

__all__ = ["get_session", "router"]


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
                func.coalesce(func.sum(AgentRun.usage_total_tokens), 0).label("tokens"),
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
                            (AgentRun.event_type == RunEventType.RUN_FINAL.value)
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
        select(AgentRun.status, func.count()).where(*filters).group_by(AgentRun.status)
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


@router.get(
    "/analytics/tools",
    response_model=ToolAnalyticsResponse,
    summary="Get tool analytics",
)
async def get_tools(
    params: Annotated[AnalyticsTimeRangeParams, Query()],
    session: AsyncSession = Depends(get_session),
) -> ToolAnalyticsResponse:
    filters = _time_range_filters(params)
    rows = await session.execute(
        select(
            ToolCall.tool_name.label("tool_name"),
            func.count().label("call_count"),
            func.count()
            .filter(ToolCall.status == ExecutionStatus.SUCCESS.value)
            .label("success_count"),
            func.count()
            .filter(ToolCall.status == ExecutionStatus.FAILURE.value)
            .label("failure_count"),
            func.count()
            .filter(ToolCall.status == ExecutionStatus.ERROR.value)
            .label("error_count"),
            func.avg(ToolCall.latency_ms).label("avg_latency"),
            func.percentile_cont(0.95)
            .within_group(ToolCall.latency_ms)
            .filter(ToolCall.latency_ms.is_not(None))
            .label("p95_latency"),
        )
        .select_from(ToolCall)
        .join(AgentRun, AgentRun.run_id == ToolCall.run_id)
        .where(*filters)
        .group_by(ToolCall.tool_name)
        .order_by(func.count().desc(), ToolCall.tool_name.asc())
    )

    return ToolAnalyticsResponse(
        items=[
            ToolStats(
                tool_name=row.tool_name,
                call_count=row.call_count,
                success_count=row.success_count,
                failure_count=row.failure_count,
                error_count=row.error_count,
                failure_rate=(row.failure_count + row.error_count) / row.call_count,
                avg_latency_ms=(
                    float(row.avg_latency) if row.avg_latency is not None else None
                ),
                p95_latency_ms=(
                    float(row.p95_latency) if row.p95_latency is not None else None
                ),
            )
            for row in rows
        ]
    )


@router.get(
    "/analytics/usage",
    response_model=UsageAnalyticsResponse,
    summary="Get LLM usage analytics",
)
async def get_usage(
    params: Annotated[AnalyticsTimeRangeParams, Query()],
    session: AsyncSession = Depends(get_session),
) -> UsageAnalyticsResponse:
    filters = _time_range_filters(params)
    total_row = (
        await session.execute(
            select(
                func.count().label("call_count"),
                func.coalesce(func.sum(LLMCall.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(LLMCall.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(LLMCall.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0).label(
                    "total_estimated_cost_usd"
                ),
            )
            .select_from(LLMCall)
            .join(AgentRun, AgentRun.run_id == LLMCall.run_id)
            .where(*filters)
        )
    ).one()
    by_model_rows = await session.execute(
        select(
            LLMCall.provider.label("provider"),
            LLMCall.model.label("model"),
            func.count().label("call_count"),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.coalesce(func.sum(LLMCall.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0).label(
                "total_estimated_cost_usd"
            ),
        )
        .select_from(LLMCall)
        .join(AgentRun, AgentRun.run_id == LLMCall.run_id)
        .where(*filters)
        .group_by(LLMCall.provider, LLMCall.model)
        .order_by(
            func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0).desc(),
            LLMCall.provider.asc(),
            LLMCall.model.asc(),
        )
    )
    by_call_type_rows = await session.execute(
        select(
            LLMCall.call_type.label("call_type"),
            func.count().label("call_count"),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.coalesce(func.sum(LLMCall.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0).label(
                "total_estimated_cost_usd"
            ),
        )
        .select_from(LLMCall)
        .join(AgentRun, AgentRun.run_id == LLMCall.run_id)
        .where(*filters)
        .group_by(LLMCall.call_type)
        .order_by(
            func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0).desc(),
            LLMCall.call_type.asc(),
        )
    )

    return UsageAnalyticsResponse(
        total=_usage_totals(total_row),
        by_model=[
            ModelUsageBreakdown(
                provider=row.provider,
                model=row.model,
                **_usage_totals(row).model_dump(),
            )
            for row in by_model_rows
        ],
        by_call_type=[
            CallTypeUsageBreakdown(
                call_type=LLMCallType(row.call_type),
                **_usage_totals(row).model_dump(),
            )
            for row in by_call_type_rows
        ],
    )


@router.get(
    "/analytics/failures",
    response_model=FailureAnalyticsResponse,
    summary="Get failure analytics",
)
async def get_failures(
    params: Annotated[AnalyticsTimeRangeParams, Query()],
    session: AsyncSession = Depends(get_session),
) -> FailureAnalyticsResponse:
    filters = _time_range_filters(params)
    evaluated_total = await session.scalar(
        select(func.count())
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(*filters)
    )
    evaluated_count = int(evaluated_total or 0)

    overall_rows = await session.execute(
        select(RunFailure.overall_status, func.count())
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(*filters)
        .group_by(RunFailure.overall_status)
    )
    overall_counts = {"pass": 0, "fail": 0, "incomplete": 0}
    for overall_status, count in overall_rows:
        overall_counts[overall_status] = count
    failing_count = overall_counts["fail"] + overall_counts["incomplete"]

    failure_type_rows = await session.execute(
        select(
            RunFailure.primary_category.label("failure_type"),
            func.count().label("run_count"),
        )
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(
            *filters,
            RunFailure.overall_status == "fail",
            RunFailure.primary_category.is_not(None),
        )
        .group_by(RunFailure.primary_category)
        .order_by(func.count().desc(), RunFailure.primary_category.asc())
    )
    severity_rows = await session.execute(
        select(
            RunFailure.max_severity.label("severity"),
            func.count().label("run_count"),
        )
        .select_from(RunFailure)
        .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
        .where(
            *filters,
            RunFailure.overall_status == "fail",
            RunFailure.max_severity.is_not(None),
        )
        .group_by(RunFailure.max_severity)
        .order_by(func.count().desc(), RunFailure.max_severity.asc())
    )

    return FailureAnalyticsResponse(
        run_counts=FailureRunCounts(
            total=evaluated_count,
            by_overall_status=overall_counts,
        ),
        by_failure_type=[
            FailureTypeBreakdown(
                failure_type=row.failure_type,
                count=row.run_count,
                pct_of_evaluated=_ratio(row.run_count, evaluated_count),
                pct_of_failing=_ratio(row.run_count, failing_count),
            )
            for row in failure_type_rows
        ],
        by_severity=[
            FailureSeverityBreakdown(
                severity=row.severity,
                count=row.run_count,
                pct_of_evaluated=_ratio(row.run_count, evaluated_count),
                pct_of_failing=_ratio(row.run_count, failing_count),
            )
            for row in severity_rows
        ],
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


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _usage_totals(row: Any) -> UsageTotals:
    return UsageTotals(
        call_count=row.call_count,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        total_estimated_cost_usd=float(row.total_estimated_cost_usd),
    )
