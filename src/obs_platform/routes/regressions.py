from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from obs_platform.api.deps import get_session
from obs_platform.api.v1.schemas import (
    RegressionAgentMetrics,
    RegressionAggregationResponse,
    RegressionComparison,
    RegressionCreateRequest,
    RegressionDetailResponse,
    RegressionEvaluationMetrics,
    RegressionEvaluatorPassRate,
    RegressionPassRate,
    RegressionScenarioPassRate,
    RegressionSummary,
)
from obs_platform.db.models import RegressionRun
from obs_platform.evaluation.contracts import (
    FINAL_SUITE_REPETITIONS,
    GOLDEN_SCENARIO_IDS,
)
from obs_platform.regressions.aggregation import aggregate_regression_run
from obs_platform.regressions.persistence import create_regression_run
from obs_platform.regressions.runner import MockedAgentTarget, RegressionRunner
from obs_platform.telemetry.v1 import load_fixture

router = APIRouter()

__all__ = ["get_session", "router"]


@router.post("/regressions", response_model=RegressionSummary, status_code=202)
async def create_regression(
    payload: RegressionCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> RegressionSummary:
    if payload.is_baseline and await session.scalar(
        select(exists().where(RegressionRun.is_baseline.is_(True)))
    ):
        raise HTTPException(
            status_code=409,
            detail="a baseline regression already exists",
        )
    record = await create_regression_run(
        session,
        name=payload.name,
        agent_version="mock-agent-v1",
        agent_model_provider=payload.agent_model_provider,
        agent_model_name=payload.agent_model_name,
        prompt_version=payload.prompt_version,
        scenario_ids=payload.scenario_ids or list(GOLDEN_SCENARIO_IDS),
        repetitions=payload.repetitions or FINAL_SUITE_REPETITIONS,
        is_baseline=payload.is_baseline,
    )
    engine = session.bind
    if not isinstance(engine, AsyncEngine):
        raise RuntimeError("regression execution requires an async engine")
    background_tasks.add_task(_execute_regression, engine, record.id)
    return _summary(record)


@router.get("/regressions", response_model=list[RegressionSummary])
async def list_regressions(
    session: AsyncSession = Depends(get_session),
) -> list[RegressionSummary]:
    rows = list(
        await session.scalars(select(RegressionRun).order_by(RegressionRun.id.desc()))
    )
    return [_summary(row) for row in rows]


@router.get("/regressions/{regression_run_id}", response_model=RegressionDetailResponse)
async def get_regression(
    regression_run_id: int,
    session: AsyncSession = Depends(get_session),
) -> RegressionDetailResponse:
    record = await session.get(RegressionRun, regression_run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="regression run not found")
    aggregation = await aggregate_regression_run(session, record.id)
    baseline = await session.scalar(
        select(RegressionRun).where(RegressionRun.is_baseline.is_(True))
    )
    return RegressionDetailResponse(
        **_summary(record).model_dump(),
        agent_version=record.agent_version,
        agent_model_provider=record.agent_model_provider,
        agent_model_name=record.agent_model_name,
        prompt_version=record.prompt_version,
        scenario_contract_version=record.scenario_contract_version,
        evaluator_versions=record.evaluator_versions,
        aggregation=RegressionAggregationResponse(
            overall=RegressionPassRate(**aggregation.overall.__dict__),
            by_scenario=[
                RegressionScenarioPassRate(**item.__dict__)
                for item in aggregation.by_scenario
            ],
            by_evaluator=[
                RegressionEvaluatorPassRate(**item.__dict__)
                for item in aggregation.by_evaluator
            ],
            failure_distribution=aggregation.failure_distribution,
            agent=RegressionAgentMetrics(**aggregation.agent.__dict__),
            evaluation=RegressionEvaluationMetrics(**aggregation.evaluation.__dict__),
        ),
        comparison=(
            _comparison(record, baseline)
            if baseline is not None and baseline.id != record.id
            else None
        ),
    )


async def _execute_regression(engine: AsyncEngine, regression_run_id: int) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        runner = RegressionRunner(session=session, target=_mock_target())
        await runner.run(regression_run_id)


def _mock_target() -> MockedAgentTarget:
    events = {
        scenario_id: load_fixture("healthy_success")
        for scenario_id in GOLDEN_SCENARIO_IDS
        if scenario_id != "GS-08"
    }
    events["GS-08"] = load_fixture("hitl_pending")
    return MockedAgentTarget(
        events,
        approved_events={"GS-08": load_fixture("hitl_approved")},
    )


def _summary(record: RegressionRun) -> RegressionSummary:
    return RegressionSummary(
        id=record.id,
        name=record.name,
        status=record.status,
        started_at=record.started_at,
        completed_at=record.completed_at,
        is_baseline=record.is_baseline,
        scenario_ids=record.scenario_ids,
        repetitions=record.repetitions,
    )


def _comparison(
    record: RegressionRun,
    baseline: RegressionRun,
) -> RegressionComparison:
    differences = []
    if record.scenario_contract_version != baseline.scenario_contract_version:
        differences.append("scenario_contract_version")
    differences.extend(
        evaluator_name
        for evaluator_name in sorted(
            set(record.evaluator_versions) | set(baseline.evaluator_versions)
        )
        if record.evaluator_versions.get(evaluator_name)
        != baseline.evaluator_versions.get(evaluator_name)
    )
    return RegressionComparison(
        baseline_id=baseline.id,
        comparable=not differences,
        differences=differences,
    )
