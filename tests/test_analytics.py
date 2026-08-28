from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import analytics, runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def analytics_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app = create_app()
    app.dependency_overrides[analytics.get_session] = get_session
    app.dependency_overrides[runs.get_session] = get_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def test_overview_analytics_aggregates_all_runs_without_time_range(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase3-overview-all-success",
        "phase3-overview-all-tool-error",
        "phase3-overview-all-pending",
    ]
    await _delete_runs(session, run_ids)
    events = [
        _overview_event(
            "healthy_success",
            run_ids[0],
            started_at=datetime(2039, 1, 1, tzinfo=UTC),
        ),
        _overview_event(
            "tool_failure",
            run_ids[1],
            started_at=datetime(2039, 1, 2, tzinfo=UTC),
        ),
        _overview_event(
            "hitl_pending",
            run_ids[2],
            started_at=datetime(2039, 1, 3, tzinfo=UTC),
        ),
    ]
    for event in events:
        await ingest_run_event(session, event)

    response = await analytics_client.get("/v1/analytics/overview")

    assert response.status_code == 200
    body = response.json()
    expected = await _expected_overview(session)
    assert body == expected

    await _delete_runs(session, run_ids)


async def test_overview_analytics_time_range_scopes_to_matching_runs(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase3-overview-range-before",
        "phase3-overview-range-inside",
        "phase3-overview-range-after",
    ]
    await _delete_runs(session, run_ids)
    events = [
        _overview_event(
            "healthy_success",
            run_ids[0],
            started_at=datetime(2040, 1, 1, tzinfo=UTC),
        ),
        _overview_event(
            "tool_failure",
            run_ids[1],
            started_at=datetime(2040, 1, 2, tzinfo=UTC),
        ),
        _overview_event(
            "trajectory_error",
            run_ids[2],
            started_at=datetime(2040, 1, 3, tzinfo=UTC),
        ),
    ]
    for event in events:
        await ingest_run_event(session, event)

    response = await analytics_client.get(
        "/v1/analytics/overview",
        params={
            "started_after": "2040-01-02T00:00:00Z",
            "started_before": "2040-01-02T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    expected = await _expected_overview(
        session,
        started_after=datetime(2040, 1, 2, tzinfo=UTC),
        started_before=datetime(2040, 1, 2, 23, 59, 59, tzinfo=UTC),
    )
    assert body == expected
    assert body["run_counts"]["total"] == 1
    assert body["run_counts"]["by_status"]["tool_error"] == 1

    await _delete_runs(session, run_ids)


async def test_overview_analytics_pending_excluded_from_success_rate_denominator(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase3-overview-denominator-success",
        "phase3-overview-denominator-pending",
    ]
    await _delete_runs(session, run_ids)
    for event in [
        _overview_event(
            "healthy_success",
            run_ids[0],
            started_at=datetime(2041, 1, 1, tzinfo=UTC),
        ),
        _overview_event(
            "hitl_pending",
            run_ids[1],
            started_at=datetime(2041, 1, 2, tzinfo=UTC),
        ),
    ]:
        await ingest_run_event(session, event)

    response = await analytics_client.get(
        "/v1/analytics/overview",
        params={
            "started_after": "2041-01-01T00:00:00Z",
            "started_before": "2041-01-02T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_success_rate"] == 1.0
    assert body["run_counts"]["total"] == 2
    assert body["run_counts"]["by_status"]["awaiting_approval"] == 1

    await _delete_runs(session, run_ids)


async def test_overview_analytics_latency_uses_execution_not_wall_clock(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase3-overview-latency-low",
        "phase3-overview-latency-mid",
        "phase3-overview-latency-high",
    ]
    await _delete_runs(session, run_ids)
    for run_id, day, latency, wall_clock in [
        (run_ids[0], 1, 100, 10_000),
        (run_ids[1], 2, 200, 20_000),
        (run_ids[2], 3, 10_000, 300),
    ]:
        event = _overview_event(
            "healthy_success",
            run_id,
            started_at=datetime(2042, 1, day, tzinfo=UTC),
        )
        await ingest_run_event(session, event)
        await session.execute(
            update(AgentRun)
            .where(AgentRun.run_id == run_id)
            .values(
                execution_latency_ms=latency,
                wall_clock_duration_ms=wall_clock,
            )
        )
        await session.commit()

    response = await analytics_client.get(
        "/v1/analytics/overview",
        params={
            "started_after": "2042-01-01T00:00:00Z",
            "started_before": "2042-01-31T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["avg_latency_ms"] == pytest.approx((100 + 200 + 10_000) / 3)
    assert body["p95_latency_ms"] == pytest.approx(9_020)
    assert body["avg_latency_ms"] != pytest.approx((10_000 + 20_000 + 300) / 3)

    await _delete_runs(session, run_ids)


async def test_overview_analytics_openapi_exposes_only_time_range_params(
    analytics_client: AsyncClient,
) -> None:
    response = await analytics_client.get("/openapi.json")

    assert response.status_code == 200
    params = response.json()["paths"]["/v1/analytics/overview"]["get"]["parameters"]
    assert {param["name"] for param in params} == {
        "started_after",
        "started_before",
    }


def _fixture_with_run_id(name: str, run_id: str) -> ExtendedRunEvent:
    event = load_fixture(name)
    event.run_id = run_id
    return event


def _overview_event(
    fixture_name: str,
    run_id: str,
    *,
    started_at: datetime,
) -> ExtendedRunEvent:
    event = _fixture_with_run_id(fixture_name, run_id)
    event.started_at = started_at
    event.agent_version = "phase3-overview"
    return event


async def _delete_runs(session: AsyncSession, run_ids: list[str]) -> None:
    for model in (LLMCall, ToolCall, Span, AgentRun):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()


async def _expected_overview(
    session: AsyncSession,
    *,
    started_after: datetime | None = None,
    started_before: datetime | None = None,
) -> dict[str, object]:
    filters = []
    if started_after is not None:
        filters.append(AgentRun.started_at >= started_after)
    if started_before is not None:
        filters.append(AgentRun.started_at <= started_before)
    rows = (await session.scalars(select(AgentRun).where(*filters))).all()
    latencies = sorted(
        row.execution_latency_ms for row in rows if row.execution_latency_ms is not None
    )
    terminal = [row for row in rows if row.event_type == "run_final"]
    success_count = sum(1 for row in terminal if row.status == "success")
    return {
        "runtime_success_rate": (
            success_count / len(terminal) if len(terminal) > 0 else None
        ),
        "avg_latency_ms": (
            sum(latencies) / len(latencies) if len(latencies) > 0 else None
        ),
        "p95_latency_ms": _percentile_cont(latencies, 0.95),
        "usage_total_tokens": sum(row.usage_total_tokens for row in rows),
        "usage_total_estimated_cost_usd": sum(
            row.usage_total_estimated_cost_usd for row in rows
        ),
        "run_counts": {
            "total": len(rows),
            "by_status": {
                "success": sum(1 for row in rows if row.status == "success"),
                "tool_error": sum(1 for row in rows if row.status == "tool_error"),
                "runtime_error": sum(
                    1 for row in rows if row.status == "runtime_error"
                ),
                "awaiting_approval": sum(
                    1 for row in rows if row.status == "awaiting_approval"
                ),
            },
        },
    }


def _percentile_cont(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    index = (len(values) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return float(values[lower])
    fraction = index - lower
    return values[lower] + (values[upper] - values[lower]) * fraction
