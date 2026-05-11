"""v3 foundation: drop vault_path columns + add scoring_proposals/replay tables

Revision ID: 0003_drop_vault_and_add_v3_tables
Revises: 0002_v2_foundation
Create Date: 2026-05-10

Removes:
  - signals.vault_path
  - positions.vault_path

Adds:
  - scoring_proposals: weight proposals written by the weekly-synthesis skill
    (replaces the v0.2 `99 Meta/scoring-rules-proposed.md` vault file).
  - scoring_replay_reports: out-of-sample evaluation results per proposal,
    written by the replay engine (replaces the 06 Weekly markdown writes).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_drop_vault_and_add_v3_tables"
down_revision: str | None = "0002_v2_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Drop vault_path columns ---
    op.drop_column("signals", "vault_path")
    op.drop_column("positions", "vault_path")

    # --- scoring_replay_reports (created first; proposals references it) ---
    op.create_table(
        "scoring_replay_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("in_sample_sharpe", sa.Float(), nullable=True),
        sa.Column("out_of_sample_sharpe", sa.Float(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("profit_factor", sa.Float(), nullable=True),
        sa.Column("total_return_pct", sa.Float(), nullable=True),
        sa.Column("sharpe_delta_vs_baseline", sa.Float(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_scoring_replay_reports_created_at",
        "scoring_replay_reports",
        ["created_at"],
    )
    op.create_index(
        "ix_scoring_replay_reports_proposal_id",
        "scoring_replay_reports",
        ["proposal_id"],
    )

    # --- scoring_proposals ---
    op.create_table(
        "scoring_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("weights", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_report_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_scoring_proposals_proposed_at",
        "scoring_proposals",
        ["proposed_at"],
    )
    op.create_index(
        "ix_scoring_proposals_status",
        "scoring_proposals",
        ["status"],
    )

    # --- Cross-table FKs (added after both tables exist) ---
    op.create_foreign_key(
        "fk_replay_proposal",
        "scoring_replay_reports",
        "scoring_proposals",
        ["proposal_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_proposal_replay",
        "scoring_proposals",
        "scoring_replay_reports",
        ["replay_report_id"],
        ["id"],
    )


def downgrade() -> None:
    # Drop FKs first to break the cycle
    op.drop_constraint("fk_proposal_replay", "scoring_proposals", type_="foreignkey")
    op.drop_constraint("fk_replay_proposal", "scoring_replay_reports", type_="foreignkey")

    op.drop_index("ix_scoring_proposals_status", table_name="scoring_proposals")
    op.drop_index("ix_scoring_proposals_proposed_at", table_name="scoring_proposals")
    op.drop_table("scoring_proposals")

    op.drop_index(
        "ix_scoring_replay_reports_proposal_id", table_name="scoring_replay_reports"
    )
    op.drop_index(
        "ix_scoring_replay_reports_created_at", table_name="scoring_replay_reports"
    )
    op.drop_table("scoring_replay_reports")

    # Re-add vault_path columns as nullable (data was not preserved)
    op.add_column("positions", sa.Column("vault_path", sa.Text(), nullable=True))
    op.add_column("signals", sa.Column("vault_path", sa.Text(), nullable=True))
