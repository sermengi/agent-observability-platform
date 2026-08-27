import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseSettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.ingestion.runs import HITLStateRegressionError, ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture

CANONICAL_FIXTURES = (
    "healthy_success",
    "hitl_pending",
    "hitl_approved",
    "unsupported_claim_candidate",
    "policy_violation",
    "tool_failure",
    "retrieval_failure",
    "trajectory_error",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(
        DatabaseSettings(
            host="localhost",
            port=5432,
            user="observability",
            password="change-me",
            name="observability",
        )
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def test_ingest_run_event_can_be_called_directly_with_async_session(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task3-direct-call")

    await _delete_run(session, event.run_id)
    result = await ingest_run_event(session, event)

    assert result.run_id == event.run_id
    assert result.event_type == event.event_type.value
    assert result.status == event.status.value
    assert await _count_for_run(session, AgentRun, event.run_id) == 1
    assert await _count_for_run(session, Span, event.run_id) == len(event.spans)
    assert await _count_for_run(session, ToolCall, event.run_id) == len(
        event.tool_calls
    )
    assert await _count_for_run(session, LLMCall, event.run_id) == len(event.llm_calls)

    await _delete_run(session, event.run_id)


async def test_post_runs_valid_fixture_returns_lifecycle_echo(
    session: AsyncSession,
) -> None:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    event = _fixture_with_run_id("healthy_success", "task3-http-ingestion")
    await _delete_run(session, event.run_id)
    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/runs",
            json=event.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": event.run_id,
        "event_type": event.event_type.value,
        "status": event.status.value,
    }

    await _delete_run(session, event.run_id)


async def test_ingest_run_event_rolls_back_when_child_normalization_fails(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task3-rollback")
    event.tool_calls[0].span_id = "missing-after-validation"

    await _delete_run(session, event.run_id)
    with pytest.raises(KeyError):
        await ingest_run_event(session, event)

    assert await _count_for_run(session, AgentRun, event.run_id) == 0
    assert await _count_for_run(session, Span, event.run_id) == 0
    assert await _count_for_run(session, ToolCall, event.run_id) == 0
    assert await _count_for_run(session, LLMCall, event.run_id) == 0


async def test_hitl_reingestion_preserves_carried_over_child_identity(
    session: AsyncSession,
) -> None:
    pending = _fixture_with_run_id("hitl_pending", "task4-hitl-identity")
    approved = _fixture_with_run_id("hitl_approved", pending.run_id)

    await _delete_run(session, pending.run_id)
    await ingest_run_event(session, pending)
    pending_span_ids = await _span_internal_ids(session, pending.run_id)
    pending_tool_span_ids = await _tool_span_ids(session, pending.run_id)
    pending_llm_span_ids = await _llm_span_ids(session, pending.run_id)
    await session.commit()

    await ingest_run_event(session, approved)
    approved_span_ids = await _span_internal_ids(session, approved.run_id)
    approved_tool_span_ids = await _tool_span_ids(session, approved.run_id)
    approved_llm_span_ids = await _llm_span_ids(session, approved.run_id)

    for span_id in {span.span_id for span in pending.spans}:
        assert approved_span_ids[span_id] == pending_span_ids[span_id]

    for tool_call_id in {tool_call.tool_call_id for tool_call in pending.tool_calls}:
        assert (
            approved_tool_span_ids[tool_call_id] == pending_tool_span_ids[tool_call_id]
        )

    for llm_call_id in {llm_call.llm_call_id for llm_call in pending.llm_calls}:
        assert approved_llm_span_ids[llm_call_id] == pending_llm_span_ids[llm_call_id]

    await _delete_run(session, pending.run_id)


async def test_child_span_before_parent_resolves_parent_span_id(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task4-child-before-parent")
    event.spans = [event.spans[1], event.spans[0]]

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)

    span_ids = await _span_internal_ids(session, event.run_id)
    parent_span_id = await session.scalar(
        select(Span.parent_span_id).where(
            Span.run_id == event.run_id,
            Span.span_id == "span-healthy-evidence",
        )
    )

    assert parent_span_id == span_ids["span-healthy-root"]

    await _delete_run(session, event.run_id)


async def test_reingesting_identical_payload_does_not_duplicate_rows(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task5-no-duplicates")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)
    initial_counts = await _entity_counts(session, event.run_id)
    await session.commit()

    await ingest_run_event(session, event)

    assert await _entity_counts(session, event.run_id) == initial_counts

    await _delete_run(session, event.run_id)


async def test_reingesting_identical_payload_keeps_span_identities_stable(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task5-stable-identities")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)
    first_span_ids = await _span_internal_ids(session, event.run_id)
    first_updated_at = await _run_updated_at(session, event.run_id)
    await session.commit()
    await asyncio.sleep(0.001)

    await ingest_run_event(session, event)
    second_span_ids = await _span_internal_ids(session, event.run_id)
    second_updated_at = await _run_updated_at(session, event.run_id)

    assert second_span_ids == first_span_ids
    assert second_updated_at > first_updated_at

    await _delete_run(session, event.run_id)


async def test_reingesting_payload_keeps_ingested_at_stable(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task5-stable-ingested-at")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)
    first_ingested_at = await _run_ingested_at(session, event.run_id)
    await session.commit()
    await asyncio.sleep(0.001)

    await ingest_run_event(session, event)
    second_ingested_at = await _run_ingested_at(session, event.run_id)

    assert second_ingested_at == first_ingested_at

    await _delete_run(session, event.run_id)


async def test_reingestion_preserves_extra_child_rows_for_same_run(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task5-preserve-extra-child")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)
    span_ids = await _span_internal_ids(session, event.run_id)
    session.add(
        ToolCall(
            run_id=event.run_id,
            tool_call_id="manual-extra-tool-call",
            span_id=span_ids["span-healthy-root"],
            tool_name="manual_debug_tool",
            sequence=999,
            arguments={"source": "test"},
            result={"preserved": True},
            started_at=event.started_at,
            completed_at=event.started_at,
            retry_count=0,
            status="success",
        )
    )
    await session.commit()

    await ingest_run_event(session, event)

    extra_tool_call = await session.scalar(
        select(ToolCall.tool_call_id).where(
            ToolCall.run_id == event.run_id,
            ToolCall.tool_call_id == "manual-extra-tool-call",
        )
    )
    assert extra_tool_call == "manual-extra-tool-call"

    await _delete_run(session, event.run_id)


async def test_ingestion_uses_one_upsert_statement_per_child_row(
    session: AsyncSession,
) -> None:
    run_event = _fixture_with_run_id("healthy_success", "task5-per-row-upserts")
    statements: list[str] = []

    def capture_sql(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO spans"):
            statements.append(statement)
        if statement.startswith("INSERT INTO tool_calls"):
            statements.append(statement)
        if statement.startswith("INSERT INTO llm_calls"):
            statements.append(statement)

    await _delete_run(session, run_event.run_id)
    sync_engine = cast(Engine, cast(Any, session.bind).sync_engine)
    sqlalchemy_event.listen(sync_engine, "before_cursor_execute", capture_sql)
    try:
        await ingest_run_event(session, run_event)
    finally:
        sqlalchemy_event.remove(sync_engine, "before_cursor_execute", capture_sql)

    assert len(statements) == len(run_event.spans) + len(run_event.tool_calls) + len(
        run_event.llm_calls
    )
    assert all("ON CONFLICT" in statement for statement in statements)
    assert not any("), (" in statement for statement in statements)

    await _delete_run(session, run_event.run_id)


async def test_hitl_pending_then_approved_overwrites_run_lifecycle_state(
    session: AsyncSession,
) -> None:
    pending = _fixture_with_run_id("hitl_pending", "task6-hitl-approved")
    approved = _fixture_with_run_id("hitl_approved", pending.run_id)

    await _delete_run(session, pending.run_id)
    await ingest_run_event(session, pending)
    await session.commit()

    await ingest_run_event(session, approved)

    run = await session.scalar(
        select(AgentRun).where(AgentRun.run_id == approved.run_id)
    )
    assert run is not None
    assert approved.final_result is not None
    assert run.hitl_state == approved.hitl.state.value
    assert run.status == approved.status.value
    assert run.event_type == approved.event_type.value
    assert run.completed_at == approved.completed_at
    assert run.final_result_output == approved.final_result.output
    assert run.hitl_pending_action is None
    assert await _count_for_run(session, AgentRun, approved.run_id) == 1

    submitted_tool_call = await session.scalar(
        select(ToolCall.tool_call_id).where(
            ToolCall.run_id == approved.run_id,
            ToolCall.tool_call_id == "tool-gs08-submit",
        )
    )
    assert submitted_tool_call == "tool-gs08-submit"

    await _delete_run(session, approved.run_id)


async def test_hitl_pending_repost_after_approved_is_rejected(
    session: AsyncSession,
) -> None:
    pending = _fixture_with_run_id("hitl_pending", "task6-reject-pending-regression")
    approved = _fixture_with_run_id("hitl_approved", pending.run_id)

    await _delete_run(session, pending.run_id)
    await ingest_run_event(session, approved)
    await session.commit()

    with pytest.raises(HITLStateRegressionError):
        await ingest_run_event(session, pending)

    hitl_state = await session.scalar(
        select(AgentRun.hitl_state).where(AgentRun.run_id == approved.run_id)
    )
    assert hitl_state == "approved"

    await _delete_run(session, approved.run_id)


@pytest.mark.parametrize("fixture_name", CANONICAL_FIXTURES)
async def test_usage_totals_are_derived_from_persisted_granular_calls(
    session: AsyncSession,
    fixture_name: str,
) -> None:
    event = _fixture_with_run_id(fixture_name, f"task7-usage-{fixture_name}")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)

    assert await _stored_usage_totals(session, event.run_id) == (
        await _derived_usage_totals(session, event.run_id)
    )

    await _delete_run(session, event.run_id)


async def test_usage_totals_ignore_incorrect_producer_reported_summary(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task7-ignore-producer-usage")
    event.usage.total_llm_calls = 999
    event.usage.total_tool_calls = 999
    event.usage.total_tokens = 999
    event.usage.total_retries = 999
    event.usage.total_estimated_cost_usd = 999.0

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)

    stored_totals = await _stored_usage_totals(session, event.run_id)
    assert stored_totals == await _derived_usage_totals(session, event.run_id)
    assert stored_totals != {
        "usage_total_llm_calls": event.usage.total_llm_calls,
        "usage_total_tool_calls": event.usage.total_tool_calls,
        "usage_total_tokens": event.usage.total_tokens,
        "usage_total_retries": event.usage.total_retries,
        "usage_total_estimated_cost_usd": event.usage.total_estimated_cost_usd,
    }

    await _delete_run(session, event.run_id)


async def test_tool_failure_usage_totals_are_not_null(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("tool_failure", "task7-tool-failure-usage")

    await _delete_run(session, event.run_id)
    await ingest_run_event(session, event)

    stored_totals = await _stored_usage_totals(session, event.run_id)
    assert stored_totals == await _derived_usage_totals(session, event.run_id)
    assert stored_totals["usage_total_tokens"] is not None
    assert stored_totals["usage_total_estimated_cost_usd"] is not None

    await _delete_run(session, event.run_id)


def test_agent_runs_do_not_store_latency_average_or_percentile_columns() -> None:
    column_names = set(AgentRun.__table__.columns.keys())

    assert not any(
        ("latency" in column_name and "avg" in column_name)
        or "percentile" in column_name
        or "p95" in column_name
        for column_name in column_names
    )


def _fixture_with_run_id(name: str, run_id: str) -> ExtendedRunEvent:
    event = load_fixture(name)
    event.run_id = run_id
    return event


async def _count_for_run(
    session: AsyncSession,
    model: type[AgentRun] | type[Span] | type[ToolCall] | type[LLMCall],
    run_id: str,
) -> int:
    count = await session.scalar(select(func.count()).where(model.run_id == run_id))
    return cast(int, count)


async def _delete_run(session: AsyncSession, run_id: str) -> None:
    for model in (LLMCall, ToolCall, Span, AgentRun):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.commit()


async def _entity_counts(session: AsyncSession, run_id: str) -> dict[str, int]:
    return {
        "agent_runs": await _count_for_run(session, AgentRun, run_id),
        "spans": await _count_for_run(session, Span, run_id),
        "tool_calls": await _count_for_run(session, ToolCall, run_id),
        "llm_calls": await _count_for_run(session, LLMCall, run_id),
    }


async def _run_updated_at(session: AsyncSession, run_id: str) -> datetime:
    updated_at = await session.scalar(
        select(AgentRun.updated_at).where(AgentRun.run_id == run_id)
    )
    return cast(datetime, updated_at)


async def _run_ingested_at(session: AsyncSession, run_id: str) -> datetime:
    ingested_at = await session.scalar(
        select(AgentRun.ingested_at).where(AgentRun.run_id == run_id)
    )
    return cast(datetime, ingested_at)


async def _span_internal_ids(session: AsyncSession, run_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(Span.span_id, Span.id).where(Span.run_id == run_id)
    )
    return {span_id: internal_id for span_id, internal_id in rows}


async def _tool_span_ids(session: AsyncSession, run_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(ToolCall.tool_call_id, ToolCall.span_id).where(ToolCall.run_id == run_id)
    )
    return {tool_call_id: span_id for tool_call_id, span_id in rows}


async def _llm_span_ids(session: AsyncSession, run_id: str) -> dict[str, int]:
    rows = await session.execute(
        select(LLMCall.llm_call_id, LLMCall.span_id).where(LLMCall.run_id == run_id)
    )
    return {llm_call_id: span_id for llm_call_id, span_id in rows}


async def _stored_usage_totals(
    session: AsyncSession,
    run_id: str,
) -> dict[str, int | float]:
    row = (
        await session.execute(
            select(
                AgentRun.usage_total_llm_calls,
                AgentRun.usage_total_tool_calls,
                AgentRun.usage_total_tokens,
                AgentRun.usage_total_retries,
                AgentRun.usage_total_estimated_cost_usd,
            ).where(AgentRun.run_id == run_id)
        )
    ).one()
    return {
        "usage_total_llm_calls": row.usage_total_llm_calls,
        "usage_total_tool_calls": row.usage_total_tool_calls,
        "usage_total_tokens": row.usage_total_tokens,
        "usage_total_retries": row.usage_total_retries,
        "usage_total_estimated_cost_usd": row.usage_total_estimated_cost_usd,
    }


async def _derived_usage_totals(
    session: AsyncSession,
    run_id: str,
) -> dict[str, int | float]:
    llm_row = (
        await session.execute(
            select(
                func.count(LLMCall.llm_call_id).label("llm_calls"),
                func.coalesce(func.sum(LLMCall.total_tokens), 0).label("tokens"),
                func.coalesce(func.sum(LLMCall.estimated_cost_usd), 0).label("cost"),
            ).where(LLMCall.run_id == run_id)
        )
    ).one()
    tool_row = (
        await session.execute(
            select(
                func.count(ToolCall.tool_call_id).label("tool_calls"),
                func.coalesce(func.sum(ToolCall.retry_count), 0).label("retries"),
            ).where(ToolCall.run_id == run_id)
        )
    ).one()
    return {
        "usage_total_llm_calls": llm_row.llm_calls,
        "usage_total_tool_calls": tool_row.tool_calls,
        "usage_total_tokens": llm_row.tokens,
        "usage_total_retries": tool_row.retries,
        "usage_total_estimated_cost_usd": llm_row.cost,
    }
