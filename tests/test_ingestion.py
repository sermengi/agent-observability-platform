from collections.abc import AsyncIterator
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obs_platform.config import DatabaseSettings
from obs_platform.database import create_engine
from obs_platform.db.models import AgentRun, LLMCall, Span, ToolCall
from obs_platform.ingestion.runs import ingest_run_event
from obs_platform.main import create_app
from obs_platform.routes import runs
from obs_platform.telemetry.v1 import ExtendedRunEvent, load_fixture


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine(
        DatabaseSettings(
            host="localhost",
            port=5432,
            user="observability",
            password="change-me",
            name="observability",
        )
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def test_ingest_run_event_can_be_called_directly_with_async_session(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task3-direct-call")

    await _delete_run(session, event.run_id)
    result = await ingest_run_event(session, event)

    assert result.run_id == event.run_id
    assert result.event_type == event.event_type.value
    assert result.status == event.status.value
    assert await _count_for_run(session, AgentRun, event.run_id) == 1
    assert await _count_for_run(session, Span, event.run_id) == len(event.spans)
    assert await _count_for_run(session, ToolCall, event.run_id) == len(
        event.tool_calls
    )
    assert await _count_for_run(session, LLMCall, event.run_id) == len(event.llm_calls)

    await _delete_run(session, event.run_id)


async def test_post_runs_valid_fixture_returns_lifecycle_echo(
    session: AsyncSession,
) -> None:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield session

    event = _fixture_with_run_id("healthy_success", "task3-http-ingestion")
    await _delete_run(session, event.run_id)
    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/runs",
            json=event.model_dump(mode="json"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": event.run_id,
        "event_type": event.event_type.value,
        "status": event.status.value,
    }

    await _delete_run(session, event.run_id)


async def test_ingest_run_event_rolls_back_when_child_normalization_fails(
    session: AsyncSession,
) -> None:
    event = _fixture_with_run_id("healthy_success", "task3-rollback")
    event.tool_calls[0].span_id = "missing-after-validation"

    await _delete_run(session, event.run_id)
    with pytest.raises(KeyError):
        await ingest_run_event(session, event)

    assert await _count_for_run(session, AgentRun, event.run_id) == 0
    assert await _count_for_run(session, Span, event.run_id) == 0
    assert await _count_for_run(session, ToolCall, event.run_id) == 0
    assert await _count_for_run(session, LLMCall, event.run_id) == 0


def _fixture_with_run_id(name: str, run_id: str) -> ExtendedRunEvent:
    event = load_fixture(name)
    event.run_id = run_id
    return event


async def _count_for_run(
    session: AsyncSession,
    model: type[AgentRun] | type[Span] | type[ToolCall] | type[LLMCall],
    run_id: str,
) -> int:
    count = await session.scalar(select(func.count()).where(model.run_id == run_id))
    return cast(int, count)


async def _delete_run(session: AsyncSession, run_id: str) -> None:
    for model in (LLMCall, ToolCall, Span, AgentRun):
        await session.execute(delete(model).where(model.run_id == run_id))
    await session.commit()
