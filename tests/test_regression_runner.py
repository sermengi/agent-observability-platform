from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import (
    AgentRun,
    EvaluationResult,
    JudgeCall,
    LLMCall,
    RegressionRun,
    RunFailure,
    Span,
    ToolCall,
)
from obs_platform.evaluation.contracts import load_scenario_contract
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.regressions.persistence import create_regression_run
from obs_platform.regressions.runner import (
    MockedAgentTarget,
    RegressionRunner,
)
from obs_platform.telemetry.v1 import load_fixture
from obs_platform.telemetry.v1.models import ExtendedRunEvent


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        await _delete_phase7_generated_runs(db_session)
        yield db_session
        await _delete_phase7_generated_runs(db_session)
    await engine.dispose()


async def test_mocked_agent_target_is_plain_deterministic_unit() -> None:
    contract = load_scenario_contract("GS-DEBUG-TRAJ-01")
    target = MockedAgentTarget({"GS-DEBUG-TRAJ-01": load_fixture("trajectory_error")})

    first = await target.run_scenario(contract)
    second = await target.run_scenario(contract)

    assert isinstance(first, ExtendedRunEvent)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first is not second


async def test_regression_runner_dispatches_ingests_links_and_evaluates(
    session: AsyncSession,
) -> None:
    regression_run = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-DEBUG-TRAJ-01"],
    )
    calls: list[tuple[str, str, int | None]] = []

    async def ingest(
        db_session: AsyncSession,
        event: ExtendedRunEvent,
    ) -> None:
        calls.append(("ingest", event.scenario_id or "", None))
        db_session.add(_agent_run_from_event(event))
        await db_session.commit()

    async def evaluate(db_session: AsyncSession, run_id: str) -> None:
        run = await db_session.get_one(AgentRun, run_id)
        calls.append(("evaluate", run.scenario_id or "", run.repetition_index))

    runner = RegressionRunner(
        session=session,
        target=MockedAgentTarget(
            {"GS-DEBUG-TRAJ-01": load_fixture("trajectory_error")}
        ),
        ingest_run_event=ingest,
        run_evaluation=evaluate,
    )

    await runner.run(regression_run.id)

    rows = list(
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.regression_run_id == regression_run.id)
            .order_by(AgentRun.repetition_index)
        )
    )
    assert [(row.scenario_id, row.repetition_index) for row in rows] == [
        ("GS-DEBUG-TRAJ-01", 0),
        ("GS-DEBUG-TRAJ-01", 1),
    ]
    assert calls == [
        ("ingest", "GS-DEBUG-TRAJ-01", None),
        ("evaluate", "GS-DEBUG-TRAJ-01", 0),
        ("ingest", "GS-DEBUG-TRAJ-01", None),
        ("evaluate", "GS-DEBUG-TRAJ-01", 1),
    ]

    await _delete_runs_for_regression(session, regression_run.id)
    await _delete_regression_run(session, regression_run.id)


async def test_runner_records_failed_repetition_and_continues(
    session: AsyncSession,
) -> None:
    regression_run = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-DEBUG-TRAJ-01"],
    )
    attempted: list[int] = []

    async def ingest(
        db_session: AsyncSession,
        event: ExtendedRunEvent,
    ) -> None:
        attempted.append(len(attempted))
        if len(attempted) == 1:
            raise RuntimeError("first dispatch failed")
        db_session.add(_agent_run_from_event(event))
        await db_session.commit()

    runner = RegressionRunner(
        session=session,
        target=MockedAgentTarget(
            {"GS-DEBUG-TRAJ-01": load_fixture("trajectory_error")}
        ),
        ingest_run_event=ingest,
        run_evaluation=_noop_evaluation,
    )

    result = await runner.run(regression_run.id)

    assert result.created_run_ids == [
        f"phase7-regression-{regression_run.id}-GS-DEBUG-TRAJ-01-rep-1"
    ]
    assert len(result.errors) == 1
    assert attempted == [0, 1]

    await _delete_runs_for_regression(session, regression_run.id)
    await _delete_regression_run(session, regression_run.id)


async def test_mocked_two_by_two_regression_links_and_evaluates_mixed_outcomes(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE__ANTHROPIC_API_KEY", "")
    scenario_ids = ["GS-DEBUG-SMOKE-01", "GS-DEBUG-TRAJ-01"]
    regression_run = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=scenario_ids,
    )
    regression_run_id = regression_run.id
    assert regression_run.status == "pending"
    status_during_ingestion: list[str] = []

    async def ingest_and_capture_status(
        db_session: AsyncSession,
        event: ExtendedRunEvent,
    ) -> None:
        current_status = await db_session.scalar(
            select(RegressionRun.status).where(RegressionRun.id == regression_run_id)
        )
        status_during_ingestion.append(current_status or "")
        await db_session.rollback()
        await ingest_run_event(db_session, event)

    runner = RegressionRunner(
        session=session,
        target=MockedAgentTarget(
            {
                "GS-DEBUG-SMOKE-01": load_fixture("healthy_success"),
                "GS-DEBUG-TRAJ-01": load_fixture("trajectory_error"),
            }
        ),
        ingest_run_event=ingest_and_capture_status,
    )

    result = await runner.run(regression_run_id)

    assert result.errors == []
    rows = list(
        await session.scalars(
            select(AgentRun)
            .where(AgentRun.regression_run_id == regression_run_id)
            .order_by(AgentRun.scenario_id, AgentRun.repetition_index)
        )
    )
    assert len(rows) == 4
    assert [(row.scenario_id, row.repetition_index) for row in rows] == [
        ("GS-DEBUG-SMOKE-01", 0),
        ("GS-DEBUG-SMOKE-01", 1),
        ("GS-DEBUG-TRAJ-01", 0),
        ("GS-DEBUG-TRAJ-01", 1),
    ]
    assert set(result.created_run_ids) == {row.run_id for row in rows}
    run_failures = list(
        await session.scalars(
            select(RunFailure)
            .join(AgentRun, AgentRun.run_id == RunFailure.run_id)
            .where(AgentRun.regression_run_id == regression_run_id)
            .order_by(AgentRun.scenario_id, AgentRun.repetition_index)
        )
    )
    assert [row.overall_status for row in run_failures] == [
        "pass",
        "pass",
        "fail",
        "fail",
    ]
    trajectory_failures = list(
        await session.scalars(
            select(EvaluationResult)
            .join(AgentRun, AgentRun.run_id == EvaluationResult.run_id)
            .where(AgentRun.regression_run_id == regression_run_id)
            .where(AgentRun.scenario_id == "GS-DEBUG-TRAJ-01")
            .where(EvaluationResult.evaluator_name == "trajectory")
            .order_by(AgentRun.repetition_index)
        )
    )
    assert len(trajectory_failures) == 2
    assert all(row.passed is False for row in trajectory_failures)
    assert all(
        "ordering_violation" in {finding["code"] for finding in row.findings or []}
        for row in trajectory_failures
    )
    assert await _evaluation_result_count(session, regression_run_id) == 28
    assert status_during_ingestion == ["running", "running", "running", "running"]
    completed = await session.get_one(RegressionRun, regression_run_id)
    assert completed.status == "completed"
    assert completed.started_at is not None
    assert completed.completed_at is not None

    await _delete_runs_for_regression(session, regression_run_id)
    await _delete_regression_run(session, regression_run_id)


async def _noop_evaluation(session: AsyncSession, run_id: str) -> None:
    return None


def _agent_run_from_event(event: ExtendedRunEvent) -> AgentRun:
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
        runtime_error_category=(
            event.runtime_error.category if event.runtime_error is not None else None
        ),
        runtime_error_code=(
            event.runtime_error.code if event.runtime_error is not None else None
        ),
        runtime_error_message=(
            event.runtime_error.message if event.runtime_error is not None else None
        ),
        runtime_error_failed_component=(
            event.runtime_error.failed_component
            if event.runtime_error is not None
            else None
        ),
        ingested_at=event.started_at,
        updated_at=event.started_at,
    )


async def _delete_runs_for_regression(
    session: AsyncSession,
    regression_run_id: int,
) -> None:
    run_ids = list(
        await session.scalars(
            select(AgentRun.run_id).where(
                AgentRun.regression_run_id == regression_run_id
            )
        )
    )
    if not run_ids:
        return

    for model in (
        RunFailure,
        EvaluationResult,
        JudgeCall,
        LLMCall,
        ToolCall,
        Span,
        AgentRun,
    ):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()


async def _evaluation_result_count(
    session: AsyncSession,
    regression_run_id: int,
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(EvaluationResult)
        .where(EvaluationResult.regression_run_id == regression_run_id)
    )
    return int(count or 0)


async def _delete_regression_run(
    session: AsyncSession,
    regression_run_id: int,
) -> None:
    await session.execute(
        delete(RegressionRun).where(RegressionRun.id == regression_run_id)
    )
    await session.commit()


async def _delete_phase7_generated_runs(session: AsyncSession) -> None:
    run_ids = list(
        await session.scalars(
            select(AgentRun.run_id).where(AgentRun.run_id.like("phase7-%"))
        )
    )
    if not run_ids:
        return

    for model in (
        RunFailure,
        EvaluationResult,
        JudgeCall,
        LLMCall,
        ToolCall,
        Span,
        AgentRun,
    ):
        await session.execute(delete(model).where(model.run_id.in_(run_ids)))
    await session.commit()
