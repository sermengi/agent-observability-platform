from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.api.v1.schemas import RunDetailResponse
from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, EvaluationResult, LLMCall, RunFailure, Span
from obs_platform.db.models import ToolCall as ToolCallRecord
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_all_fixtures, load_fixture
from obs_platform.telemetry.v1.enums import RunStatus
from obs_platform.telemetry.v1.models import ExtendedRunEvent as TelemetryRunEvent


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
    base_started_at = datetime(2100, 1, 1, tzinfo=UTC)

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
        "overall_status",
        "primary_failure_type",
        "max_severity",
    }
    assert item["overall_status"] is None
    assert item["primary_failure_type"] is None
    assert item["max_severity"] is None
    assert "spans" not in item
    assert "tool_calls" not in item
    assert "llm_calls" not in item
    assert "raw_input" not in item
    assert "normalized_input" not in item
    assert "final_result" not in item
    assert "failure" not in item
    assert "evaluation_summary" not in item

    await _delete_runs(session, [event.run_id])


async def test_get_runs_filters_by_evaluation_failure_snapshot(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    run_ids = [
        "phase5-list-eval-policy",
        "phase5-list-eval-tool",
        "phase5-list-eval-pass",
    ]
    await _delete_runs(session, run_ids)
    for run_id in run_ids:
        event = _custom_event(
            "healthy_success",
            run_id=run_id,
            started_at=datetime(2039, 1, 1, tzinfo=UTC),
            scenario_id="phase5-list-eval",
            agent_version="phase5-list-eval",
            model="phase5-list-eval",
        )
        await ingest_run_event(session, event)

    session.add_all(
        [
            RunFailure(
                run_id="phase5-list-eval-policy",
                overall_status="fail",
                primary_category="policy_violation",
                secondary_category=None,
                max_severity="critical",
                classifier_version="1.0.0",
                updated_at=datetime(2039, 1, 1, 1, tzinfo=UTC),
            ),
            RunFailure(
                run_id="phase5-list-eval-tool",
                overall_status="fail",
                primary_category="tool_failure",
                secondary_category=None,
                max_severity="error",
                classifier_version="1.0.0",
                updated_at=datetime(2039, 1, 1, 1, tzinfo=UTC),
            ),
            RunFailure(
                run_id="phase5-list-eval-pass",
                overall_status="pass",
                primary_category=None,
                secondary_category=None,
                max_severity=None,
                classifier_version="1.0.0",
                updated_at=datetime(2039, 1, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()

    fail_response = await query_client.get(
        "/v1/runs",
        params={"scenario_id": "phase5-list-eval", "overall_status": "fail"},
    )
    policy_response = await query_client.get(
        "/v1/runs",
        params={
            "scenario_id": "phase5-list-eval",
            "primary_failure_type": "policy_violation",
        },
    )
    combined_response = await query_client.get(
        "/v1/runs",
        params={
            "scenario_id": "phase5-list-eval",
            "overall_status": "fail",
            "primary_failure_type": "tool_failure",
        },
    )

    assert fail_response.status_code == 200
    assert {
        item["run_id"] for item in fail_response.json()["items"]
    } == {"phase5-list-eval-policy", "phase5-list-eval-tool"}
    assert policy_response.status_code == 200
    assert [item["run_id"] for item in policy_response.json()["items"]] == [
        "phase5-list-eval-policy"
    ]
    assert combined_response.status_code == 200
    assert [item["run_id"] for item in combined_response.json()["items"]] == [
        "phase5-list-eval-tool"
    ]

    await _delete_runs(session, run_ids)


async def test_get_run_detail_unknown_run_id_returns_404(
    query_client: AsyncClient,
) -> None:
    response = await query_client.get("/v1/runs/phase3-missing-run")

    assert response.status_code == 404


async def test_get_run_detail_returns_flat_sequence_ordered_trajectory(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    event = _fixture_with_run_id("hitl_approved", "phase3-detail-trajectory")
    await _delete_runs(session, [event.run_id])
    await ingest_run_event(session, event)

    response = await query_client.get(f"/v1/runs/{event.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert _sequence_values(body["spans"]) == sorted(_sequence_values(body["spans"]))
    assert _sequence_values(body["tool_calls"]) == sorted(
        _sequence_values(body["tool_calls"])
    )
    assert _sequence_values(body["llm_calls"]) == sorted(
        _sequence_values(body["llm_calls"])
    )
    assert not any("tool_calls" in span for span in body["spans"])
    assert not any("llm_calls" in span for span in body["spans"])
    assert all(
        isinstance(tool_call["span_id"], str) for tool_call in body["tool_calls"]
    )
    assert all(isinstance(llm_call["span_id"], str) for llm_call in body["llm_calls"])
    assert not _contains_key(body, "id")
    assert body["failure"] is None
    assert body["evaluation_summary"] is None

    await _delete_runs(session, [event.run_id])


async def test_get_run_detail_blocks_match_agent_run_columns(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    event = _fixture_with_run_id("tool_failure", "phase3-detail-blocks")
    await _delete_runs(session, [event.run_id])
    await ingest_run_event(session, event)
    row = (
        await session.execute(
            select(
                AgentRun.hitl_required,
                AgentRun.hitl_state,
                AgentRun.hitl_checkpoint_id,
                AgentRun.hitl_decision,
                AgentRun.hitl_requested_at,
                AgentRun.hitl_decided_at,
                AgentRun.hitl_pending_action,
                AgentRun.usage_total_llm_calls,
                AgentRun.usage_total_tool_calls,
                AgentRun.usage_total_tokens,
                AgentRun.usage_total_estimated_cost_usd,
                AgentRun.usage_total_retries,
                AgentRun.runtime_error_category,
                AgentRun.runtime_error_code,
                AgentRun.runtime_error_message,
                AgentRun.runtime_error_failed_component,
            ).where(AgentRun.run_id == event.run_id)
        )
    ).one()

    response = await query_client.get(f"/v1/runs/{event.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["hitl"] == {
        "required": row.hitl_required,
        "state": row.hitl_state,
        "checkpoint_id": row.hitl_checkpoint_id,
        "decision": row.hitl_decision,
        "requested_at": row.hitl_requested_at,
        "decided_at": row.hitl_decided_at,
        "pending_action": row.hitl_pending_action,
    }
    assert body["usage"] == {
        "total_llm_calls": row.usage_total_llm_calls,
        "total_tool_calls": row.usage_total_tool_calls,
        "total_tokens": row.usage_total_tokens,
        "total_estimated_cost_usd": row.usage_total_estimated_cost_usd,
        "total_retries": row.usage_total_retries,
    }
    assert body["runtime_error"] == {
        "category": row.runtime_error_category,
        "code": row.runtime_error_code,
        "message": row.runtime_error_message,
        "failed_component": row.runtime_error_failed_component,
    }

    await _delete_runs(session, [event.run_id])


async def test_run_detail_response_is_distinct_from_telemetry_contract() -> None:
    assert RunDetailResponse.__name__ != TelemetryRunEvent.__name__
    assert RunDetailResponse.__module__ == "obs_platform.api.v1.schemas"


async def test_get_run_detail_hitl_reingestion_preserves_ids_and_appends_tool_call(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    pending = _fixture_with_run_id("hitl_pending", "phase3-detail-hitl-reingestion")
    approved = _fixture_with_run_id("hitl_approved", pending.run_id)
    await _delete_runs(session, [pending.run_id])

    await ingest_run_event(session, pending)
    pending_response = await query_client.get(f"/v1/runs/{pending.run_id}")
    await session.commit()

    await ingest_run_event(session, approved)
    approved_response = await query_client.get(f"/v1/runs/{approved.run_id}")

    assert pending_response.status_code == 200
    assert approved_response.status_code == 200
    pending_body = pending_response.json()
    approved_body = approved_response.json()
    assert _ids_by_sequence(pending_body["spans"], "span_id") == [
        item
        for item in _ids_by_sequence(approved_body["spans"], "span_id")
        if item != "span-gs08-submit"
    ]
    assert _ids_by_sequence(pending_body["tool_calls"], "tool_call_id") == [
        item
        for item in _ids_by_sequence(approved_body["tool_calls"], "tool_call_id")
        if item != "tool-gs08-submit"
    ]
    assert _ids_by_sequence(pending_body["llm_calls"], "llm_call_id") == [
        item
        for item in _ids_by_sequence(approved_body["llm_calls"], "llm_call_id")
        if item != "llm-gs08-synthesis"
    ]
    assert _ids_by_sequence(approved_body["tool_calls"], "tool_call_id")[-1] == (
        "tool-gs08-submit"
    )
    assert _sequence_values(approved_body["tool_calls"]) == sorted(
        _sequence_values(approved_body["tool_calls"])
    )

    await _delete_runs(session, [pending.run_id])


async def test_get_run_detail_includes_failure_and_latest_evaluation_summary(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    event = _fixture_with_run_id("healthy_success", "phase5-detail-evaluation")
    await _delete_runs(session, [event.run_id])
    await ingest_run_event(session, event)
    session.add(
        RunFailure(
            run_id=event.run_id,
            overall_status="fail",
            primary_category="tool_failure",
            secondary_category="retrieval_failure",
            max_severity="error",
            classifier_version="1.0.0",
            updated_at=datetime(2040, 1, 1, tzinfo=UTC),
        )
    )
    session.add_all(
        [
            _evaluation_result(
                event.run_id,
                evaluator_name="tool_execution",
                created_at=datetime(2040, 1, 1, 0, 0, tzinfo=UTC),
                label="fail",
                passed=False,
                reason="old tool failure",
            ),
            _evaluation_result(
                event.run_id,
                evaluator_name="tool_execution",
                created_at=datetime(2040, 1, 1, 0, 1, tzinfo=UTC),
                label="pass",
                passed=True,
                reason="latest tool pass",
            ),
            _evaluation_result(
                event.run_id,
                evaluator_name="evidence",
                created_at=datetime(2040, 1, 1, 0, 0, tzinfo=UTC),
                label="fail",
                passed=False,
                reason="old evidence failure",
            ),
            _evaluation_result(
                event.run_id,
                evaluator_name="evidence",
                created_at=datetime(2040, 1, 1, 0, 1, tzinfo=UTC),
                label="fail",
                passed=False,
                reason="latest evidence failure",
            ),
        ]
    )
    await session.commit()

    response = await query_client.get(f"/v1/runs/{event.run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["failure"] == {
        "overall_status": "fail",
        "primary_failure_type": "tool_failure",
        "secondary_failure_type": "retrieval_failure",
        "max_severity": "error",
        "classifier_version": "1.0.0",
        "updated_at": "2040-01-01T00:00:00Z",
    }
    assert len(body["evaluation_summary"]) == 2
    by_name = {
        item["evaluator_name"]: item for item in body["evaluation_summary"]
    }
    assert by_name["tool_execution"]["label"] == "pass"
    assert by_name["tool_execution"]["reason"] == "latest tool pass"
    assert by_name["evidence"]["label"] == "fail"
    assert by_name["evidence"]["reason"] == "latest evidence failure"

    await _delete_runs(session, [event.run_id])


async def test_get_runs_and_detail_reconstruct_phase_1_fixture_corpus(
    session: AsyncSession,
    query_client: AsyncClient,
) -> None:
    events = load_all_fixtures()
    for fixture_name, event in events.items():
        event.run_id = f"phase3-corpus-{fixture_name}"
        event.agent_version = "phase3-corpus"
    run_ids = [event.run_id for event in events.values()]
    await _delete_runs(session, run_ids)
    for event in events.values():
        await ingest_run_event(session, event)

    list_response = await query_client.get(
        "/v1/runs",
        params={"agent_version": "phase3-corpus", "limit": len(run_ids)},
    )

    assert list_response.status_code == 200
    listed_run_ids = {item["run_id"] for item in list_response.json()["items"]}
    assert listed_run_ids == set(run_ids)

    for event in events.values():
        detail_response = await query_client.get(f"/v1/runs/{event.run_id}")

        assert detail_response.status_code == 200
        _assert_detail_reconstructs_event(detail_response.json(), event)

    await _delete_runs(session, run_ids)


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
    for model in (
        RunFailure,
        EvaluationResult,
        LLMCall,
        ToolCallRecord,
        Span,
        AgentRun,
    ):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()


def _started_at_values(body: dict[str, object]) -> list[str]:
    items = cast(list[dict[str, object]], body["items"])
    return [cast(str, item["started_at"]) for item in items]


def _sequence_values(items: list[dict[str, object]]) -> list[int]:
    return [cast(int, item["sequence"]) for item in items]


def _assert_detail_reconstructs_event(
    detail: dict[str, object],
    event: ExtendedRunEvent,
) -> None:
    expected = event.model_dump(mode="json")
    for field in [
        "run_id",
        "scenario_id",
        "agent_name",
        "agent_version",
        "prompt_version",
        "environment",
        "status",
        "event_type",
        "raw_input",
        "normalized_input",
        "started_at",
        "completed_at",
        "execution_latency_ms",
        "wall_clock_duration_ms",
        "resume_count",
        "spans",
        "tool_calls",
        "hitl",
        "usage",
        "final_result",
        "runtime_error",
    ]:
        assert detail[field] == expected[field]

    expected_llm_calls = sorted(
        expected["llm_calls"],
        key=lambda item: (item["started_at"], item["llm_call_id"]),
    )
    actual_llm_calls = [
        {key: value for key, value in item.items() if key != "sequence"}
        for item in cast(list[dict[str, object]], detail["llm_calls"])
    ]
    assert actual_llm_calls == expected_llm_calls


def _ids_by_sequence(items: list[dict[str, object]], id_field: str) -> list[str]:
    return [
        cast(str, item[id_field])
        for item in sorted(items, key=lambda item: cast(int, item["sequence"]))
    ]


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _evaluation_result(
    run_id: str,
    *,
    evaluator_name: str,
    created_at: datetime,
    label: str,
    passed: bool,
    reason: str,
) -> EvaluationResult:
    return EvaluationResult(
        run_id=run_id,
        evaluator_name=evaluator_name,
        evaluator_version="1.0.0",
        regression_run_id=None,
        status="completed",
        passed=passed,
        score=1.0 if passed else 0.0,
        label=label,
        severity=None,
        reason=reason,
        findings=[],
        created_at=created_at,
    )
