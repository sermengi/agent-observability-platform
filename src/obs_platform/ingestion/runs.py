from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.telemetry.v1.models import ErrorInfo, ExtendedRunEvent


@dataclass(frozen=True)
class IngestRunResult:
    run_id: str
    event_type: str
    status: str


async def ingest_run_event(
    session: AsyncSession,
    event: ExtendedRunEvent,
) -> IngestRunResult:
    now = datetime.now(UTC)

    async with session.begin():
        session.add(_agent_run_from_event(event, now))
        await session.flush()

        span_ids: dict[str, int] = {}
        for span_event in event.spans:
            parent_span_id = (
                span_ids[span_event.parent_span_id]
                if span_event.parent_span_id is not None
                else None
            )
            span = Span(
                run_id=event.run_id,
                span_id=span_event.span_id,
                parent_span_id=parent_span_id,
                name=span_event.name,
                sequence=span_event.sequence,
                started_at=span_event.started_at,
                completed_at=span_event.completed_at,
                status=span_event.status.value,
                input=span_event.input,
                output=span_event.output,
                metadata_=span_event.metadata,
                **_error_fields(span_event.error),
            )
            session.add(span)
            await session.flush()
            span_ids[span_event.span_id] = span.id

        for tool_call_event in event.tool_calls:
            session.add(
                ToolCall(
                    run_id=event.run_id,
                    tool_call_id=tool_call_event.tool_call_id,
                    span_id=span_ids[tool_call_event.span_id],
                    tool_name=tool_call_event.tool_name,
                    sequence=tool_call_event.sequence,
                    arguments=tool_call_event.arguments,
                    result=tool_call_event.result,
                    started_at=tool_call_event.started_at,
                    completed_at=tool_call_event.completed_at,
                    latency_ms=tool_call_event.latency_ms,
                    retry_count=tool_call_event.retry_count,
                    status=tool_call_event.status.value,
                    **_error_fields(tool_call_event.error),
                )
            )

        for llm_call_event in event.llm_calls:
            session.add(
                LLMCall(
                    run_id=event.run_id,
                    llm_call_id=llm_call_event.llm_call_id,
                    span_id=span_ids[llm_call_event.span_id],
                    call_type=llm_call_event.call_type.value,
                    model=llm_call_event.model,
                    provider=llm_call_event.provider,
                    started_at=llm_call_event.started_at,
                    completed_at=llm_call_event.completed_at,
                    latency_ms=llm_call_event.latency_ms,
                    prompt_tokens=llm_call_event.prompt_tokens,
                    completion_tokens=llm_call_event.completion_tokens,
                    total_tokens=llm_call_event.total_tokens,
                    estimated_cost_usd=llm_call_event.estimated_cost_usd,
                    input_payload=llm_call_event.input_payload,
                    output_payload=llm_call_event.output_payload,
                    status=llm_call_event.status.value,
                    **_error_fields(llm_call_event.error),
                )
            )

    return IngestRunResult(
        run_id=event.run_id,
        event_type=event.event_type.value,
        status=event.status.value,
    )


def _agent_run_from_event(event: ExtendedRunEvent, now: datetime) -> AgentRun:
    runtime_error = _error_fields(event.runtime_error, prefix="runtime_error")
    return AgentRun(
        run_id=event.run_id,
        schema_version=event.schema_version,
        event_type=event.event_type.value,
        agent_name=event.agent_name,
        agent_version=event.agent_version,
        prompt_version=event.prompt_version,
        environment=event.environment,
        raw_input=event.raw_input,
        normalized_input=event.normalized_input,
        scenario_id=event.scenario_id,
        started_at=event.started_at,
        completed_at=event.completed_at,
        status=event.status.value,
        execution_latency_ms=event.execution_latency_ms,
        wall_clock_duration_ms=event.wall_clock_duration_ms,
        resume_count=event.resume_count,
        hitl_required=event.hitl.required,
        hitl_state=event.hitl.state.value,
        hitl_checkpoint_id=event.hitl.checkpoint_id,
        hitl_decision=event.hitl.decision,
        hitl_requested_at=event.hitl.requested_at,
        hitl_decided_at=event.hitl.decided_at,
        hitl_pending_action=event.hitl.pending_action,
        usage_total_llm_calls=event.usage.total_llm_calls,
        usage_total_tool_calls=event.usage.total_tool_calls,
        usage_total_tokens=event.usage.total_tokens,
        usage_total_retries=event.usage.total_retries,
        usage_total_estimated_cost_usd=event.usage.total_estimated_cost_usd,
        final_result_output=(
            event.final_result.output if event.final_result is not None else None
        ),
        final_result_source_references=(
            event.final_result.source_references
            if event.final_result is not None
            else None
        ),
        ingested_at=now,
        updated_at=now,
        **runtime_error,
    )


def _error_fields(
    error: ErrorInfo | None,
    *,
    prefix: str = "error",
) -> dict[str, Any]:
    return {
        f"{prefix}_category": error.category if error is not None else None,
        f"{prefix}_code": error.code if error is not None else None,
        f"{prefix}_message": error.message if error is not None else None,
        f"{prefix}_failed_component": (
            error.failed_component if error is not None else None
        ),
    }
