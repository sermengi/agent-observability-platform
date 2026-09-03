from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, EvaluationResult, RegressionRun
from obs_platform.evaluation.contracts import SCENARIO_CONTRACTS_VERSION
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS
from obs_platform.regressions.persistence import create_regression_run


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def test_create_regression_run_freezes_metadata_before_child_runs(
    session: AsyncSession,
) -> None:
    record = await create_regression_run(
        session,
        name="phase7 metadata smoke",
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-08", "GS-DEBUG-TRAJ-01"],
    )

    assert record.id is not None
    assert record.name == "phase7 metadata smoke"
    assert record.agent_version == "agent-v1"
    assert record.agent_model_provider == "mock-provider"
    assert record.agent_model_name == "mock-model"
    assert record.prompt_version == "prompt-v1"
    assert record.scenario_contract_version == SCENARIO_CONTRACTS_VERSION
    assert record.evaluator_versions == {
        evaluator.name: evaluator.version for evaluator in DETERMINISTIC_EVALUATORS
    }
    assert len(record.evaluator_versions) == 7
    assert record.repetitions == 2
    assert record.scenario_ids == ["GS-08", "GS-DEBUG-TRAJ-01"]
    assert record.status == "pending"
    assert record.is_baseline is False
    assert record.started_at is None
    assert record.completed_at is None

    assert await _child_run_count(session, record.id) == 0
    assert await _child_evaluation_count(session, record.id) == 0

    await _delete_regression_run(session, record.id)


async def test_regression_run_status_rejects_unknown_value(
    session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            insert(RegressionRun).values(
                agent_version="agent-v1",
                agent_model_provider="mock-provider",
                agent_model_name="mock-model",
                prompt_version="prompt-v1",
                scenario_contract_version=SCENARIO_CONTRACTS_VERSION,
                evaluator_versions={},
                repetitions=1,
                scenario_ids=["GS-08"],
                status="not-a-status",
            )
        )
        await session.commit()
    await session.rollback()


async def test_only_one_regression_run_can_be_marked_baseline(
    session: AsyncSession,
) -> None:
    await session.execute(update(RegressionRun).values(is_baseline=False))
    await session.commit()

    first = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=1,
        scenario_ids=["GS-08"],
        is_baseline=True,
    )
    second = await create_regression_run(
        session,
        agent_version="agent-v2",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=1,
        scenario_ids=["GS-08"],
    )
    first_id = first.id
    second_id = second.id

    with pytest.raises(IntegrityError):
        await session.execute(
            update(RegressionRun)
            .where(RegressionRun.id == second_id)
            .values(is_baseline=True)
        )
        await session.commit()
    await session.rollback()

    baseline_ids = list(
        await session.scalars(
            select(RegressionRun.id).where(RegressionRun.is_baseline.is_(True))
        )
    )
    assert baseline_ids == [first_id]

    await _delete_regression_run(session, second_id)
    await _delete_regression_run(session, first_id)


async def test_mocked_two_scenario_run_records_exact_subset(
    session: AsyncSession,
) -> None:
    record = await create_regression_run(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=2,
        scenario_ids=["GS-08", "GS-DEBUG-TRAJ-01"],
    )

    assert record.scenario_ids == ["GS-08", "GS-DEBUG-TRAJ-01"]

    await _delete_regression_run(session, record.id)


async def _child_run_count(session: AsyncSession, regression_run_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.regression_run_id == regression_run_id)
    )
    return int(count or 0)


async def _child_evaluation_count(session: AsyncSession, regression_run_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(EvaluationResult)
        .where(EvaluationResult.regression_run_id == regression_run_id)
    )
    return int(count or 0)


async def _delete_regression_run(session: AsyncSession, regression_run_id: int) -> None:
    await session.execute(
        update(RegressionRun)
        .where(RegressionRun.id == regression_run_id)
        .values(is_baseline=False)
    )
    await session.delete(await session.get_one(RegressionRun, regression_run_id))
    await session.commit()
