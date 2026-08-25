from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from obs_platform.main import create_app
from obs_platform.routes import health, runs


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


async def test_placeholder_run_returns_501_for_arbitrary_json() -> None:
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/v1/runs",
            json={"anything": ["can", "arrive"], "phase": 0},
        )

    assert response.status_code == 501
    body = response.json()
    assert set(body) == {"detail"}
    assert body["detail"]


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
    assert "/v1/runs" in paths
    assert "/v1/health" not in paths
    assert "/runs" not in paths
    assert set(paths["/health"]) == {"get"}
    assert set(paths["/v1/runs"]) == {"post"}


async def test_route_modules_expose_separate_routers() -> None:
    assert isinstance(health.router, APIRouter)
    assert isinstance(runs.router, APIRouter)
    assert health.router is not runs.router
