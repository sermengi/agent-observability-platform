from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.telemetry.v1.enums import HITLState
from obs_platform.telemetry.v1.models import (
    ErrorInfo,
    ExtendedRunEvent,
)
from obs_platform.telemetry.v1.models import (
    LLMCall as LLMCallEvent,
)
from obs_platform.telemetry.v1.models import (
    Span as SpanEvent,
)
from obs_platform.telemetry.v1.models import (
    ToolCall as ToolCallEvent,
)


@dataclass(frozen=True)
class IngestRunResult:
    run_id: str
    event_type: str
    status: str


class HITLStateRegressionError(RuntimeError):
    pass


async def ingest_run_event(
    session: AsyncSession,
    event: ExtendedRunEvent,
) -> IngestRunResult:
    now = datetime.now(UTC)

    async with session.begin():
        await _reject_pending_after_terminal_hitl_state(session, event)
        await _upsert_agent_run(session, event, now)
        span_ids = await _upsert_spans(session, event.run_id, event.spans)
        await _resolve_span_parents(session, event.run_id, event.spans, span_ids)

        for tool_call_event in event.tool_calls:
            await _upsert_tool_call(
                session,
                event.run_id,
                tool_call_event,
                span_ids[tool_call_event.span_id],
            )

        for llm_call_event in event.llm_calls:
            await _upsert_llm_call(
                session,
                event.run_id,
                llm_call_event,
                span_ids[llm_call_event.span_id],
            )

        await _refresh_run_usage_totals(session, event.run_id)

    return IngestRunResult(
        run_id=event.run_id,
        event_type=event.event_type.value,
        status=event.status.value,
    )


async def _reject_pending_after_terminal_hitl_state(
    session: AsyncSession,
    event: ExtendedRunEvent,
) -> None:
    if event.hitl.state is not HITLState.PENDING:
        return

    stored_hitl_state = await session.scalar(
        select(AgentRun.hitl_state).where(AgentRun.run_id == event.run_id)
    )
    if stored_hitl_state in {HITLState.APPROVED.value, HITLState.REJECTED.value}:
        raise HITLStateRegressionError(
            f"run {event.run_id} is already in terminal HITL state "
            f"{stored_hitl_state!r} and cannot be moved back to pending"
        )


async def _upsert_agent_run(
    session: AsyncSession,
    event: ExtendedRunEvent,
    now: datetime,
) -> None:
    values = _agent_run_values(event, now)
    table = cast(Table, AgentRun.__table__)
    statement = insert(table).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.run_id],
            set_=_update_values(statement, values, exclude={"run_id", "ingested_at"}),
        )
    )


async def _upsert_spans(
    session: AsyncSession,
    run_id: str,
    spans: list[SpanEvent],
) -> dict[str, int]:
    table = cast(Table, Span.__table__)
    span_ids: dict[str, int] = {}
    for span_event in spans:
        values = _span_values(run_id, span_event)
        statement = insert(table).values(values)
        result = await session.execute(
            statement.on_conflict_do_update(
                index_elements=[table.c.run_id, table.c.span_id],
                set_=_update_values(
                    statement,
                    values,
                    exclude={"run_id", "span_id"},
                ),
            ).returning(table.c.span_id, table.c.id)
        )
        span_id, internal_id = result.one()
        span_ids[span_id] = internal_id

    return span_ids


async def _resolve_span_parents(
    session: AsyncSession,
    run_id: str,
    spans: list[SpanEvent],
    span_ids: dict[str, int],
) -> None:
    for span_event in spans:
        parent_span_id = (
            span_ids[span_event.parent_span_id]
            if span_event.parent_span_id is not None
            else None
        )
        await session.execute(
            update(Span)
            .where(Span.run_id == run_id, Span.span_id == span_event.span_id)
            .values(parent_span_id=parent_span_id)
        )


async def _upsert_tool_call(
    session: AsyncSession,
    run_id: str,
    tool_call: ToolCallEvent,
    span_id: int,
) -> None:
    table = cast(Table, ToolCall.__table__)
    values = _tool_call_values(run_id, tool_call, span_id)
    statement = insert(table).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.run_id, table.c.tool_call_id],
            set_=_update_values(
                statement,
                values,
                exclude={"run_id", "tool_call_id"},
            ),
        )
    )


async def _upsert_llm_call(
    session: AsyncSession,
    run_id: str,
    llm_call: LLMCallEvent,
    span_id: int,
) -> None:
    table = cast(Table, LLMCall.__table__)
    values = _llm_call_values(run_id, llm_call, span_id)
    statement = insert(table).values(values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[table.c.run_id, table.c.llm_call_id],
            set_=_update_values(
                statement,
                values,
                exclude={"run_id", "llm_call_id"},
            ),
        )
    )


async def _refresh_run_usage_totals(session: AsyncSession, run_id: str) -> None:
    llm_call_count = (
        select(func.count())
        .select_from(LLMCall)
        .where(LLMCall.run_id == run_id)
        .scalar_subquery()
    )
    tool_call_count = (
        select(func.count())
        .select_from(ToolCall)
        .where(ToolCall.run_id == run_id)
        .scalar_subquery()
    )
    token_sum = (
        select(func.coalesce(func.sum(LLMCall.total_tokens), 0))
        .where(LLMCall.run_id == run_id)
        .scalar_subquery()
    )
    retry_sum = (
        select(func.coalesce(func.sum(ToolCall.retry_count), 0))
        .where(ToolCall.run_id == run_id)
        .scalar_subquery()
    )
    estimated_cost_sum = (
        select(func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0.0))
        .where(LLMCall.run_id == run_id)
        .scalar_subquery()
    )

    await session.execute(
        update(AgentRun)
        .where(AgentRun.run_id == run_id)
        .values(
            usage_total_llm_calls=llm_call_count,
            usage_total_tool_calls=tool_call_count,
            usage_total_tokens=token_sum,
            usage_total_retries=retry_sum,
            usage_total_estimated_cost_usd=estimated_cost_sum,
        )
    )


def _agent_run_values(event: ExtendedRunEvent, now: datetime) -> dict[str, Any]:
    runtime_error = _error_fields(event.runtime_error, prefix="runtime_error")
    return {
        "run_id": event.run_id,
        "schema_version": event.schema_version,
        "event_type": event.event_type.value,
        "agent_name": event.agent_name,
        "agent_version": event.agent_version,
        "prompt_version": event.prompt_version,
        "environment": event.environment,
        "raw_input": event.raw_input,
        "normalized_input": event.normalized_input,
        "scenario_id": event.scenario_id,
        "started_at": event.started_at,
        "completed_at": event.completed_at,
        "status": event.status.value,
        "execution_latency_ms": event.execution_latency_ms,
        "wall_clock_duration_ms": event.wall_clock_duration_ms,
        "resume_count": event.resume_count,
        "hitl_required": event.hitl.required,
        "hitl_state": event.hitl.state.value,
        "hitl_checkpoint_id": event.hitl.checkpoint_id,
        "hitl_decision": event.hitl.decision,
        "hitl_requested_at": event.hitl.requested_at,
        "hitl_decided_at": event.hitl.decided_at,
        "hitl_pending_action": event.hitl.pending_action,
        "final_result_output": (
            event.final_result.output if event.final_result is not None else None
        ),
        "final_result_source_references": (
            event.final_result.source_references
            if event.final_result is not None
            else None
        ),
        "ingested_at": now,
        "updated_at": now,
        **runtime_error,
    }


def _span_values(run_id: str, span: SpanEvent) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "span_id": span.span_id,
        "parent_span_id": None,
        "name": span.name,
        "sequence": span.sequence,
        "started_at": span.started_at,
        "completed_at": span.completed_at,
        "status": span.status.value,
        "input": span.input,
        "output": span.output,
        "metadata": span.metadata,
        **_error_fields(span.error),
    }


def _tool_call_values(
    run_id: str,
    tool_call: ToolCallEvent,
    span_id: int,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "tool_call_id": tool_call.tool_call_id,
        "span_id": span_id,
        "tool_name": tool_call.tool_name,
        "sequence": tool_call.sequence,
        "arguments": tool_call.arguments,
        "result": tool_call.result,
        "started_at": tool_call.started_at,
        "completed_at": tool_call.completed_at,
        "latency_ms": tool_call.latency_ms,
        "retry_count": tool_call.retry_count,
        "status": tool_call.status.value,
        **_error_fields(tool_call.error),
    }


def _llm_call_values(
    run_id: str,
    llm_call: LLMCallEvent,
    span_id: int,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "llm_call_id": llm_call.llm_call_id,
        "span_id": span_id,
        "call_type": llm_call.call_type.value,
        "model": llm_call.model,
        "provider": llm_call.provider,
        "started_at": llm_call.started_at,
        "completed_at": llm_call.completed_at,
        "latency_ms": llm_call.latency_ms,
        "prompt_tokens": llm_call.prompt_tokens,
        "completion_tokens": llm_call.completion_tokens,
        "total_tokens": llm_call.total_tokens,
        "estimated_cost_usd": llm_call.estimated_cost_usd,
        "input_payload": llm_call.input_payload,
        "output_payload": llm_call.output_payload,
        "status": llm_call.status.value,
        **_error_fields(llm_call.error),
    }


def _update_values(
    statement: Any,
    values: dict[str, Any],
    *,
    exclude: set[str],
) -> dict[str, Any]:
    return {
        column_name: getattr(statement.excluded, column_name)
        for column_name in values
        if column_name not in exclude
    }


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
