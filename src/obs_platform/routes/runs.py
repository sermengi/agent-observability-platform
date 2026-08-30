from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from obs_platform.api.deps import get_session
from obs_platform.api.v1.schemas import (
    ErrorResponse,
    EvaluationTriggerResponse,
    EvaluatorResultSummary,
    FinalResultResponse,
    HITLResponse,
    LLMCallResponse,
    RunDetailResponse,
    RunFailureResponse,
    RunFailureSummary,
    RunListResponse,
    RunSummary,
    SpanResponse,
    ToolCallResponse,
    UsageResponse,
)
from obs_platform.db.models import AgentRun, LLMCall, RunFailure, Span, ToolCall
from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.evaluation.classifier import (
    EvaluatorOutcome,
    FailureClassifier,
)
from obs_platform.evaluation.persistence import (
    persist_evaluation_result,
    persist_run_failure,
)
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorExecutionStatus,
    LLMCallView,
    SpanView,
    ToolCallView,
)
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.telemetry.v1.enums import RunStatus
from obs_platform.telemetry.v1.models import ExtendedRunEvent

router = APIRouter()

__all__ = ["get_session", "router"]


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
    overall_status: str | None = None
    primary_failure_type: str | None = None
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
        select(func.count())
        .select_from(AgentRun)
        .outerjoin(RunFailure, RunFailure.run_id == AgentRun.run_id)
        .where(*filters)
    )
    rows = (
        await session.execute(
            select(AgentRun, RunFailure)
            .outerjoin(RunFailure, RunFailure.run_id == AgentRun.run_id)
            .where(*filters)
            .order_by(AgentRun.started_at.desc())
            .limit(params.limit)
            .offset(params.offset)
        )
    ).all()

    return RunListResponse(
        items=[
            _run_summary_response(run, run_failure)
            for run, run_failure in rows
        ],
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


@router.post(
    "/runs/{run_id}/evaluate",
    response_model=EvaluationTriggerResponse,
    summary="Evaluate a run",
)
async def evaluate_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> EvaluationTriggerResponse:
    run = await _evaluation_run_view(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    evaluated_at = datetime.now(UTC)
    outcomes: list[EvaluatorOutcome] = []
    for evaluator in DETERMINISTIC_EVALUATORS:
        try:
            result = evaluator.evaluate(run)
            status = EvaluatorExecutionStatus.COMPLETED
        except Exception as exc:
            result = _evaluator_exception_result(exc)
            status = EvaluatorExecutionStatus.FAILED

        await persist_evaluation_result(session, run_id, evaluator, status, result)
        outcomes.append(
            EvaluatorOutcome(
                evaluator_name=evaluator.name,
                evaluator_version=evaluator.version,
                execution_status=status,
                result=result,
            )
        )

    classifier = FailureClassifier()
    classification = classifier.classify(outcomes)
    await persist_run_failure(session, run_id, classification)

    return EvaluationTriggerResponse(
        run_id=run_id,
        overall_status=classification.overall_status.value,
        evaluator_results=[
            EvaluatorResultSummary(
                evaluator_name=outcome.evaluator_name,
                evaluator_version=outcome.evaluator_version,
                execution_status=outcome.execution_status.value,
                passed=(
                    outcome.result.passed
                    if outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
                    and outcome.result is not None
                    else None
                ),
                score=(
                    outcome.result.score
                    if outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
                    and outcome.result is not None
                    else None
                ),
                label=(
                    outcome.result.label
                    if outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
                    and outcome.result is not None
                    else None
                ),
                severity=(
                    outcome.result.severity
                    if outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
                    and outcome.result is not None
                    else None
                ),
                reason=outcome.result.reason if outcome.result is not None else None,
                findings=(
                    [
                        finding.model_dump(mode="json")
                        for finding in outcome.result.findings
                    ]
                    if outcome.result is not None
                    else []
                ),
            )
            for outcome in outcomes
        ],
        failure=RunFailureResponse(
            primary_category=(
                classification.primary_category.value
                if classification.primary_category is not None
                else None
            ),
            secondary_category=(
                classification.secondary_category.value
                if classification.secondary_category is not None
                else None
            ),
            max_severity=classification.max_severity,
        ),
        evaluated_at=evaluated_at,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    summary="Get run detail",
)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> RunDetailResponse:
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    spans = list(
        await session.scalars(
            select(Span).where(Span.run_id == run_id).order_by(Span.sequence)
        )
    )
    span_ids = {span.id: span.span_id for span in spans}
    tool_calls = list(
        await session.scalars(
            select(ToolCall)
            .where(ToolCall.run_id == run_id)
            .order_by(ToolCall.sequence)
        )
    )
    llm_calls = list(
        await session.scalars(
            select(LLMCall)
            .where(LLMCall.run_id == run_id)
            .order_by(LLMCall.started_at, LLMCall.llm_call_id)
        )
    )
    run_failure = await session.get(RunFailure, run_id)
    evaluation_summary = await _latest_evaluation_summary(session, run_id)

    return RunDetailResponse(
        run_id=run.run_id,
        scenario_id=run.scenario_id,
        agent_name=run.agent_name,
        agent_version=run.agent_version,
        prompt_version=run.prompt_version,
        environment=run.environment,
        status=run.status,
        event_type=run.event_type,
        raw_input=run.raw_input,
        normalized_input=run.normalized_input,
        started_at=run.started_at,
        completed_at=run.completed_at,
        execution_latency_ms=run.execution_latency_ms,
        wall_clock_duration_ms=run.wall_clock_duration_ms,
        resume_count=run.resume_count,
        spans=[
            SpanResponse(
                span_id=span.span_id,
                parent_span_id=(
                    span_ids[span.parent_span_id]
                    if span.parent_span_id is not None
                    else None
                ),
                name=span.name,
                sequence=span.sequence,
                started_at=span.started_at,
                completed_at=span.completed_at,
                status=span.status,
                input=span.input,
                output=span.output,
                metadata=span.metadata_,
                error=_error_response(
                    span.error_category,
                    span.error_code,
                    span.error_message,
                    span.error_failed_component,
                ),
            )
            for span in spans
        ],
        tool_calls=[
            ToolCallResponse(
                tool_call_id=tool_call.tool_call_id,
                span_id=span_ids[tool_call.span_id],
                tool_name=tool_call.tool_name,
                sequence=tool_call.sequence,
                arguments=tool_call.arguments,
                result=tool_call.result,
                started_at=tool_call.started_at,
                completed_at=tool_call.completed_at,
                latency_ms=tool_call.latency_ms,
                retry_count=tool_call.retry_count,
                status=tool_call.status,
                error=_error_response(
                    tool_call.error_category,
                    tool_call.error_code,
                    tool_call.error_message,
                    tool_call.error_failed_component,
                ),
            )
            for tool_call in tool_calls
        ],
        llm_calls=[
            LLMCallResponse(
                llm_call_id=llm_call.llm_call_id,
                span_id=span_ids[llm_call.span_id],
                sequence=sequence,
                call_type=llm_call.call_type,
                model=llm_call.model,
                provider=llm_call.provider,
                started_at=llm_call.started_at,
                completed_at=llm_call.completed_at,
                latency_ms=llm_call.latency_ms,
                prompt_tokens=llm_call.prompt_tokens,
                completion_tokens=llm_call.completion_tokens,
                total_tokens=llm_call.total_tokens,
                estimated_cost_usd=llm_call.estimated_cost_usd,
                input_payload=llm_call.input_payload,
                output_payload=llm_call.output_payload,
                status=llm_call.status,
                error=_error_response(
                    llm_call.error_category,
                    llm_call.error_code,
                    llm_call.error_message,
                    llm_call.error_failed_component,
                ),
            )
            for sequence, llm_call in enumerate(llm_calls, start=1)
        ],
        hitl=HITLResponse(
            required=run.hitl_required,
            state=run.hitl_state,
            checkpoint_id=run.hitl_checkpoint_id,
            decision=run.hitl_decision,
            requested_at=run.hitl_requested_at,
            decided_at=run.hitl_decided_at,
            pending_action=run.hitl_pending_action,
        ),
        usage=UsageResponse(
            total_llm_calls=run.usage_total_llm_calls,
            total_tool_calls=run.usage_total_tool_calls,
            total_tokens=run.usage_total_tokens,
            total_estimated_cost_usd=run.usage_total_estimated_cost_usd,
            total_retries=run.usage_total_retries,
        ),
        final_result=(
            FinalResultResponse(
                output=run.final_result_output,
                source_references=run.final_result_source_references or [],
            )
            if run.final_result_output is not None
            else None
        ),
        runtime_error=_error_response(
            run.runtime_error_category,
            run.runtime_error_code,
            run.runtime_error_message,
            run.runtime_error_failed_component,
        ),
        failure=(
            RunFailureSummary(
                overall_status=run_failure.overall_status,
                primary_failure_type=run_failure.primary_category,
                secondary_failure_type=run_failure.secondary_category,
                max_severity=run_failure.max_severity,
                classifier_version=run_failure.classifier_version,
                updated_at=run_failure.updated_at,
            )
            if run_failure is not None
            else None
        ),
        evaluation_summary=evaluation_summary or None,
    )


def _run_summary_response(
    run: AgentRun,
    run_failure: RunFailure | None,
) -> RunSummary:
    return RunSummary(
        run_id=run.run_id,
        scenario_id=run.scenario_id,
        agent_name=run.agent_name,
        agent_version=run.agent_version,
        prompt_version=run.prompt_version,
        environment=run.environment,
        status=run.status,
        event_type=run.event_type,
        hitl_state=run.hitl_state,
        started_at=run.started_at,
        completed_at=run.completed_at,
        execution_latency_ms=run.execution_latency_ms,
        wall_clock_duration_ms=run.wall_clock_duration_ms,
        usage_total_tokens=run.usage_total_tokens,
        usage_total_estimated_cost_usd=run.usage_total_estimated_cost_usd,
        overall_status=(
            run_failure.overall_status if run_failure is not None else None
        ),
        primary_failure_type=(
            run_failure.primary_category if run_failure is not None else None
        ),
        max_severity=run_failure.max_severity if run_failure is not None else None,
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
    if params.overall_status is not None:
        filters.append(RunFailure.overall_status == params.overall_status)
    if params.primary_failure_type is not None:
        filters.append(RunFailure.primary_category == params.primary_failure_type)
    if params.started_after is not None:
        filters.append(AgentRun.started_at >= params.started_after)
    if params.started_before is not None:
        filters.append(AgentRun.started_at <= params.started_before)
    return filters


async def _latest_evaluation_summary(
    session: AsyncSession,
    run_id: str,
) -> list[EvaluatorResultSummary]:
    latest_rank = (
        select(
            EvaluationResultRecord.id,
            func.row_number()
            .over(
                partition_by=EvaluationResultRecord.evaluator_name,
                order_by=(
                    EvaluationResultRecord.created_at.desc(),
                    EvaluationResultRecord.id.desc(),
                ),
            )
            .label("latest_rank"),
        )
        .where(EvaluationResultRecord.run_id == run_id)
        .subquery()
    )
    rows = list(
        await session.scalars(
            select(EvaluationResultRecord)
            .join(latest_rank, latest_rank.c.id == EvaluationResultRecord.id)
            .where(latest_rank.c.latest_rank == 1)
            .order_by(EvaluationResultRecord.evaluator_name)
        )
    )
    return [
        EvaluatorResultSummary(
            evaluator_name=row.evaluator_name,
            evaluator_version=row.evaluator_version,
            execution_status=row.status,
            passed=row.passed,
            score=row.score,
            label=row.label,
            severity=row.severity,
            reason=row.reason,
            findings=row.findings or [],
        )
        for row in rows
    ]


async def _evaluation_run_view(
    session: AsyncSession,
    run_id: str,
) -> EvaluationRunView | None:
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
    if run is None:
        return None

    spans = list(
        await session.scalars(
            select(Span).where(Span.run_id == run_id).order_by(Span.sequence)
        )
    )
    span_ids = {span.id: span.span_id for span in spans}
    tool_calls = list(
        await session.scalars(
            select(ToolCall)
            .where(ToolCall.run_id == run_id)
            .order_by(ToolCall.sequence)
        )
    )
    llm_calls = list(
        await session.scalars(
            select(LLMCall)
            .where(LLMCall.run_id == run_id)
            .order_by(LLMCall.started_at, LLMCall.llm_call_id)
        )
    )

    return EvaluationRunView(
        run_id=run.run_id,
        schema_version=run.schema_version,
        event_type=run.event_type,
        agent_name=run.agent_name,
        agent_version=run.agent_version,
        prompt_version=run.prompt_version,
        environment=run.environment,
        raw_input=run.raw_input,
        normalized_input=run.normalized_input,
        scenario_id=run.scenario_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        status=run.status,
        execution_latency_ms=run.execution_latency_ms,
        wall_clock_duration_ms=run.wall_clock_duration_ms,
        resume_count=run.resume_count,
        hitl_required=run.hitl_required,
        hitl_state=run.hitl_state,
        hitl_checkpoint_id=run.hitl_checkpoint_id,
        hitl_decision=run.hitl_decision,
        hitl_requested_at=run.hitl_requested_at,
        hitl_decided_at=run.hitl_decided_at,
        hitl_pending_action=run.hitl_pending_action,
        usage_total_llm_calls=run.usage_total_llm_calls,
        usage_total_tool_calls=run.usage_total_tool_calls,
        usage_total_tokens=run.usage_total_tokens,
        usage_total_retries=run.usage_total_retries,
        usage_total_estimated_cost_usd=run.usage_total_estimated_cost_usd,
        final_result_output=run.final_result_output,
        final_result_source_references=run.final_result_source_references,
        runtime_error_category=run.runtime_error_category,
        runtime_error_code=run.runtime_error_code,
        runtime_error_message=run.runtime_error_message,
        runtime_error_failed_component=run.runtime_error_failed_component,
        spans=[
            SpanView(
                span_id=span.span_id,
                parent_span_id=(
                    span_ids[span.parent_span_id]
                    if span.parent_span_id is not None
                    else None
                ),
                name=span.name,
                sequence=span.sequence,
                started_at=span.started_at,
                completed_at=span.completed_at,
                status=span.status,
                input=span.input,
                output=span.output,
                metadata=span.metadata_,
                error_category=span.error_category,
                error_code=span.error_code,
                error_message=span.error_message,
                error_failed_component=span.error_failed_component,
            )
            for span in spans
        ],
        tool_calls=[
            ToolCallView(
                tool_call_id=tool_call.tool_call_id,
                span_id=span_ids[tool_call.span_id],
                tool_name=tool_call.tool_name,
                sequence=tool_call.sequence,
                arguments=tool_call.arguments,
                result=tool_call.result,
                started_at=tool_call.started_at,
                completed_at=tool_call.completed_at,
                latency_ms=tool_call.latency_ms,
                retry_count=tool_call.retry_count,
                status=tool_call.status,
                error_category=tool_call.error_category,
                error_code=tool_call.error_code,
                error_message=tool_call.error_message,
                error_failed_component=tool_call.error_failed_component,
            )
            for tool_call in tool_calls
        ],
        llm_calls=[
            LLMCallView(
                llm_call_id=llm_call.llm_call_id,
                span_id=span_ids[llm_call.span_id],
                sequence=sequence,
                call_type=llm_call.call_type,
                model=llm_call.model,
                provider=llm_call.provider,
                started_at=llm_call.started_at,
                completed_at=llm_call.completed_at,
                latency_ms=llm_call.latency_ms,
                prompt_tokens=llm_call.prompt_tokens,
                completion_tokens=llm_call.completion_tokens,
                total_tokens=llm_call.total_tokens,
                estimated_cost_usd=llm_call.estimated_cost_usd,
                input_payload=llm_call.input_payload,
                output_payload=llm_call.output_payload,
                status=llm_call.status,
                error_category=llm_call.error_category,
                error_code=llm_call.error_code,
                error_message=llm_call.error_message,
                error_failed_component=llm_call.error_failed_component,
            )
            for sequence, llm_call in enumerate(llm_calls, start=1)
        ],
    )


def _evaluator_exception_result(exc: Exception) -> EvaluationResult:
    return EvaluationResult(
        passed=False,
        score=None,
        label=None,
        severity=None,
        reason=f"{type(exc).__name__}: {exc}",
        findings=[
            EvaluationFinding(
                code="evaluator_exception",
                message="Evaluator raised an exception",
                data={
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        ],
    )


def _error_response(
    category: str | None,
    code: str | None,
    message: str | None,
    failed_component: str | None,
) -> ErrorResponse | None:
    if category is None or message is None:
        return None
    return ErrorResponse(
        category=category,
        code=code,
        message=message,
        failed_component=failed_component,
    )
