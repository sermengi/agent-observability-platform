from httpx import AsyncClient

from obs_platform.main import create_app


async def test_health_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


async def test_invalid_run_returns_422(client: AsyncClient) -> None:
    response = await client.post("/v1/runs", json={"phase": 0, "payload": {}})

    assert response.status_code == 422


async def test_create_app() -> None:
    app = create_app()
    paths = app.openapi()["paths"]

    assert "/health" in paths
    assert "/v1/runs" in paths
