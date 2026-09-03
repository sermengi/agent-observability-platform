import inspect
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import (
    AgentRun,
    JudgeCall,
    LLMCall,
    RegressionRun,
    Span,
    ToolCall,
)
from obs_platform.db.models import EvaluationResult as EvaluationResultRecord
from obs_platform.db.models import RunFailure as RunFailureRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.classifier import FailureClassifier, RunFailureResult
from obs_platform.evaluation.judges.client import JudgeCallResult
from obs_platform.evaluation.persistence import (
    persist_evaluation_result,
    persist_judge_call,
    persist_regression_linkage,
    persist_run_failure,
)
from obs_platform.evaluation.types import (
    EvaluationResult,
    EvaluationRunView,
    EvaluatorExecutionStatus,
    EvaluatorType,
    FailureType,
    OverallEvaluationStatus,
)
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.regressions.persistence import (
    create_regression_run as create_regression_run_record,
)
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
            passed=False,
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
    assert record.passed is False
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


async def test_failed_evaluator_row_nulls_out_outcome_fields(
    session: AsyncSession,
) -> None:
    run_id = "phase5-evaluation-result-failed-status"
    await _create_run(session, run_id)

    record = await persist_evaluation_result(
        session,
        run_id,
        _evaluator("policy"),
        EvaluatorExecutionStatus.FAILED,
        _result(
            passed=False,
            score=0.2,
            label="fail",
            severity="critical",
            reason="RuntimeError: forced evaluator failure",
            findings=[
                {
                    "code": "evaluator_exception",
                    "message": "Evaluator raised an exception",
                    "data": {
                        "exception_type": "RuntimeError",
                        "message": "forced evaluator failure",
                    },
                }
            ],
        ),
    )

    assert record.status == "failed"
    assert record.passed is None
    assert record.score is None
    assert record.label is None
    assert record.severity is None
    assert record.reason == "RuntimeError: forced evaluator failure"
    assert record.findings == [
        {
            "code": "evaluator_exception",
            "message": "Evaluator raised an exception",
            "data": {
                "exception_type": "RuntimeError",
                "message": "forced evaluator failure",
            },
        }
    ]

    await _delete_run(session, run_id)


async def test_evaluation_result_status_rejects_unknown_value(
    session: AsyncSession,
) -> None:
    run_id = "phase5-evaluation-result-status-check"
    await _create_run(session, run_id)

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(EvaluationResultRecord).values(
                run_id=run_id,
                evaluator_name="test",
                evaluator_version="1.0.0",
                regression_run_id=None,
                status="not-a-status",
                passed=True,
                score=1.0,
                label="pass",
                severity=None,
                reason="invalid status",
                findings=[],
                created_at=load_fixture("healthy_success").started_at,
            )
        )
        await session.commit()
    await session.rollback()

    await _delete_run(session, run_id)


def test_persist_evaluation_result_requires_explicit_status_argument() -> None:
    signature = inspect.signature(persist_evaluation_result)

    assert "status" in signature.parameters
    assert signature.parameters["status"].default is inspect.Signature.empty


def test_persist_run_failure_has_locked_task_6_signature() -> None:
    signature = inspect.signature(persist_run_failure)

    assert list(signature.parameters) == ["session", "run_id", "classification"]


async def test_persist_run_failure_upserts_current_snapshot(
    session: AsyncSession,
) -> None:
    run_id = "phase5-run-failure-upsert"
    await _create_run(session, run_id)

    first = await persist_run_failure(
        session,
        run_id,
        RunFailureResult(
            overall_status=OverallEvaluationStatus.FAIL,
            primary_category=FailureType.TOOL_FAILURE,
            secondary_category=None,
            max_severity="error",
        ),
    )
    second = await persist_run_failure(
        session,
        run_id,
        RunFailureResult(
            overall_status=OverallEvaluationStatus.PASS,
            primary_category=None,
            secondary_category=None,
            max_severity=None,
        ),
    )

    rows = list(
        await session.scalars(
            select(RunFailureRecord).where(RunFailureRecord.run_id == run_id)
        )
    )
    assert len(rows) == 1
    assert first.run_id == second.run_id == run_id
    assert rows[0].overall_status == "pass"
    assert rows[0].primary_category is None
    assert rows[0].max_severity is None

    await _delete_run(session, run_id)


async def test_persist_run_failure_populates_classifier_version(
    session: AsyncSession,
) -> None:
    run_id = "phase5-run-failure-classifier-version"
    await _create_run(session, run_id)

    record = await persist_run_failure(
        session,
        run_id,
        RunFailureResult(
            overall_status=OverallEvaluationStatus.FAIL,
            primary_category=FailureType.POLICY_VIOLATION,
            secondary_category=FailureType.TOOL_FAILURE,
            max_severity="critical",
        ),
    )

    assert record.classifier_version == FailureClassifier.version

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


async def test_persist_judge_call_inserts_call_accounting_separately_from_llm_calls(
    session: AsyncSession,
) -> None:
    run_id = "phase6-judge-call-persistence"
    await _create_run(session, run_id)

    record = await persist_judge_call(
        session,
        run_id,
        evaluator_name="groundedness",
        evaluator_version="1.0.0",
        call=_judge_call_result(),
        succeeded=True,
    )

    assert record.id is not None
    assert record.run_id == run_id
    assert record.evaluator_name == "groundedness"
    assert record.evaluator_version == "1.0.0"
    assert record.provider == "mock-provider"
    assert record.model == "mock-model"
    assert record.latency_ms == 123
    assert record.prompt_tokens == 17
    assert record.completion_tokens == 5
    assert record.estimated_cost_usd == 0.00042
    assert record.succeeded is True
    assert await _judge_call_count(session, run_id) == 1
    assert await _llm_call_count(session, run_id) == 2

    await _delete_run(session, run_id)


async def test_persist_judge_call_is_insert_only(
    session: AsyncSession,
) -> None:
    run_id = "phase6-judge-call-insert-only"
    await _create_run(session, run_id)

    first = await persist_judge_call(
        session,
        run_id,
        evaluator_name="groundedness",
        evaluator_version="1.0.0",
        call=_judge_call_result(),
        succeeded=False,
    )
    second = await persist_judge_call(
        session,
        run_id,
        evaluator_name="groundedness",
        evaluator_version="1.0.0",
        call=_judge_call_result(),
        succeeded=True,
    )

    assert first.id != second.id
    assert await _judge_call_count(session, run_id) == 2

    await _delete_run(session, run_id)


async def test_persist_regression_linkage_overwrites_event_scenario_id(
    session: AsyncSession,
) -> None:
    run_id = "phase7-regression-linkage-overwrite"
    await _delete_run(session, run_id)
    regression_run = await _create_regression_run(session)
    event = load_fixture("healthy_success")
    event.run_id = run_id
    event.scenario_id = "producer-supplied-scenario"
    await ingest_run_event(session, event)

    record = await persist_regression_linkage(
        session,
        run_id=run_id,
        regression_run_id=regression_run.id,
        scenario_id="orchestrator-dispatched-scenario",
        repetition_index=0,
    )

    assert record.run_id == run_id
    assert record.regression_run_id == regression_run.id
    assert record.scenario_id == "orchestrator-dispatched-scenario"
    assert record.repetition_index == 0

    await _delete_run(session, run_id)
    await _delete_regression_run(session, regression_run.id)


async def test_duplicate_regression_scenario_repetition_linkage_is_rejected(
    session: AsyncSession,
) -> None:
    first_run_id = "phase7-regression-linkage-duplicate-1"
    second_run_id = "phase7-regression-linkage-duplicate-2"
    await _delete_run(session, first_run_id)
    await _delete_run(session, second_run_id)
    regression_run = await _create_regression_run(session)
    regression_run_id = regression_run.id
    await _create_run(session, first_run_id)
    await _create_run(session, second_run_id)

    await persist_regression_linkage(
        session,
        run_id=first_run_id,
        regression_run_id=regression_run_id,
        scenario_id="GS-DUPLICATE",
        repetition_index=0,
    )

    with pytest.raises(IntegrityError):
        await persist_regression_linkage(
            session,
            run_id=second_run_id,
            regression_run_id=regression_run_id,
            scenario_id="GS-DUPLICATE",
            repetition_index=0,
        )
    await session.rollback()

    await _delete_run(session, first_run_id)
    await _delete_run(session, second_run_id)
    await _delete_regression_run(session, regression_run_id)


async def test_live_runs_keep_null_regression_linkage_and_do_not_conflict(
    session: AsyncSession,
) -> None:
    first_run_id = "phase7-live-null-linkage-1"
    second_run_id = "phase7-live-null-linkage-2"
    await _delete_run(session, first_run_id)
    await _delete_run(session, second_run_id)

    await _create_run(session, first_run_id)
    await _create_run(session, second_run_id)

    rows = list(
        await session.scalars(
            select(AgentRun).where(AgentRun.run_id.in_([first_run_id, second_run_id]))
        )
    )

    assert len(rows) == 2
    assert all(row.regression_run_id is None for row in rows)
    assert all(row.repetition_index is None for row in rows)

    await _delete_run(session, first_run_id)
    await _delete_run(session, second_run_id)


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


class _JudgeOutput(BaseModel):
    verdict: str


def _judge_call_result() -> JudgeCallResult[_JudgeOutput]:
    return JudgeCallResult(
        output=_JudgeOutput(verdict="pass"),
        model="mock-model",
        provider="mock-provider",
        latency_ms=123,
        prompt_tokens=17,
        completion_tokens=5,
        estimated_cost_usd=0.00042,
    )


async def _create_run(session: AsyncSession, run_id: str) -> None:
    await _delete_run(session, run_id)
    event = load_fixture("healthy_success")
    event.run_id = run_id
    await ingest_run_event(session, event)


async def _create_regression_run(session: AsyncSession) -> RegressionRun:
    record = await create_regression_run_record(
        session,
        agent_version="agent-v1",
        agent_model_provider="mock-provider",
        agent_model_name="mock-model",
        prompt_version="prompt-v1",
        repetitions=1,
        scenario_ids=["GS-08"],
    )
    await session.commit()
    return record


async def _delete_run(session: AsyncSession, run_id: str) -> None:
    for model in cast(
        tuple[Any, ...],
        (
            RunFailureRecord,
            EvaluationResultRecord,
            JudgeCall,
            LLMCall,
            ToolCall,
            Span,
            AgentRun,
        ),
    ):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.commit()


async def _delete_regression_run(session: AsyncSession, regression_run_id: int) -> None:
    await session.execute(
        delete(RegressionRun).where(RegressionRun.id == regression_run_id)
    )
    await session.commit()


async def _evaluation_result_count(session: AsyncSession, run_id: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(EvaluationResultRecord)
        .where(EvaluationResultRecord.run_id == run_id)
    )
    return int(count or 0)


async def _judge_call_count(session: AsyncSession, run_id: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(JudgeCall).where(JudgeCall.run_id == run_id)
    )
    return int(count or 0)


async def _llm_call_count(session: AsyncSession, run_id: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(LLMCall).where(LLMCall.run_id == run_id)
    )
    return int(count or 0)
