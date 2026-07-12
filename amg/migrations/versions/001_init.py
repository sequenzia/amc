"""init

Revision ID: 001_init
Revises:
Create Date: 2026-05-03

Initial migration: creates all nine tables defined in
:mod:`amg.core.schema` (spec §7.3.2 / §7.3.3) along with their indexes and
``CHECK`` constraints. We delegate to ``metadata.create_all`` /
``metadata.drop_all`` so the migration cannot drift from the schema module.

WAL mode is intentionally NOT applied here — that pragma is set per
connection at adapter startup by :mod:`amg.core.db`, which is the
recommended SQLite practice (set on a real connection, not via DDL
emission, since it persists in the database header).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from amg.core.schema import metadata

revision: str = "001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all tables, indexes, and constraints."""
    bind = op.get_bind()
    metadata.create_all(bind=bind)


def downgrade() -> None:
    """Drop everything in reverse-dependency order."""
    bind = op.get_bind()
    metadata.drop_all(bind=bind)
