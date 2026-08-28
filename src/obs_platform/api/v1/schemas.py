from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


class APIResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunSummary(APIResponseModel):
    run_id: str
    scenario_id: str | None
    agent_name: str
    agent_version: str
    prompt_version: str
    environment: str
    status: RunStatus
    event_type: RunEventType
    hitl_state: HITLState
    started_at: datetime
    completed_at: datetime | None
    execution_latency_ms: int | None
    wall_clock_duration_ms: int | None
    usage_total_tokens: int
    usage_total_estimated_cost_usd: float


class RunListResponse(APIResponseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int


class ErrorResponse(APIResponseModel):
    category: str
    code: str | None
    message: str
    failed_component: str | None


class SpanResponse(APIResponseModel):
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
    error: ErrorResponse | None = Field(
        description="Populated when the span failed or errored."
    )


class ToolCallResponse(APIResponseModel):
    tool_call_id: str
    span_id: str
    tool_name: str
    sequence: int
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime
    latency_ms: int | None
    retry_count: int
    status: ExecutionStatus
    error: ErrorResponse | None = Field(
        description="Populated when the tool call failed or errored."
    )


class LLMCallResponse(APIResponseModel):
    llm_call_id: str
    span_id: str
    sequence: int
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
    error: ErrorResponse | None = Field(
        description="Populated when the LLM call failed or errored."
    )


class HITLResponse(APIResponseModel):
    required: bool
    state: HITLState
    checkpoint_id: str | None = Field(
        description="Populated for runs that reached a human approval checkpoint."
    )
    decision: Literal["approve", "reject"] | None = Field(
        description="Populated after a required human approval decision is recorded."
    )
    requested_at: datetime | None = Field(
        description="Populated when human approval was requested."
    )
    decided_at: datetime | None = Field(
        description="Populated after a required human approval decision is recorded."
    )
    pending_action: dict[str, Any] | None = Field(
        description="Populated while the run is awaiting human approval."
    )


class UsageResponse(APIResponseModel):
    total_llm_calls: int
    total_tool_calls: int
    total_tokens: int
    total_estimated_cost_usd: float
    total_retries: int


class FinalResultResponse(APIResponseModel):
    output: dict[str, Any]
    source_references: list[str]


class RunDetailResponse(APIResponseModel):
    run_id: str
    scenario_id: str | None
    agent_name: str
    agent_version: str
    prompt_version: str
    environment: str
    status: RunStatus
    event_type: RunEventType
    raw_input: Any
    normalized_input: str | None
    started_at: datetime
    completed_at: datetime | None
    execution_latency_ms: int | None
    wall_clock_duration_ms: int | None
    resume_count: int
    spans: list[SpanResponse]
    tool_calls: list[ToolCallResponse]
    llm_calls: list[LLMCallResponse]
    hitl: HITLResponse
    usage: UsageResponse
    final_result: FinalResultResponse | None = Field(
        description="Populated after a run reaches a final result."
    )
    runtime_error: ErrorResponse | None = Field(
        description="Populated when status is tool_error or runtime_error."
    )


class RunCounts(APIResponseModel):
    total: int
    by_status: dict[RunStatus, int]


class OverviewAnalyticsResponse(APIResponseModel):
    runtime_success_rate: float | None = Field(
        description="Populated when at least one terminal run is in scope."
    )
    avg_latency_ms: float | None = Field(
        description="Populated when at least one run has execution latency in scope."
    )
    p95_latency_ms: float | None = Field(
        description="Populated when at least one run has execution latency in scope."
    )
    usage_total_tokens: int
    usage_total_estimated_cost_usd: float
    run_counts: RunCounts


class ToolStats(APIResponseModel):
    tool_name: str
    call_count: int
    success_count: int
    failure_count: int
    error_count: int
    failure_rate: float
    avg_latency_ms: float | None = Field(
        description="Populated when at least one call for the tool has latency."
    )
    p95_latency_ms: float | None = Field(
        description="Populated when at least one call for the tool has latency."
    )


class ToolAnalyticsResponse(APIResponseModel):
    items: list[ToolStats]
