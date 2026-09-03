from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import RegressionRun
from obs_platform.evaluation.contracts import SCENARIO_CONTRACTS_VERSION
from obs_platform.evaluation.registry import DETERMINISTIC_EVALUATORS


async def create_regression_run(
    session: AsyncSession,
    *,
    agent_version: str,
    agent_model_provider: str,
    agent_model_name: str,
    prompt_version: str,
    repetitions: int,
    scenario_ids: list[str],
    name: str | None = None,
    is_baseline: bool = False,
) -> RegressionRun:
    record = RegressionRun(
        name=name,
        agent_version=agent_version,
        agent_model_provider=agent_model_provider,
        agent_model_name=agent_model_name,
        prompt_version=prompt_version,
        scenario_contract_version=SCENARIO_CONTRACTS_VERSION,
        evaluator_versions={
            evaluator.name: evaluator.version for evaluator in DETERMINISTIC_EVALUATORS
        },
        repetitions=repetitions,
        scenario_ids=scenario_ids,
        status="pending",
        is_baseline=is_baseline,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    await session.commit()
    return record
