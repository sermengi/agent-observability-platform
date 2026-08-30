from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, RunFailure, Span, ToolCall
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import analytics, runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture
from obs_platform.telemetry.v1.enums import ExecutionStatus, LLMCallType


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
    assert body.pop("usage_total_estimated_cost_usd") == pytest.approx(
        expected.pop("usage_total_estimated_cost_usd")
    )
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


async def test_tool_analytics_returns_one_row_per_present_tool_with_counts(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_id = "phase3-tools-counts"
    await _seed_tool_analytics_run(
        session,
        run_id,
        started_at=datetime(2044, 1, 1, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/tools",
        params={
            "started_after": "2044-01-01T00:00:00Z",
            "started_before": "2044-01-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["tool_name"] for item in body["items"]] == [
        "alpha_tool",
        "beta_tool",
        "gamma_tool",
    ]
    assert body["items"][0] == {
        "tool_name": "alpha_tool",
        "call_count": 3,
        "success_count": 1,
        "failure_count": 1,
        "error_count": 1,
        "failure_rate": 2 / 3,
        "avg_latency_ms": 200.0,
        "p95_latency_ms": 290.0,
    }
    assert "absent_tool" not in {item["tool_name"] for item in body["items"]}

    await _delete_runs(session, [run_id])


async def test_tool_analytics_time_range_excludes_out_of_scope_tool_names(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    in_scope_run_id = "phase3-tools-in-scope"
    out_of_scope_run_id = "phase3-tools-out-of-scope"
    await _delete_runs(session, [in_scope_run_id, out_of_scope_run_id])
    await _seed_tool_analytics_run(
        session,
        in_scope_run_id,
        started_at=datetime(2044, 2, 1, tzinfo=UTC),
    )
    await _seed_tool_analytics_run(
        session,
        out_of_scope_run_id,
        started_at=datetime(2044, 2, 2, tzinfo=UTC),
        tool_prefix="outside",
    )

    response = await analytics_client.get(
        "/v1/analytics/tools",
        params={
            "started_after": "2044-02-01T00:00:00Z",
            "started_before": "2044-02-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    tool_names = {item["tool_name"] for item in response.json()["items"]}
    assert tool_names == {"alpha_tool", "beta_tool", "gamma_tool"}
    assert "outside_alpha_tool" not in tool_names

    await _delete_runs(session, [in_scope_run_id, out_of_scope_run_id])


async def test_tool_analytics_openapi_exposes_only_time_range_params(
    analytics_client: AsyncClient,
) -> None:
    response = await analytics_client.get("/openapi.json")

    assert response.status_code == 200
    params = response.json()["paths"]["/v1/analytics/tools"]["get"]["parameters"]
    assert {param["name"] for param in params} == {
        "started_after",
        "started_before",
    }


async def test_usage_analytics_returns_totals_model_and_call_type_breakdowns(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_id = "phase3-usage-breakdowns"
    await _seed_usage_analytics_run(
        session,
        run_id,
        started_at=datetime(2045, 1, 1, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/usage",
        params={
            "started_after": "2045-01-01T00:00:00Z",
            "started_before": "2045-01-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == {
        "call_count": 4,
        "prompt_tokens": 42,
        "completion_tokens": 18,
        "total_tokens": 60,
        "total_estimated_cost_usd": pytest.approx(0.95),
    }
    assert sum(item["total_tokens"] for item in body["by_model"]) == 60
    assert sum(item["total_tokens"] for item in body["by_call_type"]) == 60

    await _delete_runs(session, [run_id])


async def test_usage_analytics_groups_by_provider_model_pair(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_id = "phase3-usage-provider-model"
    await _seed_usage_analytics_run(
        session,
        run_id,
        started_at=datetime(2045, 2, 1, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/usage",
        params={
            "started_after": "2045-02-01T00:00:00Z",
            "started_before": "2045-02-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    by_model = response.json()["by_model"]
    assert [
        (item["provider"], item["model"], item["call_count"]) for item in by_model
    ] == [
        ("openai", "premium-model", 1),
        ("openai", "shared-model", 2),
        ("anthropic", "shared-model", 1),
    ]

    await _delete_runs(session, [run_id])


async def test_usage_analytics_call_type_breakdown_only_contains_present_values(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_id = "phase3-usage-call-types"
    await _seed_usage_analytics_run(
        session,
        run_id,
        started_at=datetime(2045, 3, 1, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/usage",
        params={
            "started_after": "2045-03-01T00:00:00Z",
            "started_before": "2045-03-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    by_call_type = response.json()["by_call_type"]
    assert [item["call_type"] for item in by_call_type] == [
        "synthesis",
        "interpretation",
    ]
    assert "evidence_gathering" not in {item["call_type"] for item in by_call_type}

    await _delete_runs(session, [run_id])


async def test_usage_analytics_time_range_scopes_llm_calls_by_run_started_at(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    in_scope_run_id = "phase3-usage-in-scope"
    out_of_scope_run_id = "phase3-usage-out-of-scope"
    await _seed_usage_analytics_run(
        session,
        in_scope_run_id,
        started_at=datetime(2045, 4, 1, tzinfo=UTC),
    )
    await _seed_usage_analytics_run(
        session,
        out_of_scope_run_id,
        started_at=datetime(2045, 4, 2, tzinfo=UTC),
        provider_prefix="outside",
    )

    response = await analytics_client.get(
        "/v1/analytics/usage",
        params={
            "started_after": "2045-04-01T00:00:00Z",
            "started_before": "2045-04-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"]["call_count"] == 4
    assert {item["provider"] for item in body["by_model"]} == {
        "anthropic",
        "openai",
    }

    await _delete_runs(session, [in_scope_run_id, out_of_scope_run_id])


async def test_usage_analytics_response_shape_has_no_latency_fields(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_id = "phase3-usage-no-latency"
    await _seed_usage_analytics_run(
        session,
        run_id,
        started_at=datetime(2045, 5, 1, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/usage",
        params={
            "started_after": "2045-05-01T00:00:00Z",
            "started_before": "2045-05-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    assert not _contains_key_fragment(response.json(), "latency")

    await _delete_runs(session, [run_id])


async def test_failure_analytics_counts_evaluated_runs_by_overall_status(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase5-failures-pass",
        "phase5-failures-policy",
        "phase5-failures-tool",
        "phase5-failures-incomplete",
        "phase5-failures-never-evaluated",
    ]
    await _delete_runs(session, run_ids)
    await _seed_failure_analytics_run(
        session,
        run_ids[0],
        started_at=datetime(2046, 1, 1, tzinfo=UTC),
        overall_status="pass",
        primary_category=None,
        max_severity=None,
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[1],
        started_at=datetime(2046, 1, 2, tzinfo=UTC),
        overall_status="fail",
        primary_category="policy_violation",
        max_severity="critical",
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[2],
        started_at=datetime(2046, 1, 3, tzinfo=UTC),
        overall_status="fail",
        primary_category="tool_failure",
        max_severity="error",
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[3],
        started_at=datetime(2046, 1, 4, tzinfo=UTC),
        overall_status="incomplete",
        primary_category=None,
        max_severity=None,
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[4],
        started_at=datetime(2046, 1, 5, tzinfo=UTC),
    )

    response = await analytics_client.get(
        "/v1/analytics/failures",
        params={
            "started_after": "2046-01-01T00:00:00Z",
            "started_before": "2046-01-31T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_counts"] == {
        "total": 4,
        "by_overall_status": {
            "pass": 1,
            "fail": 2,
            "incomplete": 1,
        },
    }

    await _delete_runs(session, run_ids)


async def test_failure_analytics_breakdowns_and_percentages(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    run_ids = [
        "phase5-failures-breakdown-policy",
        "phase5-failures-breakdown-tool",
        "phase5-failures-breakdown-trajectory",
        "phase5-failures-breakdown-pass",
        "phase5-failures-breakdown-incomplete",
        "phase5-failures-breakdown-unassigned",
    ]
    await _delete_runs(session, run_ids)
    await _seed_failure_analytics_run(
        session,
        run_ids[0],
        started_at=datetime(2047, 1, 1, tzinfo=UTC),
        overall_status="fail",
        primary_category="policy_violation",
        max_severity="critical",
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[1],
        started_at=datetime(2047, 1, 2, tzinfo=UTC),
        overall_status="fail",
        primary_category="tool_failure",
        max_severity="error",
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[2],
        started_at=datetime(2047, 1, 3, tzinfo=UTC),
        overall_status="fail",
        primary_category="trajectory_error",
        max_severity="error",
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[3],
        started_at=datetime(2047, 1, 4, tzinfo=UTC),
        overall_status="pass",
        primary_category=None,
        max_severity=None,
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[4],
        started_at=datetime(2047, 1, 5, tzinfo=UTC),
        overall_status="incomplete",
        primary_category=None,
        max_severity=None,
    )
    await _seed_failure_analytics_run(
        session,
        run_ids[5],
        started_at=datetime(2047, 1, 6, tzinfo=UTC),
        overall_status="fail",
        primary_category="unknown",
        max_severity=None,
    )

    response = await analytics_client.get(
        "/v1/analytics/failures",
        params={
            "started_after": "2047-01-01T00:00:00Z",
            "started_before": "2047-01-31T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    by_failure_type = {
        item["failure_type"]: item for item in body["by_failure_type"]
    }
    assert set(by_failure_type) == {
        "policy_violation",
        "tool_failure",
        "trajectory_error",
        "unknown",
    }
    assert sum(item["count"] for item in body["by_failure_type"]) == 4
    assert by_failure_type["policy_violation"] == {
        "failure_type": "policy_violation",
        "count": 1,
        "pct_of_evaluated": pytest.approx(1 / 6),
        "pct_of_failing": pytest.approx(1 / 5),
    }
    by_severity = {item["severity"]: item for item in body["by_severity"]}
    assert by_severity == {
        "critical": {
            "severity": "critical",
            "count": 1,
            "pct_of_evaluated": pytest.approx(1 / 6),
            "pct_of_failing": pytest.approx(1 / 5),
        },
        "error": {
            "severity": "error",
            "count": 2,
            "pct_of_evaluated": pytest.approx(2 / 6),
            "pct_of_failing": pytest.approx(2 / 5),
        },
    }
    assert "unassigned" not in by_severity

    await _delete_runs(session, run_ids)


async def test_failure_analytics_time_range_scopes_by_run_started_at(
    session: AsyncSession,
    analytics_client: AsyncClient,
) -> None:
    in_scope_run_id = "phase5-failures-range-in"
    out_of_scope_run_id = "phase5-failures-range-out"
    await _delete_runs(session, [in_scope_run_id, out_of_scope_run_id])
    await _seed_failure_analytics_run(
        session,
        in_scope_run_id,
        started_at=datetime(2048, 1, 1, tzinfo=UTC),
        overall_status="fail",
        primary_category="policy_violation",
        max_severity="critical",
    )
    await _seed_failure_analytics_run(
        session,
        out_of_scope_run_id,
        started_at=datetime(2048, 1, 2, tzinfo=UTC),
        overall_status="fail",
        primary_category="tool_failure",
        max_severity="error",
    )

    response = await analytics_client.get(
        "/v1/analytics/failures",
        params={
            "started_after": "2048-01-01T00:00:00Z",
            "started_before": "2048-01-01T23:59:59Z",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_counts"]["total"] == 1
    assert [item["failure_type"] for item in body["by_failure_type"]] == [
        "policy_violation"
    ]

    await _delete_runs(session, [in_scope_run_id, out_of_scope_run_id])


async def test_failure_analytics_openapi_exposes_only_time_range_params(
    analytics_client: AsyncClient,
) -> None:
    response = await analytics_client.get("/openapi.json")

    assert response.status_code == 200
    params = response.json()["paths"]["/v1/analytics/failures"]["get"]["parameters"]
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
    for model in (RunFailure, LLMCall, ToolCall, Span, AgentRun):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()


async def _seed_failure_analytics_run(
    session: AsyncSession,
    run_id: str,
    *,
    started_at: datetime,
    overall_status: str | None = None,
    primary_category: str | None = None,
    max_severity: str | None = None,
) -> None:
    event = _overview_event("healthy_success", run_id, started_at=started_at)
    await ingest_run_event(session, event)
    if overall_status is not None:
        session.add(
            RunFailure(
                run_id=run_id,
                overall_status=overall_status,
                primary_category=primary_category,
                secondary_category="retrieval_failure",
                max_severity=max_severity,
                classifier_version="1.0.0",
                updated_at=started_at,
            )
        )
        await session.commit()


async def _seed_tool_analytics_run(
    session: AsyncSession,
    run_id: str,
    *,
    started_at: datetime = datetime(2043, 1, 1, tzinfo=UTC),
    tool_prefix: str = "",
) -> None:
    event = _overview_event("healthy_success", run_id, started_at=started_at)
    await _delete_runs(session, [run_id])
    await ingest_run_event(session, event)
    span_id = await session.scalar(select(Span.id).where(Span.run_id == run_id))
    assert span_id is not None
    await session.execute(delete(ToolCall).where(ToolCall.run_id == run_id))
    for tool_call_id, tool_name, status, latency_ms in [
        ("alpha-success", "alpha_tool", ExecutionStatus.SUCCESS, 100),
        ("alpha-failure", "alpha_tool", ExecutionStatus.FAILURE, 200),
        ("alpha-error", "alpha_tool", ExecutionStatus.ERROR, 300),
        ("beta-success-1", "beta_tool", ExecutionStatus.SUCCESS, 50),
        ("beta-success-2", "beta_tool", ExecutionStatus.SUCCESS, 150),
        ("gamma-success", "gamma_tool", ExecutionStatus.SUCCESS, None),
    ]:
        session.add(
            ToolCall(
                run_id=run_id,
                tool_call_id=f"{run_id}-{tool_call_id}",
                span_id=span_id,
                tool_name=f"{tool_prefix}_{tool_name}" if tool_prefix else tool_name,
                sequence=len(session.new) + 1,
                arguments={"source": "test"},
                result={"ok": status is ExecutionStatus.SUCCESS},
                started_at=started_at,
                completed_at=started_at,
                latency_ms=latency_ms,
                retry_count=0,
                status=status.value,
            )
        )
    await session.commit()


async def _seed_usage_analytics_run(
    session: AsyncSession,
    run_id: str,
    *,
    started_at: datetime,
    provider_prefix: str = "",
) -> None:
    event = _overview_event("healthy_success", run_id, started_at=started_at)
    await _delete_runs(session, [run_id])
    await ingest_run_event(session, event)
    span_id = await session.scalar(select(Span.id).where(Span.run_id == run_id))
    assert span_id is not None
    await session.execute(delete(LLMCall).where(LLMCall.run_id == run_id))
    for (
        llm_call_id,
        provider,
        model,
        call_type,
        prompt_tokens,
        completion_tokens,
        estimated_cost_usd,
    ) in [
        (
            "openai-shared-one",
            "openai",
            "shared-model",
            LLMCallType.INTERPRETATION,
            10,
            5,
            0.10,
        ),
        (
            "openai-shared-two",
            "openai",
            "shared-model",
            LLMCallType.INTERPRETATION,
            12,
            6,
            0.20,
        ),
        (
            "anthropic-shared",
            "anthropic",
            "shared-model",
            LLMCallType.INTERPRETATION,
            8,
            2,
            0.15,
        ),
        (
            "openai-premium",
            "openai",
            "premium-model",
            LLMCallType.SYNTHESIS,
            12,
            5,
            0.50,
        ),
    ]:
        session.add(
            LLMCall(
                run_id=run_id,
                llm_call_id=f"{run_id}-{llm_call_id}",
                span_id=span_id,
                call_type=call_type.value,
                model=model,
                provider=(
                    f"{provider_prefix}_{provider}" if provider_prefix else provider
                ),
                started_at=started_at,
                completed_at=started_at,
                latency_ms=999,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_usd=estimated_cost_usd,
                input_payload={"source": "test"},
                output_payload={"ok": True},
                status=ExecutionStatus.SUCCESS.value,
            )
        )
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


def _contains_key_fragment(value: object, fragment: str) -> bool:
    if isinstance(value, dict):
        return any(
            fragment in key or _contains_key_fragment(child, fragment)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key_fragment(item, fragment) for item in value)
    return False
