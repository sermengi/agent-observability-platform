"""add regression run metadata

Revision ID: 20260903_0007
Revises: 20260903_0006
Create Date: 2026-09-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0007"
down_revision: str | Sequence[str] | None = "20260903_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("regression_runs", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "regression_runs",
        sa.Column("agent_version", sa.Text(), server_default="unknown", nullable=False),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "agent_model_provider",
            sa.Text(),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "agent_model_name",
            sa.Text(),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "prompt_version",
            sa.Text(),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "scenario_contract_version",
            sa.Text(),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "evaluator_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column("repetitions", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "regression_runs",
        sa.Column(
            "scenario_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "regression_runs",
        sa.Column("is_baseline", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_check_constraint(
        "ck_regression_runs_status",
        "regression_runs",
        "status IN ('pending', 'running', 'completed', 'failed')",
    )
    op.create_index(
        "uq_regression_runs_baseline",
        "regression_runs",
        ["is_baseline"],
        unique=True,
        postgresql_where=sa.text("is_baseline = true"),
    )
    for column_name in [
        "agent_version",
        "agent_model_provider",
        "agent_model_name",
        "prompt_version",
        "scenario_contract_version",
        "evaluator_versions",
        "repetitions",
        "scenario_ids",
    ]:
        op.alter_column("regression_runs", column_name, server_default=None)


def downgrade() -> None:
    op.drop_index("uq_regression_runs_baseline", table_name="regression_runs")
    op.drop_constraint(
        "ck_regression_runs_status",
        "regression_runs",
        type_="check",
    )
    op.drop_column("regression_runs", "is_baseline")
    op.drop_column("regression_runs", "scenario_ids")
    op.drop_column("regression_runs", "repetitions")
    op.drop_column("regression_runs", "evaluator_versions")
    op.drop_column("regression_runs", "scenario_contract_version")
    op.drop_column("regression_runs", "prompt_version")
    op.drop_column("regression_runs", "agent_model_name")
    op.drop_column("regression_runs", "agent_model_provider")
    op.drop_column("regression_runs", "agent_version")
    op.drop_column("regression_runs", "name")
