from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture
from obs_platform.telemetry.v1.enums import RunStatus


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def query_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def test_get_runs_defaults_to_twenty_most_recent_with_total_count(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    run_ids = [f"phase3-list-{index:02d}" for index in range(21)]
    await _delete_runs(session, run_ids)
    base_started_at = datetime(2035, 1, 1, tzinfo=UTC)

    for index, run_id in enumerate(run_ids):
        event = _fixture_with_run_id("healthy_success", run_id)
        event.started_at = base_started_at + timedelta(minutes=index)
        event.scenario_id = "phase3-list-default"
        await ingest_run_event(session, event)

    response = await query_client.get("/v1/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["total"] == await _count_runs(session)
    assert [item["run_id"] for item in body["items"][:20]] == list(
        reversed(run_ids[1:21])
    )
    assert _started_at_values(body) == sorted(_started_at_values(body), reverse=True)

    await _delete_runs(session, run_ids)


async def test_get_runs_filters_independently_and_combines_with_and_semantics(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    events = [
        _custom_event(
            "healthy_success",
            run_id="phase3-filter-success-target",
            started_at=datetime(2036, 1, 1, 12, 0, tzinfo=UTC),
            scenario_id="phase3-scenario-target",
            agent_version="phase3-agent-target",
            model="phase3-model-target",
        ),
        _custom_event(
            "tool_failure",
            run_id="phase3-filter-tool-error",
            started_at=datetime(2036, 1, 1, 12, 10, tzinfo=UTC),
            scenario_id="phase3-scenario-other",
            agent_version="phase3-agent-other",
            model="phase3-model-other",
        ),
        _custom_event(
            "hitl_pending",
            run_id="phase3-filter-awaiting",
            started_at=datetime(2036, 1, 1, 12, 20, tzinfo=UTC),
            scenario_id="phase3-scenario-target",
            agent_version="phase3-agent-other",
            model="phase3-model-other",
        ),
    ]
    await _delete_runs(session, [event.run_id for event in events])
    for event in events:
        await ingest_run_event(session, event)

    cases = [
        ({"status": RunStatus.TOOL_ERROR.value}, ["phase3-filter-tool-error"]),
        (
            {"scenario_id": "phase3-scenario-target"},
            ["phase3-filter-awaiting", "phase3-filter-success-target"],
        ),
        ({"agent_version": "phase3-agent-target"}, ["phase3-filter-success-target"]),
        ({"model": "phase3-model-target"}, ["phase3-filter-success-target"]),
        (
            {"started_after": "2036-01-01T12:05:00Z"},
            ["phase3-filter-awaiting", "phase3-filter-tool-error"],
        ),
        (
            {"started_before": "2036-01-01T12:15:00Z"},
            ["phase3-filter-tool-error", "phase3-filter-success-target"],
        ),
        (
            {
                "started_after": "2036-01-01T12:05:00Z",
                "started_before": "2036-01-01T12:25:00Z",
            },
            ["phase3-filter-awaiting", "phase3-filter-tool-error"],
        ),
        (
            {
                "scenario_id": "phase3-scenario-target",
                "agent_version": "phase3-agent-target",
            },
            ["phase3-filter-success-target"],
        ),
    ]

    for params, expected_run_ids in cases:
        response = await query_client.get("/v1/runs", params=params)
        assert response.status_code == 200
        body = response.json()
        assert [item["run_id"] for item in body["items"]] == expected_run_ids
        assert body["total"] == len(expected_run_ids)

    await _delete_runs(session, [event.run_id for event in events])


async def test_get_runs_model_filter_matches_llm_call_rows(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    event = _custom_event(
        "healthy_success",
        run_id="phase3-model-filter",
        started_at=datetime(2037, 1, 1, tzinfo=UTC),
        scenario_id="phase3-model-filter",
        agent_version="phase3-model-filter",
        model="phase3-model-from-llm-call",
    )
    await _delete_runs(session, [event.run_id])
    await ingest_run_event(session, event)

    assert not hasattr(AgentRun, "model")

    response = await query_client.get(
        "/v1/runs",
        params={"model": "phase3-model-from-llm-call"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["run_id"] for item in body["items"]] == [event.run_id]
    assert body["total"] == 1

    await _delete_runs(session, [event.run_id])


async def test_get_runs_rejects_over_limit_and_invalid_status(
    query_client: AsyncClient,
) -> None:
    over_limit = await query_client.get("/v1/runs", params={"limit": 101})
    invalid_status = await query_client.get(
        "/v1/runs",
        params={"status": "not-a-real-status"},
    )

    assert over_limit.status_code == 422
    assert invalid_status.status_code == 422


async def test_get_runs_response_items_are_lightweight_summaries(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    event = _custom_event(
        "healthy_success",
        run_id="phase3-summary-shape",
        started_at=datetime(2038, 1, 1, tzinfo=UTC),
        scenario_id="phase3-summary-shape",
        agent_version="phase3-summary-shape",
        model="phase3-summary-shape",
    )
    await _delete_runs(session, [event.run_id])
    await ingest_run_event(session, event)

    response = await query_client.get(
        "/v1/runs",
        params={"scenario_id": "phase3-summary-shape"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert set(item) == {
        "run_id",
        "scenario_id",
        "agent_name",
        "agent_version",
        "prompt_version",
        "environment",
        "status",
        "event_type",
        "hitl_state",
        "started_at",
        "completed_at",
        "execution_latency_ms",
        "wall_clock_duration_ms",
        "usage_total_tokens",
        "usage_total_estimated_cost_usd",
    }
    assert "spans" not in item
    assert "tool_calls" not in item
    assert "llm_calls" not in item
    assert "raw_input" not in item
    assert "normalized_input" not in item
    assert "final_result" not in item

    await _delete_runs(session, [event.run_id])


def _fixture_with_run_id(name: str, run_id: str) -> ExtendedRunEvent:
    event = load_fixture(name)
    event.run_id = run_id
    return event


def _custom_event(
    fixture_name: str,
    *,
    run_id: str,
    started_at: datetime,
    scenario_id: str,
    agent_version: str,
    model: str,
) -> ExtendedRunEvent:
    event = _fixture_with_run_id(fixture_name, run_id)
    event.started_at = started_at
    event.scenario_id = scenario_id
    event.agent_version = agent_version
    for llm_call in event.llm_calls:
        llm_call.model = model
    return event


async def _count_runs(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(AgentRun))
    return cast(int, count)


async def _delete_runs(session: AsyncSession, run_ids: list[str]) -> None:
    for model in (LLMCall, ToolCall, Span, AgentRun):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()


def _started_at_values(body: dict[str, object]) -> list[str]:
    items = cast(list[dict[str, object]], body["items"])
    return [cast(str, item["started_at"]) for item in items]
