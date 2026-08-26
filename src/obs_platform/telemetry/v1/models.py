from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)

StableID = Annotated[str, Field(min_length=1, max_length=256)]


class TelemetryModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator(
        "run_id",
        "span_id",
        "parent_span_id",
        "tool_call_id",
        "llm_call_id",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def strip_and_validate_ids(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                raise ValueError("ID fields must not be empty or whitespace only")
            return stripped
        return value

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
    span_id: StableID
    parent_span_id: StableID | None
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
    tool_call_id: StableID
    span_id: StableID
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
    llm_call_id: StableID
    span_id: StableID
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
    run_id: StableID
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

    @model_validator(mode="after")
    def validate_run_consistency(self) -> Self:
        self._validate_child_ids_and_references()
        self._validate_lifecycle()
        return self

    def _validate_child_ids_and_references(self) -> None:
        span_ids = self._require_unique_ids(
            [span.span_id for span in self.spans],
            "span_id",
        )
        self._require_unique_ids(
            [tool_call.tool_call_id for tool_call in self.tool_calls],
            "tool_call_id",
        )
        self._require_unique_ids(
            [llm_call.llm_call_id for llm_call in self.llm_calls],
            "llm_call_id",
        )

        for span in self.spans:
            if span.parent_span_id is not None and span.parent_span_id not in span_ids:
                raise ValueError(
                    f"span.parent_span_id references missing span_id: "
                    f"{span.parent_span_id}"
                )

        for tool_call in self.tool_calls:
            if tool_call.span_id not in span_ids:
                raise ValueError(
                    f"tool_call.span_id references missing span_id: "
                    f"{tool_call.span_id}"
                )

        for llm_call in self.llm_calls:
            if llm_call.span_id not in span_ids:
                raise ValueError(
                    f"llm_call.span_id references missing span_id: {llm_call.span_id}"
                )

    def _validate_lifecycle(self) -> None:
        if self.event_type is RunEventType.RUN_AWAITING_APPROVAL:
            self._validate_awaiting_approval_lifecycle()
            return

        self._validate_final_lifecycle()

    def _validate_awaiting_approval_lifecycle(self) -> None:
        if self.completed_at is not None:
            raise ValueError("completed_at must be None for awaiting approval events")
        if self.status is not RunStatus.AWAITING_APPROVAL:
            raise ValueError(
                "awaiting approval events must use awaiting approval status"
            )
        if self.final_result is not None:
            raise ValueError("final_result must be None for awaiting approval events")
        if self.runtime_error is not None:
            raise ValueError("runtime_error must be None for awaiting approval events")
        if self.hitl.required is not True or self.hitl.state is not HITLState.PENDING:
            raise ValueError("awaiting approval events require pending HITL state")
        if self.hitl.pending_action is None:
            raise ValueError("pending_action is required for pending HITL state")

    def _validate_final_lifecycle(self) -> None:
        if self.completed_at is None:
            raise ValueError("completed_at is required for final events")
        if self.hitl.state is HITLState.PENDING:
            raise ValueError(
                "pending HITL state is only valid for awaiting approval events"
            )
        if self.hitl.pending_action is not None:
            raise ValueError("pending_action must be None for final events")

        if self.status is RunStatus.SUCCESS:
            if self.final_result is None:
                raise ValueError("final_result is required for successful final events")
            if self.runtime_error is not None:
                raise ValueError(
                    "runtime_error must be None for successful final events"
                )
            if (
                self.hitl.required is False
                and self.hitl.state is not HITLState.NOT_REQUIRED
            ):
                raise ValueError(
                    "non-HITL final events require not_required HITL state"
                )
            if self.hitl.required is True and self.hitl.state not in {
                HITLState.APPROVED,
                HITLState.REJECTED,
            }:
                raise ValueError(
                    "HITL final events require approved or rejected HITL state"
                )
            return

        if self.status in {RunStatus.TOOL_ERROR, RunStatus.RUNTIME_ERROR}:
            if self.final_result is not None:
                raise ValueError("final_result must be None for error final events")
            if self.runtime_error is None:
                raise ValueError("runtime_error is required for error final events")
            if (
                self.hitl.required is not False
                or self.hitl.state is not HITLState.NOT_REQUIRED
            ):
                raise ValueError("error final events require not_required HITL state")
            return

        raise ValueError("awaiting approval status is not valid for final events")

    @staticmethod
    def _require_unique_ids(ids: list[str], field_name: str) -> set[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item_id in ids:
            if item_id in seen:
                duplicates.add(item_id)
            seen.add(item_id)

        if duplicates:
            raise ValueError(f"duplicate {field_name} values: {sorted(duplicates)}")

        return seen
