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
    completed_at: datetime | None = Field(
        description="Populated once the run reaches a terminal state."
    )
    execution_latency_ms: int | None = Field(
        description="Populated once the run reaches a terminal state."
    )
    wall_clock_duration_ms: int | None = Field(
        description="Populated once the run reaches a terminal state."
    )
    usage_total_tokens: int
    usage_total_estimated_cost_usd: float
    overall_status: str | None
    primary_failure_type: str | None
    max_severity: str | None


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
    completed_at: datetime | None = Field(
        description="Populated once the span completes."
    )
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
    latency_ms: int | None = Field(
        description="Populated once the tool call completes."
    )
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
    latency_ms: int | None = Field(description="Populated once the LLM call completes.")
    prompt_tokens: int | None = Field(
        description="Populated once the LLM call completes."
    )
    completion_tokens: int | None = Field(
        description="Populated once the LLM call completes."
    )
    total_tokens: int | None = Field(
        description="Populated once the LLM call completes."
    )
    estimated_cost_usd: float | None = Field(
        description="Populated once the LLM call completes."
    )
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


class EvaluatorResultSummary(APIResponseModel):
    evaluator_name: str
    evaluator_version: str
    execution_status: str
    passed: bool | None
    score: float | None
    label: str | None
    severity: str | None
    reason: str | None
    findings: list[dict[str, Any]]


class RunFailureSummary(APIResponseModel):
    overall_status: str
    primary_failure_type: str | None
    secondary_failure_type: str | None
    max_severity: str | None
    classifier_version: str
    updated_at: datetime


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
    completed_at: datetime | None = Field(
        description="Populated once the run reaches a terminal state."
    )
    execution_latency_ms: int | None = Field(
        description="Populated once the run reaches a terminal state."
    )
    wall_clock_duration_ms: int | None = Field(
        description="Populated once the run reaches a terminal state."
    )
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
    failure: RunFailureSummary | None
    evaluation_summary: list[EvaluatorResultSummary] | None


class RunFailureResponse(APIResponseModel):
    primary_category: str | None
    secondary_category: str | None
    max_severity: str | None


class EvaluationTriggerResponse(APIResponseModel):
    run_id: str
    overall_status: str
    evaluator_results: list[EvaluatorResultSummary]
    failure: RunFailureResponse | None
    evaluated_at: datetime


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


class UsageTotals(APIResponseModel):
    call_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    total_estimated_cost_usd: float


class ModelUsageBreakdown(UsageTotals):
    provider: str
    model: str


class CallTypeUsageBreakdown(UsageTotals):
    call_type: LLMCallType


class UsageAnalyticsResponse(APIResponseModel):
    total: UsageTotals
    by_model: list[ModelUsageBreakdown]
    by_call_type: list[CallTypeUsageBreakdown]
