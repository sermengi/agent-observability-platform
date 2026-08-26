from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from obs_platform.telemetry.v1 import (
    ErrorInfo,
    ExecutionStatus,
    ExtendedRunEvent,
    FinalResult,
    HITLInfo,
    HITLState,
    LLMCall,
    LLMCallType,
    RunEventType,
    RunStatus,
    Span,
    ToolCall,
    UsageSummary,
)


def aware_now() -> datetime:
    return datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_v1_contract_models_import_from_public_package() -> None:
    assert ExtendedRunEvent.__module__ == "obs_platform.telemetry.v1.models"
    assert Span.__module__ == "obs_platform.telemetry.v1.models"
    assert ToolCall.__module__ == "obs_platform.telemetry.v1.models"
    assert LLMCall.__module__ == "obs_platform.telemetry.v1.models"


def test_extended_run_event_schema_version_is_fixed_literal() -> None:
    schema_version_field = ExtendedRunEvent.model_fields["schema_version"]

    assert get_args(schema_version_field.annotation) == ("1.0",)


def test_unknown_fields_are_ignored_on_contract_models() -> None:
    span = Span(
        span_id="span-custom",
        parent_span_id=None,
        name="arbitrary planning step",
        sequence=1,
        started_at=aware_now(),
        completed_at=aware_now(),
        status=ExecutionStatus.SUCCESS,
        input=None,
        output=None,
        metadata=None,
        error=None,
        unexpected_wire_field="producer-added-value",
    )

    assert not hasattr(span, "unexpected_wire_field")


def test_execution_entities_share_execution_status_enum() -> None:
    assert Span.model_fields["status"].annotation is ExecutionStatus
    assert ToolCall.model_fields["status"].annotation is ExecutionStatus
    assert LLMCall.model_fields["status"].annotation is ExecutionStatus


def test_execution_entities_share_error_info_model() -> None:
    assert Span.model_fields["error"].annotation == ErrorInfo | None
    assert ToolCall.model_fields["error"].annotation == ErrorInfo | None
    assert LLMCall.model_fields["error"].annotation == ErrorInfo | None
    assert ExtendedRunEvent.model_fields["runtime_error"].annotation == ErrorInfo | None


def test_span_name_accepts_free_text() -> None:
    span = Span(
        span_id="span-free-form",
        parent_span_id=None,
        name="operator-defined graph node that is not in any enum",
        sequence=1,
        started_at=aware_now(),
        completed_at=aware_now(),
        status=ExecutionStatus.SUCCESS,
        input={"asset_id": "PUMP-101"},
        output={"next": "continue"},
        metadata={"source": "test"},
        error=None,
    )

    assert span.name == "operator-defined graph node that is not in any enum"


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Span(
            span_id="span-naive",
            parent_span_id=None,
            name="bad timestamp",
            sequence=1,
            started_at=datetime(2026, 8, 26, 12, 0),
            completed_at=None,
            status=ExecutionStatus.SUCCESS,
            input=None,
            output=None,
            metadata=None,
            error=None,
        )


def test_minimal_extended_run_event_validates() -> None:
    event = ExtendedRunEvent(
        schema_version="1.0",
        event_type=RunEventType.RUN_FINAL,
        run_id="run-001",
        agent_name="maintenance-agent",
        agent_version="0.1.0",
        prompt_version="prompt-001",
        environment="test",
        raw_input="Inspect PUMP-101",
        normalized_input=None,
        scenario_id=None,
        started_at=aware_now(),
        completed_at=aware_now(),
        status=RunStatus.SUCCESS,
        execution_latency_ms=1200,
        wall_clock_duration_ms=1250,
        resume_count=0,
        spans=[],
        tool_calls=[],
        llm_calls=[],
        hitl=HITLInfo(
            required=False,
            state=HITLState.NOT_REQUIRED,
            checkpoint_id=None,
            decision=None,
            requested_at=None,
            decided_at=None,
            pending_action=None,
        ),
        usage=UsageSummary(
            total_llm_calls=0,
            total_tool_calls=0,
            total_tokens=0,
            total_estimated_cost_usd=0.0,
            total_retries=0,
        ),
        final_result=FinalResult(output={"answer": "ok"}),
        runtime_error=None,
    )

    assert event.schema_version == "1.0"
    assert event.final_result is not None


def test_llm_call_type_uses_shared_enum_values() -> None:
    assert {member.value for member in LLMCallType} == {
        "interpretation",
        "evidence_gathering",
        "synthesis",
    }
