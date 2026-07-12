"""outbound delivery_status

Revision ID: 002_outbound_status
Revises: 001_init
Create Date: 2026-05-03

Adds the ``delivery_status`` column to ``messages`` (spec §13 OQ-3).

Why this column exists
----------------------
Inbound rows do not need a delivery state — they are observed facts
about messages the platform has already delivered. **Outbound** rows
need a state machine the operator can reason about post-incident:

* ``pending``      — adapter accepted the request and is about to dispatch
                     to the platform connector. (Reserved for the Phase 2+
                     async send queue; v1 inserts directly as ``sent``.)
* ``sent``         — platform connector returned success. v1 outbound rows
                     are only persisted on successful send and land here.
* ``send_failed``  — platform connector returned a terminal failure
                     (post-retry). Reserved for forensics; v1 does not
                     persist failed sends (the route raises 502 instead).

The column is ``NULL`` on inbound rows because the value is meaningless
there; a CHECK constraint enforces ``NULL OR IN(pending|sent|send_failed)``
so a bad write fails loudly. The schema module
:mod:`amg.core.schema` declares the column on the live ``messages`` table
so introspection / Pydantic mapping stays accurate.

SQLite ALTER TABLE caveat
-------------------------
SQLite cannot ``ALTER TABLE ADD COLUMN`` with an inline ``CHECK`` constraint
in older versions, and Alembic's plain ``op.add_column`` cannot attach a
named ``CheckConstraint`` to a SQLite column. We therefore use
``op.batch_alter_table`` which performs the canonical
"create-new / copy / drop-old / rename" dance, attaching the constraint
the same way a fresh schema build would. Existing rows default to ``NULL``
(the column is nullable) so no backfill is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_outbound_status"
down_revision: str | None = "001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``messages.delivery_status`` with the CHECK constraint."""
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column("delivery_status", sa.Text(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_messages_delivery_status",
            "delivery_status IS NULL OR "
            "delivery_status IN ('pending','sent','send_failed')",
        )


def downgrade() -> None:
    """Drop the CHECK constraint and the column."""
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint("ck_messages_delivery_status", type_="check")
        batch_op.drop_column("delivery_status")
