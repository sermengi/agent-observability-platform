from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.db.models import RunFailure as RunFailureRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.classifier import FailureClassifier, RunFailureResult
from obs_platform.evaluation.types import EvaluationResult, EvaluatorExecutionStatus


async def persist_evaluation_result(
    session: AsyncSession,
    run_id: str,
    evaluator: Evaluator,
    status: EvaluatorExecutionStatus,
    result: EvaluationResult | None,
) -> EvaluationResultRecord:
    if status is not EvaluatorExecutionStatus.FAILED and result is None:
        raise ValueError("result is required unless status is failed")

    findings = None
    if result is not None:
        findings = [finding.model_dump(mode="json") for finding in result.findings]
    completed_result = result if status is EvaluatorExecutionStatus.COMPLETED else None

    record = EvaluationResultRecord(
        run_id=run_id,
        evaluator_name=evaluator.name,
        evaluator_version=evaluator.version,
        regression_run_id=None,
        status=status.value,
        passed=(completed_result.passed if completed_result is not None else None),
        score=(completed_result.score if completed_result is not None else None),
        label=(completed_result.label if completed_result is not None else None),
        severity=(completed_result.severity if completed_result is not None else None),
        reason=(result.reason if result is not None else None),
        findings=findings,
        created_at=datetime.now(UTC),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def persist_run_failure(
    session: AsyncSession,
    run_id: str,
    classification: RunFailureResult,
) -> RunFailureRecord:
    values = {
        "run_id": run_id,
        "overall_status": classification.overall_status.value,
        "primary_category": (
            classification.primary_category.value
            if classification.primary_category is not None
            else None
        ),
        "secondary_category": (
            classification.secondary_category.value
            if classification.secondary_category is not None
            else None
        ),
        "max_severity": classification.max_severity,
        "classifier_version": FailureClassifier.version,
        "updated_at": datetime.now(UTC),
    }
    statement = insert(RunFailureRecord).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[RunFailureRecord.run_id],
        set_={
            "overall_status": statement.excluded.overall_status,
            "primary_category": statement.excluded.primary_category,
            "secondary_category": statement.excluded.secondary_category,
            "max_severity": statement.excluded.max_severity,
            "classifier_version": statement.excluded.classifier_version,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await session.execute(statement)
    await session.commit()
    record = await session.get(RunFailureRecord, run_id, populate_existing=True)
    if record is None:
        raise RuntimeError("run failure upsert did not return a record")
    return record
