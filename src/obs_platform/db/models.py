from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from obs_platform.database import Base
from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)


def _values(values: type[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in values)


_EVALUATOR_EXECUTION_STATUSES = "'pending', 'running', 'completed', 'failed', 'skipped'"
_OVERALL_EVALUATION_STATUSES = "'pass', 'fail', 'incomplete'"


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_values(RunEventType)})",
            name="ck_agent_runs_event_type",
        ),
        CheckConstraint(
            f"status IN ({_values(RunStatus)})",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            f"hitl_state IN ({_values(HITLState)})",
            name="ck_agent_runs_hitl_state",
        ),
        CheckConstraint(
            "hitl_decision IS NULL OR hitl_decision IN ('approve', 'reject')",
            name="ck_agent_runs_hitl_decision",
        ),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_scenario_id", "scenario_id"),
        Index("ix_agent_runs_agent_version", "agent_version"),
        Index("ix_agent_runs_started_at", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    raw_input: Mapped[Any] = mapped_column(JSONB, nullable=False)
    normalized_input: Mapped[str | None] = mapped_column(Text)
    scenario_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    execution_latency_ms: Mapped[int | None] = mapped_column(Integer)
    wall_clock_duration_ms: Mapped[int | None] = mapped_column(Integer)
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hitl_required: Mapped[bool] = mapped_column(nullable=False)
    hitl_state: Mapped[str] = mapped_column(Text, nullable=False)
    hitl_checkpoint_id: Mapped[str | None] = mapped_column(Text)
    hitl_decision: Mapped[str | None] = mapped_column(Text)
    hitl_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hitl_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hitl_pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage_total_llm_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    usage_total_tool_calls: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    usage_total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    usage_total_retries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    usage_total_estimated_cost_usd: Mapped[float] = mapped_column(
        DOUBLE_PRECISION,
        nullable=False,
        default=0.0,
        server_default="0",
    )
    final_result_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_result_source_references: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
    )
    runtime_error_category: Mapped[str | None] = mapped_column(Text)
    runtime_error_code: Mapped[str | None] = mapped_column(Text)
    runtime_error_message: Mapped[str | None] = mapped_column(Text)
    runtime_error_failed_component: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (
        UniqueConstraint("run_id", "span_id", name="uq_spans_run_id_span_id"),
        CheckConstraint(
            f"status IN ({_values(ExecutionStatus)})",
            name="ck_spans_status",
        ),
        Index("ix_spans_parent_span_id", "parent_span_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id"),
        nullable=False,
    )
    span_id: Mapped[str] = mapped_column(String(256), nullable=False)
    parent_span_id: Mapped[int | None] = mapped_column(ForeignKey("spans.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    error_category: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_failed_component: Mapped[str | None] = mapped_column(Text)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_values(ExecutionStatus)})",
            name="ck_tool_calls_status",
        ),
        Index("ix_tool_calls_span_id", "span_id"),
        Index("ix_tool_calls_tool_name_status", "tool_name", "status"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id"),
        primary_key=True,
    )
    tool_call_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    span_id: Mapped[int] = mapped_column(ForeignKey("spans.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_category: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_failed_component: Mapped[str | None] = mapped_column(Text)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(
            f"call_type IN ({_values(LLMCallType)})",
            name="ck_llm_calls_call_type",
        ),
        CheckConstraint(
            f"status IN ({_values(ExecutionStatus)})",
            name="ck_llm_calls_status",
        ),
        Index("ix_llm_calls_span_id", "span_id"),
        Index("ix_llm_calls_model", "model"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id"),
        primary_key=True,
    )
    llm_call_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    span_id: Mapped[int] = mapped_column(ForeignKey("spans.id"), nullable=False)
    call_type: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    input_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_category: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_failed_component: Mapped[str | None] = mapped_column(Text)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_EVALUATOR_EXECUTION_STATUSES})",
            name="ck_evaluation_results_status",
        ),
        UniqueConstraint(
            "run_id",
            "evaluator_name",
            "evaluator_version",
            "regression_run_id",
            name="uq_evaluation_results_run_evaluator_version_regression",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id"),
        nullable=False,
    )
    evaluator_name: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(Text, nullable=False)
    regression_run_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(DOUBLE_PRECISION)
    label: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class RunFailure(Base):
    __tablename__ = "run_failures"
    __table_args__ = (
        CheckConstraint(
            f"overall_status IN ({_OVERALL_EVALUATION_STATUSES})",
            name="ck_run_failures_overall_status",
        ),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id"),
        primary_key=True,
    )
    overall_status: Mapped[str] = mapped_column(Text, nullable=False)
    primary_category: Mapped[str | None] = mapped_column(Text)
    secondary_category: Mapped[str | None] = mapped_column(Text)
    max_severity: Mapped[str | None] = mapped_column(Text)
    classifier_version: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
