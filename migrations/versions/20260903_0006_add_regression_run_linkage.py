"""add regression run linkage

Revision ID: 20260903_0006
Revises: 20260901_0005
Create Date: 2026-09-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0006"
down_revision: str | Sequence[str] | None = "20260901_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "regression_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("regression_run_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("repetition_index", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_runs_regression_run_id",
        "agent_runs",
        "regression_runs",
        ["regression_run_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_evaluation_results_regression_run_id",
        "evaluation_results",
        "regression_runs",
        ["regression_run_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_agent_runs_regression_scenario_repetition",
        "agent_runs",
        ["regression_run_id", "scenario_id", "repetition_index"],
    )
    op.create_index(
        "ix_agent_runs_regression_run_id",
        "agent_runs",
        ["regression_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_regression_run_id", table_name="agent_runs")
    op.drop_constraint(
        "uq_agent_runs_regression_scenario_repetition",
        "agent_runs",
        type_="unique",
    )
    op.drop_constraint(
        "fk_evaluation_results_regression_run_id",
        "evaluation_results",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_agent_runs_regression_run_id",
        "agent_runs",
        type_="foreignkey",
    )
    op.drop_column("agent_runs", "repetition_index")
    op.drop_column("agent_runs", "regression_run_id")
    op.drop_table("regression_runs")
