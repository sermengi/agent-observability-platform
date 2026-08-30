"""add phase 5 evaluation status snapshot

Revision ID: 20260830_0004
Revises: 20260827_0003
Create Date: 2026-08-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0004"
down_revision: str | Sequence[str] | None = "20260827_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_failures",
        sa.Column("overall_status", sa.Text(), nullable=False),
    )
    op.add_column(
        "run_failures",
        sa.Column("classifier_version", sa.Text(), nullable=False),
    )
    op.alter_column("run_failures", "primary_category", nullable=True)
    op.alter_column("run_failures", "max_severity", nullable=True)
    op.create_check_constraint(
        "ck_evaluation_results_status",
        "evaluation_results",
        "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
    )
    op.create_check_constraint(
        "ck_run_failures_overall_status",
        "run_failures",
        "overall_status IN ('pass', 'fail', 'incomplete')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_run_failures_overall_status",
        "run_failures",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_results_status",
        "evaluation_results",
        type_="check",
    )
    op.alter_column("run_failures", "max_severity", nullable=False)
    op.alter_column("run_failures", "primary_category", nullable=False)
    op.drop_column("run_failures", "classifier_version")
    op.drop_column("run_failures", "overall_status")
