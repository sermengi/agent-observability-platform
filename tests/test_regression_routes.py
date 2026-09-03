from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseOnlySettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, RegressionRun
from obs_platform.evaluation.contracts import ScenarioContract
from obs_platform.main import create_app
from obs_platform.regressions.persistence import create_regression_run
from obs_platform.regressions.runner import AgentTarget
from obs_platform.routes import regressions
from obs_platform.telemetry.v1.models import ExtendedRunEvent


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(DatabaseOnlySettings().db)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        await db_session.execute(
            delete(RegressionRun).where(RegressionRun.name == "task8 endpoint test")
        )
        await db_session.commit()
        yield db_session
        await db_session.execute(
            delete(RegressionRun).where(RegressionRun.name == "task8 endpoint test")
        )
        await db_session.commit()
    await engine.dispose()


async def test_regression_routes_create_default_list_and_read_detail(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*args: object) -> None:
        return None

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(regressions, "_execute_regression", execute)
    app = create_app()
    app.dependency_overrides[regressions.get_session] = get_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/v1/regressions",
            json={
                "name": "task8 endpoint test",
                "agent_model_provider": "mock",
                "agent_model_name": "agent",
                "prompt_version": "v1",
            },
        )
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "pending"
        assert body["repetitions"] == 5
        assert len(body["scenario_ids"]) == 8

        listed = await client.get("/v1/regressions")
        assert listed.status_code == 200
        assert body in listed.json()
        assert "aggregation" not in listed.json()[0]

        detail = await client.get(f"/v1/regressions/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["id"] == body["id"]
        assert detail.json()["aggregation"]["overall"]["counts"] == {
            "pass": 0,
            "fail": 0,
            "incomplete": 0,
        }


async def test_regression_routes_reject_duplicate_baseline_and_unknown_detail(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*args: object) -> None:
        return None

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(regressions, "_execute_regression", execute)
    app = create_app()
    app.dependency_overrides[regressions.get_session] = get_session
    payload = {
        "name": "task8 endpoint test",
        "agent_model_provider": "mock",
        "agent_model_name": "agent",
        "prompt_version": "v1",
        "is_baseline": True,
        "scenario_ids": ["GS-DEBUG-SMOKE-01"],
        "repetitions": 1,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/v1/regressions", json=payload)).status_code == 202
        assert (await client.post("/v1/regressions", json=payload)).status_code == 409
        assert (await client.get("/v1/regressions/999999")).status_code == 404


async def test_background_execution_continues_after_each_failed_repetition(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FailingTarget()
    regression = await create_regression_run(
        session,
        name="task8 endpoint test",
        agent_version="test",
        agent_model_provider="mock",
        agent_model_name="agent",
        prompt_version="v1",
        scenario_ids=["GS-DEBUG-SMOKE-01"],
        repetitions=2,
    )
    engine = create_engine(DatabaseOnlySettings().db)
    monkeypatch.setattr(regressions, "_mock_target", lambda: target)
    try:
        await regressions._execute_regression(engine, regression.id)
    finally:
        await engine.dispose()

    completed = await session.get_one(RegressionRun, regression.id)
    await session.refresh(completed)
    assert completed.status == "completed"
    assert target.calls == ["GS-DEBUG-SMOKE-01", "GS-DEBUG-SMOKE-01"]
    assert await session.scalar(
        select(AgentRun).where(AgentRun.regression_run_id == regression.id)
    ) is None


class FailingTarget(AgentTarget):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_scenario(self, contract: ScenarioContract) -> ExtendedRunEvent:
        self.calls.append(contract.scenario_id)
        raise RuntimeError("simulated target failure")

    async def resume_after_approval(
        self,
        checkpoint_id: str,
        decision: str,
    ) -> ExtendedRunEvent:
        raise AssertionError("HITL is not expected")
