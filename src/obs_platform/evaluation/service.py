from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.api.v1.schemas import (
    EvaluationTriggerResponse,
    EvaluatorResultSummary,
    RunFailureResponse,
)
from obs_platform.config import JudgeOnlySettings, JudgeSettings
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.classifier import EvaluatorOutcome, FailureClassifier
from obs_platform.evaluation.judges.client import (
    JudgeCallResult,
    JudgeClient,
    create_judge_client,
)
from obs_platform.evaluation.persistence import (
    persist_evaluation_result,
    persist_judge_call,
    persist_run_failure,
)
from obs_platform.evaluation.registry import ALL_EVALUATORS
from obs_platform.evaluation.types import (
    EvaluationFinding,
    EvaluationResult,
    EvaluationRunView,
    EvaluatorExecutionStatus,
    EvaluatorType,
    LLMCallView,
    SpanView,
    ToolCallView,
)


class RunNotFoundError(KeyError):
    pass


async def run_evaluation(
    session: AsyncSession,
    run_id: str,
    *,
    evaluators: Sequence[Evaluator] = ALL_EVALUATORS,
    judge_settings: JudgeSettings | None = None,
    judge_client_factory: Callable[[JudgeSettings], JudgeClient] = create_judge_client,
    persist_judge_call_fn: Callable[..., Any] = persist_judge_call,
    persist_run_failure_fn: Callable[..., Any] = persist_run_failure,
) -> EvaluationTriggerResponse:
    run = await evaluation_run_view(session, run_id)
    if run is None:
        raise RunNotFoundError(run_id)

    regression_run_id = await session.scalar(
        select(AgentRun.regression_run_id).where(AgentRun.run_id == run_id)
    )
    evaluated_at = datetime.now(UTC)
    outcomes: list[EvaluatorOutcome] = []
    judge_persistence_errors: list[Exception] = []
    active_judge_settings = judge_settings or get_judge_settings()
    judge_client: JudgeClient | None = None
    for evaluator in evaluators:
        active_evaluator = evaluator
        call_log: list[JudgeCallResult[Any]] = []
        if _should_skip_unconfigured_judge(active_evaluator, active_judge_settings):
            result = _judge_unavailable_result()
            status = EvaluatorExecutionStatus.SKIPPED
        else:
            try:
                if active_evaluator.type is EvaluatorType.DETERMINISTIC:
                    result = active_evaluator.evaluate(run)
                elif active_evaluator.type is EvaluatorType.LLM_BASED:
                    if judge_client is None:
                        judge_client = judge_client_factory(active_judge_settings)
                    active_evaluator = _with_judge_client(
                        active_evaluator, judge_client
                    )
                    result = await active_evaluator.evaluate_async(run, call_log)
                else:
                    raise ValueError(
                        f"unsupported evaluator type: {active_evaluator.type}"
                    )
                status = EvaluatorExecutionStatus.COMPLETED
            except Exception as exc:
                result = _evaluator_exception_result(exc)
                status = EvaluatorExecutionStatus.FAILED

        await persist_evaluation_result(
            session,
            run_id,
            active_evaluator,
            status,
            result,
            regression_run_id=regression_run_id,
        )
        outcomes.append(
            EvaluatorOutcome(
                evaluator_name=active_evaluator.name,
                evaluator_version=active_evaluator.version,
                execution_status=status,
                result=result,
            )
        )
        for call in call_log:
            try:
                await persist_judge_call_fn(
                    session,
                    run_id,
                    evaluator_name=active_evaluator.name,
                    evaluator_version=active_evaluator.version,
                    call=call,
                    succeeded=status is EvaluatorExecutionStatus.COMPLETED,
                )
            except Exception as exc:
                await session.rollback()
                judge_persistence_errors.append(exc)

    classifier = FailureClassifier()
    classification = classifier.classify(outcomes)
    await persist_run_failure_fn(session, run_id, classification)
    if judge_persistence_errors:
        raise judge_persistence_errors[0]

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


async def evaluation_run_view(
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


def get_judge_settings() -> JudgeSettings:
    return JudgeOnlySettings().judge


def _should_skip_unconfigured_judge(
    evaluator: Evaluator,
    judge_settings: JudgeSettings,
) -> bool:
    return (
        evaluator.type is EvaluatorType.LLM_BASED and not judge_settings.is_configured
    )


def _with_judge_client(evaluator: Evaluator, judge_client: JudgeClient) -> Evaluator:
    with_client = getattr(evaluator, "with_judge_client", None)
    if not callable(with_client):
        return evaluator
    configured_evaluator = with_client(judge_client)
    if not isinstance(configured_evaluator, Evaluator):
        raise TypeError("with_judge_client must return an Evaluator")
    return configured_evaluator


def _judge_unavailable_result() -> EvaluationResult:
    return EvaluationResult(
        passed=False,
        score=None,
        label=None,
        severity=None,
        reason="judge credentials not configured",
        findings=[
            EvaluationFinding(
                code="judge_unavailable",
                message="judge credentials not configured",
                data={},
            )
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
