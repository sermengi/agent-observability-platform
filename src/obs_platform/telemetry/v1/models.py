from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


class TelemetryModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="after")
    @classmethod
    def require_utc_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("datetime fields must be timezone-aware UTC")
            if value.utcoffset() != UTC.utcoffset(value):
                raise ValueError("datetime fields must be timezone-aware UTC")
        return value


class ErrorInfo(TelemetryModel):
    category: str
    code: str | None
    message: str
    failed_component: str | None


class Span(TelemetryModel):
    span_id: str
    parent_span_id: str | None
    name: str
    sequence: int
    started_at: datetime
    completed_at: datetime | None
    status: ExecutionStatus
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    metadata: dict[str, Any] | None
    error: ErrorInfo | None


class ToolCall(TelemetryModel):
    tool_call_id: str
    span_id: str
    tool_name: str
    sequence: int
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime
    latency_ms: int | None
    retry_count: int = 0
    status: ExecutionStatus
    error: ErrorInfo | None


class LLMCall(TelemetryModel):
    llm_call_id: str
    span_id: str
    call_type: LLMCallType
    model: str
    provider: str
    started_at: datetime
    completed_at: datetime
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost_usd: float | None
    input_payload: dict[str, Any] | None
    output_payload: dict[str, Any] | None
    status: ExecutionStatus
    error: ErrorInfo | None


class HITLInfo(TelemetryModel):
    required: bool
    state: HITLState
    checkpoint_id: str | None
    decision: Literal["approve", "reject"] | None
    requested_at: datetime | None
    decided_at: datetime | None
    pending_action: dict[str, Any] | None


class UsageSummary(TelemetryModel):
    total_llm_calls: int
    total_tool_calls: int
    total_tokens: int
    total_estimated_cost_usd: float
    total_retries: int


class FinalResult(TelemetryModel):
    output: dict[str, Any]
    source_references: list[str] = Field(default_factory=list)


class ExtendedRunEvent(TelemetryModel):
    schema_version: Literal["1.0"]
    event_type: RunEventType
    run_id: str
    agent_name: str
    agent_version: str
    prompt_version: str
    environment: str
    raw_input: str
    normalized_input: str | None
    scenario_id: str | None
    started_at: datetime
    completed_at: datetime | None
    status: RunStatus
    execution_latency_ms: int | None
    wall_clock_duration_ms: int | None
    resume_count: int = 0
    spans: list[Span]
    tool_calls: list[ToolCall]
    llm_calls: list[LLMCall]
    hitl: HITLInfo
    usage: UsageSummary
    final_result: FinalResult | None
    runtime_error: ErrorInfo | None
