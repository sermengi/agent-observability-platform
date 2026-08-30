from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.persistence import persist_evaluation_result
from obs_platform.evaluation.types import (
    EvaluationResult,
    EvaluationRunView,
    EvaluatorExecutionStatus,
    EvaluatorType,
)
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.telemetry.v1 import load_fixture


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def test_persisting_same_evaluator_twice_inserts_two_rows(
    session: AsyncSession,
) -> None:
    run_id = "phase4-evaluation-result-append-only"
    await _create_run(session, run_id)
    evaluator = _evaluator("tool_execution")

    first = await persist_evaluation_result(
        session,
        run_id,
        evaluator,
        EvaluatorExecutionStatus.COMPLETED,
        _result(reason="first evaluation"),
    )
    second = await persist_evaluation_result(
        session,
        run_id,
        evaluator,
        EvaluatorExecutionStatus.COMPLETED,
        _result(reason="second evaluation"),
    )

    assert first.id != second.id
    assert await _evaluation_result_count(session, run_id) == 2

    await _delete_run(session, run_id)


async def test_persisted_rows_use_completed_status_and_no_regression_run(
    session: AsyncSession,
) -> None:
    run_id = "phase4-evaluation-result-completed"
    await _create_run(session, run_id)

    record = await persist_evaluation_result(
        session,
        run_id,
        _evaluator("structured_output"),
        EvaluatorExecutionStatus.COMPLETED,
        _result(
            score=0.5,
            label="fail",
            severity=None,
            reason="structured output failed",
            findings=[
                {
                    "code": "empty_output",
                    "message": "Final result output is empty",
                    "data": {"run_id": run_id},
                }
            ],
        ),
    )

    assert record.status == "completed"
    assert record.regression_run_id is None
    assert record.evaluator_name == "structured_output"
    assert record.evaluator_version == "1.0.0"
    assert record.score == 0.5
    assert record.label == "fail"
    assert record.severity is None
    assert record.reason == "structured output failed"
    assert record.findings == [
        {
            "code": "empty_output",
            "message": "Final result output is empty",
            "data": {"run_id": run_id},
        }
    ]

    await _delete_run(session, run_id)


async def test_later_persistence_failure_does_not_roll_back_prior_commits(
    session: AsyncSession,
) -> None:
    run_id = "phase4-evaluation-result-partial-commits"
    await _create_run(session, run_id)

    for evaluator_name in ("tool_execution", "structured_output", "trajectory"):
        await persist_evaluation_result(
            session,
            run_id,
            _evaluator(evaluator_name),
            EvaluatorExecutionStatus.COMPLETED,
            _result(reason=f"{evaluator_name} passed"),
        )

    with pytest.raises(IntegrityError):
        await persist_evaluation_result(
            session,
            "missing-run-id",
            _evaluator("policy"),
            EvaluatorExecutionStatus.COMPLETED,
            _result(reason="policy passed"),
        )
    await session.rollback()

    assert await _evaluation_result_count(session, run_id) == 3

    await _delete_run(session, run_id)


async def test_persist_evaluation_result_uses_plain_async_session(
    session: AsyncSession,
) -> None:
    run_id = "phase4-evaluation-result-direct-session"
    await _create_run(session, run_id)

    record = await persist_evaluation_result(
        session,
        run_id,
        _evaluator("evidence"),
        EvaluatorExecutionStatus.COMPLETED,
        _result(reason="evidence passed"),
    )

    assert record.run_id == run_id
    assert await _evaluation_result_count(session, run_id) == 1

    await _delete_run(session, run_id)


class _Evaluator(Evaluator):
    name = "test"
    version = "1.0.0"
    type = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationResult:
        return _result(reason=f"{run.run_id} passed")


def _evaluator(name: str) -> Evaluator:
    evaluator_class = cast(
        type[Evaluator],
        type(
            f"{name.title().replace('_', '')}Evaluator",
            (_Evaluator,),
            {"name": name},
        ),
    )
    return evaluator_class()


def _result(
    *,
    passed: bool = True,
    score: float | None = 1.0,
    label: str | None = "pass",
    severity: str | None = None,
    reason: str = "passed",
    findings: list[dict[str, object]] | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        passed=passed,
        score=score,
        label=label,
        severity=severity,
        reason=reason,
        findings=findings or [],
    )


async def _create_run(session: AsyncSession, run_id: str) -> None:
    await _delete_run(session, run_id)
    event = load_fixture("healthy_success")
    event.run_id = run_id
    await ingest_run_event(session, event)


async def _delete_run(session: AsyncSession, run_id: str) -> None:
    for model in cast(
        tuple[Any, ...],
        (EvaluationResultRecord, LLMCall, ToolCall, Span, AgentRun),
    ):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.commit()


async def _evaluation_result_count(session: AsyncSession, run_id: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(EvaluationResultRecord)
        .where(EvaluationResultRecord.run_id == run_id)
    )
    return int(count or 0)
