from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import (
    AgentRun,
    EvaluationResult,
    LLMCall,
    RunFailure,
    Span,
)
from obs_platform.db.models import ToolCall as ToolCallRecord
from obs_platform.evaluation.base import Evaluator
from obs_platform.evaluation.classifier import FailureClassifier, RunFailureResult
from obs_platform.evaluation.types import EvaluationResult as EvaluationOutcome
from obs_platform.evaluation.types import (
    EvaluationRunView,
    EvaluatorExecutionStatus,
    EvaluatorType,
)
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import runs
from obs_platform.telemetry.v1 import load_fixture


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
async def evaluation_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        yield client


async def test_evaluate_unknown_run_id_returns_404(
    evaluation_client: AsyncClient,
) -> None:
    response = await evaluation_client.post("/v1/runs/missing-run/evaluate")

    assert response.status_code == 404


async def test_evaluate_runs_all_deterministic_evaluators_for_awaiting_approval_run(
    session: AsyncSession,
    evaluation_client: AsyncClient,
) -> None:
    run_id = "phase5-evaluate-awaiting-approval"
    await _create_run(session, "hitl_pending", run_id)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert {item["evaluator_name"] for item in body["evaluator_results"]} == {
        "tool_execution",
        "structured_output",
        "trajectory",
        "policy",
        "evidence",
    }
    assert await _evaluation_result_count(session, run_id) == 5

    await _delete_run(session, run_id)


async def test_evaluator_exception_does_not_block_other_evaluators(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-evaluate-isolates-exceptions"
    await _create_run(session, "healthy_success", run_id)
    evaluators = [
        _FirstEvaluator(),
        _ExplodingEvaluator(),
        _SecondEvaluator(),
        _ThirdEvaluator(),
        _FourthEvaluator(),
    ]
    monkeypatch.setattr(runs, "DETERMINISTIC_EVALUATORS", evaluators)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert [item["evaluator_name"] for item in body["evaluator_results"]] == [
        "first",
        "exploding",
        "second",
        "third",
        "fourth",
    ]
    failed = body["evaluator_results"][1]
    assert failed["execution_status"] == "failed"
    assert failed["passed"] is None
    assert failed["score"] is None
    assert failed["label"] is None
    assert failed["severity"] is None
    assert failed["findings"][0]["code"] == "evaluator_exception"
    assert await _evaluation_result_count(session, run_id) == 5

    await _delete_run(session, run_id)


async def test_evaluate_dispatches_deterministic_and_llm_based_evaluators(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase6-evaluate-mixed-evaluator-types"
    await _create_run(session, "healthy_success", run_id)
    evaluators = [_FirstEvaluator(), _AsyncEvaluator()]
    monkeypatch.setattr(runs, "DETERMINISTIC_EVALUATORS", evaluators)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert [item["evaluator_name"] for item in body["evaluator_results"]] == [
        "first",
        "async_judge",
    ]
    assert [item["execution_status"] for item in body["evaluator_results"]] == [
        "completed",
        "completed",
    ]
    assert body["evaluator_results"][1]["reason"] == (
        f"{run_id} passed asynchronously"
    )
    assert await _evaluation_result_count(session, run_id) == 2

    await _delete_run(session, run_id)


async def test_evaluate_response_is_dedicated_schema_without_run_detail_fields(
    session: AsyncSession,
    evaluation_client: AsyncClient,
) -> None:
    run_id = "phase5-evaluate-response-shape"
    await _create_run(session, "healthy_success", run_id)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "run_id",
        "overall_status",
        "evaluator_results",
        "failure",
        "evaluated_at",
    }
    assert "spans" not in body
    assert "tool_calls" not in body
    assert "llm_calls" not in body
    assert "hitl" not in body
    assert "usage" not in body

    await _delete_run(session, run_id)


async def test_evaluate_twice_appends_results_and_upserts_one_run_failure(
    session: AsyncSession,
    evaluation_client: AsyncClient,
) -> None:
    run_id = "phase5-evaluate-repeat"
    await _create_run(session, "tool_failure", run_id)

    first = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")
    second = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert first.status_code == 200
    assert second.status_code == 200
    assert await _evaluation_result_count(session, run_id) == 10
    run_failures = list(
        await session.scalars(select(RunFailure).where(RunFailure.run_id == run_id))
    )
    assert len(run_failures) == 1
    assert run_failures[0].primary_category is not None
    assert (
        second.json()["failure"]["primary_category"]
        == run_failures[0].primary_category
    )

    await _delete_run(session, run_id)


async def test_evaluate_pass_verdict_persists_run_failure_without_category(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-overall-pass"
    await _create_run(session, "healthy_success", run_id)
    monkeypatch.setattr(
        runs,
        "DETERMINISTIC_EVALUATORS",
        [_FirstEvaluator(), _NotApplicableEvaluator()],
    )

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "pass"
    assert body["failure"] == {
        "primary_category": None,
        "secondary_category": None,
        "max_severity": None,
    }
    run_failure = await session.get(RunFailure, run_id)
    assert run_failure is not None
    assert run_failure.overall_status == "pass"
    assert run_failure.primary_category is None

    await _delete_run(session, run_id)


async def test_evaluate_fail_verdict_wins_over_other_completed_outcomes(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-overall-fail"
    await _create_run(session, "healthy_success", run_id)
    monkeypatch.setattr(
        runs,
        "DETERMINISTIC_EVALUATORS",
        [_FirstEvaluator(), _StructuredFailureEvaluator(), _NotApplicableEvaluator()],
    )

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    assert response.json()["overall_status"] == "fail"
    run_failure = await session.get(RunFailure, run_id)
    assert run_failure is not None
    assert run_failure.overall_status == "fail"

    await _delete_run(session, run_id)


async def test_evaluate_incomplete_when_evaluator_fails_without_confirmed_failure(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-overall-incomplete"
    await _create_run(session, "healthy_success", run_id)
    monkeypatch.setattr(
        runs,
        "DETERMINISTIC_EVALUATORS",
        [_FirstEvaluator(), _ExplodingEvaluator(), _SecondEvaluator()],
    )

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "incomplete"
    assert body["failure"]["primary_category"] is None
    run_failure = await session.get(RunFailure, run_id)
    assert run_failure is not None
    assert run_failure.overall_status == "incomplete"
    assert run_failure.primary_category is None

    await _delete_run(session, run_id)


async def test_evaluate_fail_precedes_incomplete_when_failure_and_crash_occur(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-overall-fail-before-incomplete"
    await _create_run(session, "healthy_success", run_id)
    monkeypatch.setattr(
        runs,
        "DETERMINISTIC_EVALUATORS",
        [_ExplodingEvaluator(), _StructuredFailureEvaluator(), _SecondEvaluator()],
    )

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    assert response.json()["overall_status"] == "fail"
    run_failure = await session.get(RunFailure, run_id)
    assert run_failure is not None
    assert run_failure.overall_status == "fail"
    assert run_failure.primary_category == "output_validation_error"

    await _delete_run(session, run_id)


async def test_evaluate_classifies_current_in_memory_evaluator_outcomes(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-evaluate-classifies-current-outcomes"
    await _create_run(session, "healthy_success", run_id)
    evaluators = [_FirstEvaluator(), _StructuredFailureEvaluator()]
    observed_outcomes = []
    monkeypatch.setattr(runs, "DETERMINISTIC_EVALUATORS", evaluators)

    original_classify = FailureClassifier.classify

    def spy_classify(self: FailureClassifier, outcomes: list[Any]) -> RunFailureResult:
        observed_outcomes.extend(outcomes)
        return original_classify(self, outcomes)

    monkeypatch.setattr(FailureClassifier, "classify", spy_classify)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 200
    assert [outcome.evaluator_name for outcome in observed_outcomes] == [
        "first",
        "structured_failure",
    ]
    assert all(
        outcome.execution_status is EvaluatorExecutionStatus.COMPLETED
        for outcome in observed_outcomes
    )
    assert [outcome.result.label for outcome in observed_outcomes] == ["pass", "fail"]

    await _delete_run(session, run_id)


async def test_run_failure_persistence_failure_leaves_evaluation_results_committed(
    session: AsyncSession,
    evaluation_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "phase5-run-failure-persist-failure"
    await _create_run(session, "healthy_success", run_id)
    monkeypatch.setattr(runs, "DETERMINISTIC_EVALUATORS", [_FirstEvaluator()])

    async def fail_persist_run_failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forced run failure persistence failure")

    monkeypatch.setattr(runs, "persist_run_failure", fail_persist_run_failure)

    response = await evaluation_client.post(f"/v1/runs/{run_id}/evaluate")

    assert response.status_code == 500
    assert await _evaluation_result_count(session, run_id) == 1
    assert await session.get(RunFailure, run_id) is None

    await _delete_run(session, run_id)


async def test_run_failure_overall_status_rejects_unknown_value(
    session: AsyncSession,
) -> None:
    run_id = "phase5-overall-status-check"
    await _create_run(session, "healthy_success", run_id)

    with pytest.raises(IntegrityError):
        await session.execute(
            insert(RunFailure).values(
                run_id=run_id,
                overall_status="unknown",
                primary_category=None,
                secondary_category=None,
                max_severity=None,
                classifier_version="1.0.0",
                updated_at=load_fixture("healthy_success").started_at,
            )
        )
        await session.commit()
    await session.rollback()

    await _delete_run(session, run_id)


class _PassingEvaluator(Evaluator):
    def evaluate(self, run: EvaluationRunView) -> EvaluationOutcome:
        return EvaluationOutcome(
            passed=True,
            score=1.0,
            label="pass",
            severity=None,
            reason=f"{run.run_id} passed",
            findings=[],
        )


class _NotApplicableEvaluator(Evaluator):
    name: ClassVar[str] = "not_applicable"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationOutcome:
        return EvaluationOutcome(
            passed=True,
            score=None,
            label="not_applicable",
            severity=None,
            reason=f"{run.run_id} not applicable",
            findings=[],
        )


class _StructuredFailureEvaluator(Evaluator):
    name: ClassVar[str] = "structured_failure"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationOutcome:
        return EvaluationOutcome(
            passed=False,
            score=None,
            label="fail",
            severity=None,
            reason=f"{run.run_id} failed structured output validation",
            findings=[
                {
                    "code": "empty_output",
                    "message": "Final result output is empty",
                    "data": {"run_id": run.run_id},
                }
            ],
        )


class _FirstEvaluator(_PassingEvaluator):
    name: ClassVar[str] = "first"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC


class _SecondEvaluator(_PassingEvaluator):
    name: ClassVar[str] = "second"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC


class _ThirdEvaluator(_PassingEvaluator):
    name: ClassVar[str] = "third"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC


class _FourthEvaluator(_PassingEvaluator):
    name: ClassVar[str] = "fourth"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC


class _ExplodingEvaluator(Evaluator):
    name: ClassVar[str] = "exploding"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.DETERMINISTIC

    def evaluate(self, run: EvaluationRunView) -> EvaluationOutcome:
        raise RuntimeError("forced evaluator failure")


class _AsyncEvaluator(Evaluator):
    name: ClassVar[str] = "async_judge"
    version: ClassVar[str] = "1.0.0"
    type: ClassVar[EvaluatorType] = EvaluatorType.LLM_BASED

    def evaluate(self, run: EvaluationRunView) -> EvaluationOutcome:
        raise AssertionError("LLM-based evaluator should use evaluate_async")

    async def evaluate_async(
        self, run: EvaluationRunView, call_log: list[Any]
    ) -> EvaluationOutcome:
        return EvaluationOutcome(
            passed=True,
            score=1.0,
            label="pass",
            severity=None,
            reason=f"{run.run_id} passed asynchronously",
            findings=[],
        )


async def _create_run(session: AsyncSession, fixture_name: str, run_id: str) -> None:
    await _delete_run(session, run_id)
    event = load_fixture(fixture_name)
    event.run_id = run_id
    await ingest_run_event(session, event)


async def _delete_run(session: AsyncSession, run_id: str) -> None:
    for model in cast(
        tuple[Any, ...],
        (RunFailure, EvaluationResult, LLMCall, ToolCallRecord, Span, AgentRun),
    ):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.commit()


async def _evaluation_result_count(session: AsyncSession, run_id: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(EvaluationResult)
        .where(EvaluationResult.run_id == run_id)
    )
    return int(count or 0)
