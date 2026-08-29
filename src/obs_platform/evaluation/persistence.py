from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.types import EvaluationResult


async def persist_evaluation_result(
    session: AsyncSession,
    run_id: str,
    evaluator: Evaluator,
    result: EvaluationResult,
) -> EvaluationResultRecord:
    record = EvaluationResultRecord(
        run_id=run_id,
        evaluator_name=evaluator.name,
        evaluator_version=evaluator.version,
        regression_run_id=None,
        status="completed",
        score=result.score,
        label=result.label,
        severity=result.severity,
        reason=result.reason,
        findings=[
            finding.model_dump(mode="json") for finding in result.findings
        ],
        created_at=datetime.now(UTC),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record
