"""add evaluation result passed column

Revision ID: 20260830_0005
Revises: 20260830_0004
Create Date: 2026-08-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0005"
down_revision: str | Sequence[str] | None = "20260830_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_results",
        sa.Column("passed", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_results", "passed")
