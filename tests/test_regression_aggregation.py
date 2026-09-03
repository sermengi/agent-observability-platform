from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
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
from obs_platform.evaluation.persistence import persist_regression_linkage
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.regressions.aggregation import aggregate_regression_run
from obs_platform.regressions.persistence import create_regression_run
from obs_platform.telemetry.v1 import load_fixture


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        await _delete_generated_regressions(db_session)
        try:
            yield db_session
        finally:
            await _delete_generated_regressions(db_session)
    await engine.dispose()


async def test_regression_aggregation_reports_locked_metrics(
    session: AsyncSession,
) -> None:
    regression_run_id = await _seed_completed_regression(session)

    aggregation = await aggregate_regression_run(session, regression_run_id)

    assert aggregation.regression_run_id == regression_run_id
    assert aggregation.overall.pass_rate == pytest.approx(0.25)
    assert aggregation.overall.counts == {"pass": 1, "fail": 2, "incomplete": 1}
    assert [
        (item.scenario_id, item.pass_rate, item.counts)
        for item in aggregation.by_scenario
    ] == [
        ("GS-DEBUG-SMOKE-01", 0.5, {"pass": 1, "fail": 1, "incomplete": 0}),
        ("GS-DEBUG-TRAJ-01", 0.0, {"pass": 0, "fail": 1, "incomplete": 1}),
    ]
    assert aggregation.failure_distribution == [
        ("retrieval_failure", 1),
        ("trajectory_error", 1),
    ]
    assert aggregation.agent.avg_latency_ms == pytest.approx(250)
    assert aggregation.agent.p95_latency_ms == pytest.approx(385)
    assert aggregation.agent.avg_tokens == pytest.approx(25)
    assert aggregation.agent.avg_cost_usd == pytest.approx(2.5)
    assert aggregation.evaluation.total_tokens == 100
    assert aggregation.evaluation.avg_tokens == pytest.approx(25)
    assert aggregation.evaluation.total_cost_usd == pytest.approx(1.0)
    assert aggregation.evaluation.avg_cost_usd == pytest.approx(0.25)
    assert aggregation.evaluation.avg_latency_ms == pytest.approx(27.5)

    await _delete_regression(session, regression_run_id)


async def test_regression_aggregation_excludes_skipped_evaluators_and_is_stable(
    session: AsyncSession,
) -> None:
    regression_run_id = await _seed_completed_regression(session)

    first = await aggregate_regression_run(session, regression_run_id)
    second = await aggregate_regression_run(session, regression_run_id)

    groundedness = next(
        item for item in first.by_evaluator if item.evaluator_name == "groundedness"
    )
    assert groundedness.total_count == 4
    assert groundedness.skipped_count == 2
    assert groundedness.passed_count == 1
    assert groundedness.pass_rate == pytest.approx(0.5)
    assert first == second


async def test_regression_aggregation_counts_runs_still_awaiting_evaluation(
    session: AsyncSession,
) -> None:
    """A run's agent_runs row is committed before evaluation writes
    run_failures, so a live poll of a "running" regression can see a linked
    run with no run_failures row yet. It must still count toward the
    pass-rate denominator instead of silently vanishing (regression test for
    the inner-join bug: it used to be dropped from `overall`/`by_scenario`
    while `agent` already counted it).
    """
    regression = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-DEBUG-SMOKE-01"],
        name="phase7 aggregation test",
    )
    regression_run_id = regression.id
    await session.commit()
    await session.rollback()

    for repetition, evaluated in [(0, True), (1, False)]:
        run_id = f"phase7-aggregation-pending-{regression_run_id}-{repetition}"
        event = load_fixture("healthy_success").model_copy(
            deep=True, update={"run_id": run_id}
        )
        await session.rollback()
        await ingest_run_event(session, event)
        await persist_regression_linkage(
            session, run_id, regression_run_id, "GS-DEBUG-SMOKE-01", repetition
        )
        await session.commit()
        if evaluated:
            session.add(
                RunFailure(
                    run_id=run_id,
                    overall_status="pass",
                    primary_category=None,
                    secondary_category=None,
                    max_severity=None,
                    classifier_version="test",
                    updated_at=datetime(2050, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()

    aggregation = await aggregate_regression_run(session, regression_run_id)

    assert aggregation.overall.counts == {"pass": 1, "fail": 0, "incomplete": 0}
    assert aggregation.overall.pass_rate == pytest.approx(0.5)
    assert len(aggregation.by_scenario) == 1
    assert aggregation.by_scenario[0].scenario_id == "GS-DEBUG-SMOKE-01"
    assert aggregation.by_scenario[0].counts == {"pass": 1, "fail": 0, "incomplete": 0}
    assert aggregation.by_scenario[0].pass_rate == pytest.approx(0.5)
    assert aggregation.agent.avg_tokens is not None  # both runs counted here too

    await _delete_regression(session, regression_run_id)

    await _delete_regression(session, regression_run_id)


async def _seed_completed_regression(session: AsyncSession) -> int:
    regression = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-DEBUG-SMOKE-01", "GS-DEBUG-TRAJ-01"],
        name="phase7 aggregation test",
    )
    regression_run_id = regression.id
    await session.commit()
    await session.rollback()
    run_specs = [
        ("GS-DEBUG-SMOKE-01", 0, "pass", None, 100, 10, 1.0),
        ("GS-DEBUG-SMOKE-01", 1, "fail", "trajectory_error", 200, 20, 2.0),
        ("GS-DEBUG-TRAJ-01", 0, "fail", "retrieval_failure", 300, 30, 3.0),
        ("GS-DEBUG-TRAJ-01", 1, "incomplete", None, 400, 40, 4.0),
    ]
    now = datetime(2050, 1, 1, tzinfo=UTC)
    for index, (
        scenario_id,
        repetition,
        overall,
        category,
        latency,
        tokens,
        cost,
    ) in enumerate(run_specs):
        run_id = f"phase7-aggregation-{regression_run_id}-{index}"
        base_event = load_fixture("healthy_success")
        event = base_event.model_copy(
            deep=True,
            update={
                "run_id": run_id,
                "execution_latency_ms": latency,
                "usage": base_event.usage.model_copy(
                    update={
                        "total_tokens": tokens,
                        "total_estimated_cost_usd": cost,
                    }
                ),
            },
        )
        await session.rollback()
        await ingest_run_event(session, event)
        await persist_regression_linkage(
            session,
            run_id,
            regression_run_id,
            scenario_id,
            repetition,
        )
        run = await session.get_one(AgentRun, run_id)
        run.usage_total_tokens = tokens
        run.usage_total_estimated_cost_usd = cost
        await session.commit()
        session.add(
            RunFailure(
                run_id=run_id,
                overall_status=overall,
                primary_category=category,
                secondary_category=None,
                max_severity=None,
                classifier_version="test",
                updated_at=now,
            )
        )
        session.add_all(
            [
                EvaluationResult(
                    run_id=run_id,
                    regression_run_id=regression_run_id,
                    evaluator_name="tool_execution",
                    evaluator_version="test",
                    status="completed",
                    passed=True,
                    score=1.0,
                    label="pass",
                    severity=None,
                    reason="test",
                    findings=[],
                    created_at=now,
                ),
                EvaluationResult(
                    run_id=run_id,
                    regression_run_id=regression_run_id,
                    evaluator_name="groundedness",
                    evaluator_version="test",
                    status="completed" if index < 2 else "skipped",
                    passed=(index == 0) if index < 2 else None,
                    score=1.0 if index == 0 else 0.0 if index == 1 else None,
                    label="pass" if index == 0 else "fail" if index == 1 else None,
                    severity=None,
                    reason="test",
                    findings=[],
                    created_at=now,
                ),
                JudgeCall(
                    run_id=run_id,
                    evaluator_name="groundedness",
                    evaluator_version="test",
                    model="judge",
                    provider="mock",
                    latency_ms=(index + 1) * 11,
                    prompt_tokens=(index + 1) * 4,
                    completion_tokens=(index + 1) * 6,
                    estimated_cost_usd=(index + 1) / 10,
                    succeeded=True,
                    created_at=now,
                ),
            ]
        )
        await session.commit()
    regression.status = "completed"
    await session.commit()
    return regression_run_id


async def _delete_regression(session: AsyncSession, regression_run_id: int) -> None:
    run_ids = [
        row.run_id
        for row in await session.scalars(
            select(AgentRun).where(AgentRun.regression_run_id == regression_run_id)
        )
    ]
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
    await session.execute(
        delete(RegressionRun).where(RegressionRun.id == regression_run_id)
    )
    await session.commit()


async def _delete_generated_regressions(session: AsyncSession) -> None:
    named_regression_ids = list(
        await session.scalars(
            select(RegressionRun.id).where(
                RegressionRun.name == "phase7 aggregation test"
            )
        )
    )
    run_regression_ids = list(
        await session.scalars(
            select(AgentRun.regression_run_id)
            .where(AgentRun.run_id.like("phase7-aggregation-%"))
            .where(AgentRun.regression_run_id.is_not(None))
        )
    )
    for regression_run_id in set(named_regression_ids + run_regression_ids):
        if regression_run_id is not None:
            await _delete_regression(session, regression_run_id)
    await session.commit()
