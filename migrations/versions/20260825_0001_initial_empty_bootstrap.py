"""initial empty bootstrap

Revision ID: 20260825_0001
Revises:
Create Date: 2026-08-25 00:00:00.000000

"""

from collections.abc import Sequence

revision: str = "20260825_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
