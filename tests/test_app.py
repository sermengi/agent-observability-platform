from collections.abc import AsyncIterator
from types import UnionType
from typing import Any, cast, get_args, get_origin

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from obs_platform.api.v1 import schemas
from obs_platform.main import create_app
from obs_platform.routes import analytics, health, runs
from obs_platform.telemetry.v1 import load_fixture
from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


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
    assert "/v1/analytics/tools" in paths
    assert "/v1/analytics/usage" in paths
    assert "/v1/runs" in paths
    assert "/v1/health" not in paths
    assert "/runs" not in paths
    assert set(paths["/health"]) == {"get"}
    assert set(paths["/v1/analytics/overview"]) == {"get"}
    assert set(paths["/v1/analytics/tools"]) == {"get"}
    assert set(paths["/v1/analytics/usage"]) == {"get"}
    assert set(paths["/v1/runs"]) == {"get", "post"}


async def test_route_modules_expose_separate_routers() -> None:
    assert isinstance(health.router, APIRouter)
    assert isinstance(analytics.router, APIRouter)
    assert isinstance(runs.router, APIRouter)
    assert health.router is not runs.router
    assert analytics.router is not runs.router


def test_phase_3_get_routes_declare_explicit_response_models() -> None:
    phase_3_routes = {
        ("GET", "/runs"),
        ("GET", "/runs/{run_id}"),
        ("GET", "/analytics/overview"),
        ("GET", "/analytics/tools"),
        ("GET", "/analytics/usage"),
    }

    route_models = {
        (method, route.path): route.response_model
        for router in (runs.router, analytics.router)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in getattr(route, "methods", set())
        if (method, route.path) in phase_3_routes
    }

    assert route_models == {
        ("GET", "/runs"): schemas.RunListResponse,
        ("GET", "/runs/{run_id}"): schemas.RunDetailResponse,
        ("GET", "/analytics/overview"): schemas.OverviewAnalyticsResponse,
        ("GET", "/analytics/tools"): schemas.ToolAnalyticsResponse,
        ("GET", "/analytics/usage"): schemas.UsageAnalyticsResponse,
    }


def test_phase_3_response_models_live_in_api_schema_module() -> None:
    response_models = [
        schemas.RunSummary,
        schemas.RunListResponse,
        schemas.RunDetailResponse,
        schemas.SpanResponse,
        schemas.ToolCallResponse,
        schemas.LLMCallResponse,
        schemas.HITLResponse,
        schemas.UsageResponse,
        schemas.OverviewAnalyticsResponse,
        schemas.ToolAnalyticsResponse,
        schemas.ToolStats,
        schemas.UsageAnalyticsResponse,
        schemas.ModelUsageBreakdown,
        schemas.CallTypeUsageBreakdown,
    ]

    assert all(
        model.__module__ == "obs_platform.api.v1.schemas"
        for model in response_models
    )


def test_phase_3_response_models_reuse_telemetry_enums() -> None:
    assert schemas.RunSummary.model_fields["status"].annotation is RunStatus
    assert schemas.RunSummary.model_fields["event_type"].annotation is RunEventType
    assert schemas.RunSummary.model_fields["hitl_state"].annotation is HITLState
    assert schemas.SpanResponse.model_fields["status"].annotation is ExecutionStatus
    assert schemas.ToolCallResponse.model_fields["status"].annotation is ExecutionStatus
    assert schemas.LLMCallResponse.model_fields["status"].annotation is ExecutionStatus
    assert schemas.LLMCallResponse.model_fields["call_type"].annotation is LLMCallType
    assert schemas.HITLResponse.model_fields["state"].annotation is HITLState
    assert _optional_value_type(
        schemas.OverviewAnalyticsResponse.model_fields[
            "runtime_success_rate"
        ].annotation
    ) is float
    assert schemas.CallTypeUsageBreakdown.model_fields[
        "call_type"
    ].annotation is LLMCallType


def test_api_response_models_share_from_attributes_base() -> None:
    for model in schemas.APIResponseModel.__subclasses__():
        assert issubclass(model, BaseModel)
        assert model.model_config["from_attributes"] is True


def test_openapi_documents_conditionally_nullable_fields() -> None:
    components = create_app().openapi()["components"]["schemas"]
    fields = {
        "SpanResponse": ["error"],
        "ToolCallResponse": ["error"],
        "LLMCallResponse": ["error"],
        "HITLResponse": [
            "checkpoint_id",
            "decision",
            "requested_at",
            "decided_at",
            "pending_action",
        ],
        "RunDetailResponse": ["final_result", "runtime_error"],
        "OverviewAnalyticsResponse": [
            "runtime_success_rate",
            "avg_latency_ms",
            "p95_latency_ms",
        ],
        "ToolStats": ["avg_latency_ms", "p95_latency_ms"],
    }

    for schema_name, field_names in fields.items():
        properties = components[schema_name]["properties"]
        for field_name in field_names:
            assert properties[field_name]["description"]


def test_ci_does_not_add_openapi_schema_diff_or_snapshot_testing() -> None:
    with open(".github/workflows/ci.yml", encoding="utf-8") as workflow_file:
        workflow = workflow_file.read().lower()

    assert "openapi" not in workflow
    assert "snapshot" not in workflow
    assert "schema diff" not in workflow


def _optional_value_type(annotation: object) -> object:
    assert get_origin(annotation) is UnionType
    value_types = [arg for arg in get_args(annotation) if arg is not type(None)]
    assert len(value_types) == 1
    return value_types[0]
