"""create phase 2 core tables

Revision ID: 20260827_0002
Revises: 20260825_0001
Create Date: 2026-08-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260827_0002"
down_revision: str | Sequence[str] | None = "20260825_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("raw_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_input", sa.Text(), nullable=True),
        sa.Column("scenario_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("execution_latency_ms", sa.Integer(), nullable=True),
        sa.Column("wall_clock_duration_ms", sa.Integer(), nullable=True),
        sa.Column("resume_count", sa.Integer(), nullable=False),
        sa.Column("hitl_required", sa.Boolean(), nullable=False),
        sa.Column("hitl_state", sa.Text(), nullable=False),
        sa.Column("hitl_checkpoint_id", sa.Text(), nullable=True),
        sa.Column("hitl_decision", sa.Text(), nullable=True),
        sa.Column("hitl_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hitl_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "hitl_pending_action",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "usage_total_llm_calls",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "usage_total_tool_calls",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "usage_total_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "usage_total_retries",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "usage_total_estimated_cost_usd",
            postgresql.DOUBLE_PRECISION(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "final_result_output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "final_result_source_references",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column("runtime_error_category", sa.Text(), nullable=True),
        sa.Column("runtime_error_code", sa.Text(), nullable=True),
        sa.Column("runtime_error_message", sa.Text(), nullable=True),
        sa.Column("runtime_error_failed_component", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('run_final', 'run_awaiting_approval')",
            name="ck_agent_runs_event_type",
        ),
        sa.CheckConstraint(
            "hitl_decision IS NULL OR hitl_decision IN ('approve', 'reject')",
            name="ck_agent_runs_hitl_decision",
        ),
        sa.CheckConstraint(
            "hitl_state IN ('not_required', 'pending', 'approved', 'rejected')",
            name="ck_agent_runs_hitl_state",
        ),
        sa.CheckConstraint(
            "status IN ('success', 'tool_error', 'runtime_error', 'awaiting_approval')",
            name="ck_agent_runs_status",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("evaluator_name", sa.Text(), nullable=False),
        sa.Column("evaluator_version", sa.Text(), nullable=False),
        sa.Column("regression_run_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("score", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "evaluator_name",
            "evaluator_version",
            "regression_run_id",
            name="uq_evaluation_results_run_evaluator_version_regression",
        ),
    )
    op.create_table(
        "run_failures",
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("primary_category", sa.Text(), nullable=False),
        sa.Column("secondary_category", sa.Text(), nullable=True),
        sa.Column("max_severity", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "spans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("span_id", sa.String(length=256), nullable=False),
        sa.Column("parent_span_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_failed_component", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('success', 'failure', 'error')",
            name="ck_spans_status",
        ),
        sa.ForeignKeyConstraint(["parent_span_id"], ["spans.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "span_id", name="uq_spans_run_id_span_id"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("llm_call_id", sa.String(length=256), nullable=False),
        sa.Column("span_id", sa.Integer(), nullable=False),
        sa.Column("call_type", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", postgresql.DOUBLE_PRECISION(), nullable=True),
        sa.Column(
            "input_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "output_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_failed_component", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "call_type IN ('interpretation', 'evidence_gathering', 'synthesis')",
            name="ck_llm_calls_call_type",
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failure', 'error')",
            name="ck_llm_calls_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.ForeignKeyConstraint(["span_id"], ["spans.id"]),
        sa.PrimaryKeyConstraint("run_id", "llm_call_id"),
    )
    op.create_table(
        "tool_calls",
        sa.Column("run_id", sa.String(length=256), nullable=False),
        sa.Column("tool_call_id", sa.String(length=256), nullable=False),
        sa.Column("span_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_failed_component", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('success', 'failure', 'error')",
            name="ck_tool_calls_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.ForeignKeyConstraint(["span_id"], ["spans.id"]),
        sa.PrimaryKeyConstraint("run_id", "tool_call_id"),
    )


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("llm_calls")
    op.drop_table("spans")
    op.drop_table("run_failures")
    op.drop_table("evaluation_results")
    op.drop_table("agent_runs")
