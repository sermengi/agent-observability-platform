"""add phase 2 minimal indexes

Revision ID: 20260827_0003
Revises: 20260827_0002
Create Date: 2026-08-27 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0003"
down_revision: str | Sequence[str] | None = "20260827_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_scenario_id", "agent_runs", ["scenario_id"])
    op.create_index("ix_agent_runs_agent_version", "agent_runs", ["agent_version"])
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"])
    op.create_index("ix_spans_parent_span_id", "spans", ["parent_span_id"])
    op.create_index("ix_tool_calls_span_id", "tool_calls", ["span_id"])
    op.create_index(
        "ix_tool_calls_tool_name_status",
        "tool_calls",
        ["tool_name", "status"],
    )
    op.create_index("ix_llm_calls_span_id", "llm_calls", ["span_id"])
    op.create_index("ix_llm_calls_model", "llm_calls", ["model"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_model", table_name="llm_calls")
    op.drop_index("ix_llm_calls_span_id", table_name="llm_calls")
    op.drop_index("ix_tool_calls_tool_name_status", table_name="tool_calls")
    op.drop_index("ix_tool_calls_span_id", table_name="tool_calls")
    op.drop_index("ix_spans_parent_span_id", table_name="spans")
    op.drop_index("ix_agent_runs_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_version", table_name="agent_runs")
    op.drop_index("ix_agent_runs_scenario_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
