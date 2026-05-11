"""v2 foundation: enum additions + DailyState + Position.broker_order_id unique

Revision ID: 0002_v2_foundation
Revises: 0001_initial
Create Date: 2026-05-10

Adds:
  - SignalSource.MARKET_MOVEMENT enum value
  - CloseReason.EXTERNAL enum value
  - DailyState table (PK: date, mode) for live kill-switch state
  - UNIQUE constraint on positions.broker_order_id (deduping any existing
    duplicates first per V2.3)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_v2_foundation"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Enum value additions (Postgres ALTER TYPE ... ADD VALUE) ---
    # These cannot run inside an implicit transaction in older PG versions;
    # alembic handles this for us via op.execute when the env is configured
    # as autocommit. If your alembic env wraps in a tx, set
    # `transaction_per_migration = True` or run `COMMIT` first.
    op.execute("ALTER TYPE signal_source ADD VALUE IF NOT EXISTS 'market_movement'")
    op.execute("ALTER TYPE close_reason ADD VALUE IF NOT EXISTS 'external'")

    # --- Dedupe positions before adding the unique constraint (V2.3) ---
    op.execute(
        """
        DELETE FROM positions p1
        USING positions p2
        WHERE p1.id > p2.id
          AND p1.broker_order_id = p2.broker_order_id
          AND p1.broker_order_id IS NOT NULL
        """
    )
    op.create_unique_constraint(
        "uq_positions_broker_order_id",
        "positions",
        ["broker_order_id"],
    )

    # --- DailyState table ---
    # Reuse the existing `mode` enum type created in 0001_initial.
    mode = postgresql.ENUM("paper", "live", "baseline", name="mode", create_type=False)
    op.create_table(
        "daily_state",
        sa.Column("date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("mode", mode, primary_key=True, nullable=False),
        sa.Column(
            "daily_pnl_pct",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("equity_at_open", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_state")
    op.drop_constraint("uq_positions_broker_order_id", "positions")
    # Note: Postgres does not support removing enum values cleanly; leaving
    # 'market_movement' and 'external' in place on downgrade. Recreate the
    # enum manually if a hard rollback is needed.
