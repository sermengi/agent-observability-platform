from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.main import create_app
from obs_platform.routes import analytics, health, runs
from obs_platform.telemetry.v1 import load_fixture


async def test_health_returns_ok_with_injected_check() -> None:
    async def check_database() -> None:
        return None

    app = create_app(check_database=check_database)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


async def test_unstructured_run_payload_returns_422() -> None:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/runs",
            json={"anything": ["can", "arrive"], "phase": 0},
        )

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"]


async def test_ingest_run_returns_200_for_valid_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ingest_run_event(
        session: AsyncSession,
        event: Any,
    ) -> runs.IngestRunResponse:
        return runs.IngestRunResponse(
            run_id=event.run_id,
            event_type=event.event_type.value,
            status=event.status.value,
        )

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    monkeypatch.setattr(runs, "ingest_run_event", ingest_run_event)
    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    payload = load_fixture("healthy_success").model_dump(mode="json")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/v1/runs", json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "run_id": payload["run_id"],
        "event_type": payload["event_type"],
        "status": payload["status"],
    }


async def test_ingest_run_missing_required_field_returns_default_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    payload = load_fixture("healthy_success").model_dump(mode="json")
    payload.pop("run_id")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/v1/runs", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"}
    assert any(error["loc"] == ["body", "run_id"] for error in body["detail"])


async def test_ingest_run_ignores_extra_payload_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_event = None

    async def ingest_run_event(
        session: AsyncSession,
        event: Any,
    ) -> runs.IngestRunResponse:
        nonlocal seen_event
        seen_event = event
        return runs.IngestRunResponse(
            run_id=event.run_id,
            event_type=event.event_type.value,
            status=event.status.value,
        )

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    monkeypatch.setattr(runs, "ingest_run_event", ingest_run_event)
    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    payload = load_fixture("healthy_success").model_dump(mode="json")
    payload["unexpected_extra"] = "ignored"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/v1/runs", json=payload)

    assert response.status_code == 200
    assert seen_event is not None
    assert not hasattr(seen_event, "unexpected_extra")


async def test_ingest_run_db_error_surfaces_as_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ingest_run_event(session: AsyncSession, event: Any) -> None:
        raise RuntimeError("database broke")

    async def get_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    monkeypatch.setattr(runs, "ingest_run_event", ingest_run_event)
    app = create_app()
    app.dependency_overrides[runs.get_session] = get_session
    payload = load_fixture("healthy_success").model_dump(mode="json")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/v1/runs", json=payload)

    assert response.status_code == 500


async def test_create_app_returns_distinct_fastapi_instances() -> None:
    first = create_app()
    second = create_app()

    first.state.marker = "first"

    assert isinstance(first, FastAPI)
    assert isinstance(second, FastAPI)
    assert first is not second
    assert not hasattr(second.state, "marker")


async def test_routes_are_registered_with_expected_prefixes() -> None:
    app = create_app()
    paths = app.openapi()["paths"]

    assert "/health" in paths
    assert "/v1/analytics/overview" in paths
    assert "/v1/runs" in paths
    assert "/v1/health" not in paths
    assert "/runs" not in paths
    assert set(paths["/health"]) == {"get"}
    assert set(paths["/v1/analytics/overview"]) == {"get"}
    assert set(paths["/v1/runs"]) == {"get", "post"}


async def test_route_modules_expose_separate_routers() -> None:
    assert isinstance(health.router, APIRouter)
    assert isinstance(analytics.router, APIRouter)
    assert isinstance(runs.router, APIRouter)
    assert health.router is not runs.router
    assert analytics.router is not runs.router
