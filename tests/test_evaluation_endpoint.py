from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
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
from obs_platform.evaluation.types import EvaluationResult as EvaluationOutcome
from obs_platform.evaluation.types import (
    EvaluationRunView,
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
        transport=ASGITransport(app=app),
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
