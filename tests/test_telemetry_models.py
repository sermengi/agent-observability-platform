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


def minimal_event_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "event_type": RunEventType.RUN_FINAL,
        "run_id": "run-001",
        "agent_name": "maintenance-agent",
        "agent_version": "0.1.0",
        "prompt_version": "prompt-001",
        "environment": "test",
        "raw_input": "Inspect PUMP-101",
        "normalized_input": None,
        "scenario_id": None,
        "started_at": aware_now(),
        "completed_at": aware_now(),
        "status": RunStatus.SUCCESS,
        "execution_latency_ms": 1200,
        "wall_clock_duration_ms": 1250,
        "resume_count": 0,
        "spans": [],
        "tool_calls": [],
        "llm_calls": [],
        "hitl": HITLInfo(
            required=False,
            state=HITLState.NOT_REQUIRED,
            checkpoint_id=None,
            decision=None,
            requested_at=None,
            decided_at=None,
            pending_action=None,
        ),
        "usage": UsageSummary(
            total_llm_calls=0,
            total_tool_calls=0,
            total_tokens=0,
            total_estimated_cost_usd=0.0,
            total_retries=0,
        ),
        "final_result": FinalResult(output={"answer": "ok"}),
        "runtime_error": None,
    }
    values.update(overrides)
    return values


def span(**overrides: object) -> Span:
    values: dict[str, object] = {
        "span_id": "span-root",
        "parent_span_id": None,
        "name": "root",
        "sequence": 1,
        "started_at": aware_now(),
        "completed_at": aware_now(),
        "status": ExecutionStatus.SUCCESS,
        "input": None,
        "output": None,
        "metadata": None,
        "error": None,
    }
    values.update(overrides)
    return Span.model_validate(values)


def tool_call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "tool_call_id": "tool-root",
        "span_id": "span-root",
        "tool_name": "resolve_asset",
        "sequence": 1,
        "arguments": {},
        "result": {},
        "started_at": aware_now(),
        "completed_at": aware_now(),
        "latency_ms": 50,
        "retry_count": 0,
        "status": ExecutionStatus.SUCCESS,
        "error": None,
    }
    values.update(overrides)
    return ToolCall.model_validate(values)


def llm_call(**overrides: object) -> LLMCall:
    values: dict[str, object] = {
        "llm_call_id": "llm-root",
        "span_id": "span-root",
        "call_type": LLMCallType.INTERPRETATION,
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
        "started_at": aware_now(),
        "completed_at": aware_now(),
        "latency_ms": 500,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "estimated_cost_usd": 0.000105,
        "input_payload": {},
        "output_payload": {},
        "status": ExecutionStatus.SUCCESS,
        "error": None,
    }
    values.update(overrides)
    return LLMCall.model_validate(values)


def test_v1_contract_models_import_from_public_package() -> None:
    assert ExtendedRunEvent.__module__ == "obs_platform.telemetry.v1.models"
    assert Span.__module__ == "obs_platform.telemetry.v1.models"
    assert ToolCall.__module__ == "obs_platform.telemetry.v1.models"
    assert LLMCall.__module__ == "obs_platform.telemetry.v1.models"


def test_extended_run_event_schema_version_is_fixed_literal() -> None:
    schema_version_field = ExtendedRunEvent.model_fields["schema_version"]

    assert get_args(schema_version_field.annotation) == ("1.0",)


def test_extended_run_event_documents_snapshot_semantics() -> None:
    docstring = ExtendedRunEvent.__doc__

    assert docstring is not None
    assert "snapshot" in docstring.lower()
    assert "not an event-sourcing event" in docstring.lower()
    assert "hitl_pending" in docstring
    assert "hitl_approved" in docstring
    assert "snapshot" in ExtendedRunEvent.model_json_schema()["description"].lower()


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
    event = ExtendedRunEvent.model_validate(minimal_event_kwargs())

    assert event.schema_version == "1.0"
    assert event.final_result is not None


def test_llm_call_type_uses_shared_enum_values() -> None:
    assert {member.value for member in LLMCallType} == {
        "interpretation",
        "evidence_gathering",
        "synthesis",
    }


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (ExtendedRunEvent, "run_id"),
        (Span, "span_id"),
        (ToolCall, "tool_call_id"),
        (LLMCall, "llm_call_id"),
    ],
)
@pytest.mark.parametrize("bad_id", ["", "   "])
def test_id_fields_reject_empty_and_whitespace_only_strings(
    model: type[ExtendedRunEvent] | type[Span] | type[ToolCall] | type[LLMCall],
    field_name: str,
    bad_id: str,
) -> None:
    payload_by_model: dict[type[object], dict[str, object]] = {
        ExtendedRunEvent: minimal_event_kwargs(),
        Span: span().model_dump(),
        ToolCall: tool_call().model_dump(),
        LLMCall: llm_call().model_dump(),
    }
    payload = payload_by_model[model]
    payload[field_name] = bad_id

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (ExtendedRunEvent, "run_id"),
        (Span, "span_id"),
        (ToolCall, "tool_call_id"),
        (LLMCall, "llm_call_id"),
    ],
)
def test_id_fields_are_stripped(
    model: type[ExtendedRunEvent] | type[Span] | type[ToolCall] | type[LLMCall],
    field_name: str,
) -> None:
    payload_by_model: dict[type[object], dict[str, object]] = {
        ExtendedRunEvent: minimal_event_kwargs(),
        Span: span().model_dump(),
        ToolCall: tool_call().model_dump(),
        LLMCall: llm_call().model_dump(),
    }
    payload = payload_by_model[model]
    payload[field_name] = "  stable-id  "

    instance = model.model_validate(payload)

    assert getattr(instance, field_name) == "stable-id"


@pytest.mark.parametrize(
    ("model", "field_name"),
    [
        (ExtendedRunEvent, "run_id"),
        (Span, "span_id"),
        (ToolCall, "tool_call_id"),
        (LLMCall, "llm_call_id"),
    ],
)
def test_id_fields_reject_strings_longer_than_256_characters(
    model: type[ExtendedRunEvent] | type[Span] | type[ToolCall] | type[LLMCall],
    field_name: str,
) -> None:
    payload_by_model: dict[type[object], dict[str, object]] = {
        ExtendedRunEvent: minimal_event_kwargs(),
        Span: span().model_dump(),
        ToolCall: tool_call().model_dump(),
        LLMCall: llm_call().model_dump(),
    }
    payload = payload_by_model[model]
    payload[field_name] = "x" * 257

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_run_id_is_required() -> None:
    payload = minimal_event_kwargs()
    del payload["run_id"]

    with pytest.raises(ValidationError):
        ExtendedRunEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "overrides"),
    [
        ("span_id", {"spans": [span(), span(sequence=2)]}),
        ("tool_call_id", {"spans": [span()], "tool_calls": [tool_call(), tool_call()]}),
        ("llm_call_id", {"spans": [span()], "llm_calls": [llm_call(), llm_call()]}),
    ],
)
def test_child_ids_must_be_unique_within_run(
    field_name: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        ExtendedRunEvent.model_validate(minimal_event_kwargs(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"spans": [span(span_id="span-child", parent_span_id="span-missing")]},
        {"spans": [span()], "tool_calls": [tool_call(span_id="span-missing")]},
        {"spans": [span()], "llm_calls": [llm_call(span_id="span-missing")]},
    ],
)
def test_child_references_must_point_to_spans(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="span_id"):
        ExtendedRunEvent.model_validate(minimal_event_kwargs(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": RunEventType.RUN_FINAL, "completed_at": None},
        {
            "event_type": RunEventType.RUN_AWAITING_APPROVAL,
            "status": RunStatus.AWAITING_APPROVAL,
            "completed_at": aware_now(),
            "final_result": None,
            "hitl": HITLInfo(
                required=True,
                state=HITLState.PENDING,
                checkpoint_id="checkpoint-001",
                decision=None,
                requested_at=aware_now(),
                decided_at=None,
                pending_action={"draft": {"priority": "high"}},
            ),
        },
    ],
)
def test_completed_at_matches_event_type(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="completed_at"):
        ExtendedRunEvent.model_validate(minimal_event_kwargs(**overrides))


def test_successful_final_run_requires_final_result() -> None:
    with pytest.raises(ValidationError, match="final_result"):
        ExtendedRunEvent.model_validate(minimal_event_kwargs(final_result=None))


@pytest.mark.parametrize("status", [RunStatus.TOOL_ERROR, RunStatus.RUNTIME_ERROR])
def test_error_final_run_requires_runtime_error(status: RunStatus) -> None:
    with pytest.raises(ValidationError, match="runtime_error"):
        ExtendedRunEvent.model_validate(
            minimal_event_kwargs(status=status, final_result=None)
        )


def test_pending_hitl_requires_pending_action() -> None:
    with pytest.raises(ValidationError, match="pending_action"):
        ExtendedRunEvent.model_validate(
            minimal_event_kwargs(
                event_type=RunEventType.RUN_AWAITING_APPROVAL,
                status=RunStatus.AWAITING_APPROVAL,
                completed_at=None,
                final_result=None,
                hitl=HITLInfo(
                    required=True,
                    state=HITLState.PENDING,
                    checkpoint_id="checkpoint-001",
                    decision=None,
                    requested_at=aware_now(),
                    decided_at=None,
                    pending_action=None,
                ),
            )
        )


def test_awaiting_approval_requires_pending_status_and_hitl_state() -> None:
    with pytest.raises(ValidationError, match="awaiting approval"):
        ExtendedRunEvent.model_validate(
            minimal_event_kwargs(
                event_type=RunEventType.RUN_AWAITING_APPROVAL,
                status=RunStatus.SUCCESS,
                completed_at=None,
                final_result=None,
                hitl=HITLInfo(
                    required=True,
                    state=HITLState.PENDING,
                    checkpoint_id="checkpoint-001",
                    decision=None,
                    requested_at=aware_now(),
                    decided_at=None,
                    pending_action={"draft": {"priority": "high"}},
                ),
            )
        )


def test_final_hitl_approved_run_requires_success_status_and_final_result() -> None:
    event = ExtendedRunEvent.model_validate(
        minimal_event_kwargs(
            hitl=HITLInfo(
                required=True,
                state=HITLState.APPROVED,
                checkpoint_id="checkpoint-001",
                decision="approve",
                requested_at=aware_now(),
                decided_at=aware_now(),
                pending_action=None,
            ),
            resume_count=1,
        )
    )

    assert event.status is RunStatus.SUCCESS
    assert event.hitl.required is True
    assert event.hitl.state is HITLState.APPROVED


def test_no_hitl_run_keeps_non_nullable_not_required_snapshot() -> None:
    event = ExtendedRunEvent.model_validate(minimal_event_kwargs())

    assert event.hitl.required is False
    assert event.hitl.state is HITLState.NOT_REQUIRED
    assert event.hitl.pending_action is None
