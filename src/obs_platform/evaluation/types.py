from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluatorType(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM_BASED = "llm_based"


class EvaluationFinding(EvaluationModel):
    code: str
    message: str
    data: dict[str, Any]


class EvaluationResult(EvaluationModel):
    passed: bool
    score: float | None
    label: str | None
    severity: str | None
    reason: str
    findings: list[EvaluationFinding]


class SpanView(EvaluationModel):
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
    error_category: str | None
    error_code: str | None
    error_message: str | None
    error_failed_component: str | None


class ToolCallView(EvaluationModel):
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
    error_category: str | None
    error_code: str | None
    error_message: str | None
    error_failed_component: str | None


class LLMCallView(EvaluationModel):
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
    error_category: str | None
    error_code: str | None
    error_message: str | None
    error_failed_component: str | None


class EvaluationRunView(EvaluationModel):
    run_id: str
    schema_version: str
    event_type: RunEventType
    agent_name: str
    agent_version: str
    prompt_version: str
    environment: str
    raw_input: Any
    normalized_input: str | None
    scenario_id: str | None
    started_at: datetime
    completed_at: datetime | None
    status: RunStatus
    execution_latency_ms: int | None
    wall_clock_duration_ms: int | None
    resume_count: int
    hitl_required: bool
    hitl_state: HITLState
    hitl_checkpoint_id: str | None
    hitl_decision: Literal["approve", "reject"] | None
    hitl_requested_at: datetime | None
    hitl_decided_at: datetime | None
    hitl_pending_action: dict[str, Any] | None
    usage_total_llm_calls: int
    usage_total_tool_calls: int
    usage_total_tokens: int
    usage_total_retries: int
    usage_total_estimated_cost_usd: float
    final_result_output: dict[str, Any] | None
    final_result_source_references: list[str] | None
    runtime_error_category: str | None
    runtime_error_code: str | None
    runtime_error_message: str | None
    runtime_error_failed_component: str | None
    spans: list[SpanView]
    tool_calls: list[ToolCallView]
    llm_calls: list[LLMCallView]
