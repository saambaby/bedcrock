"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-03

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Enums ---
    mode = postgresql.ENUM("paper", "live", "baseline", name="mode", create_type=False)
    mode.create(op.get_bind(), checkfirst=True)

    signal_source = postgresql.ENUM(
        "sec_form4", "sec_13d", "sec_13f",
        "quiver_congress",
        "uw_flow", "uw_congress", "uw_darkpool",
        "manual",
        name="signal_source",
        create_type=False,
    )
    signal_source.create(op.get_bind(), checkfirst=True)

    signal_status = postgresql.ENUM(
        "new", "processed", "ignored", "blocked",
        name="signal_status",
        create_type=False,
    )
    signal_status.create(op.get_bind(), checkfirst=True)

    action = postgresql.ENUM("buy", "sell", name="action", create_type=False)
    action.create(op.get_bind(), checkfirst=True)

    order_status = postgresql.ENUM(
        "draft", "sent", "filled", "partially_filled",
        "cancelled", "rejected", "expired", "skipped",
        name="order_status",
        create_type=False,
    )
    order_status.create(op.get_bind(), checkfirst=True)

    position_status = postgresql.ENUM("open", "closed", name="position_status", create_type=False)
    position_status.create(op.get_bind(), checkfirst=True)

    close_reason = postgresql.ENUM(
        "stop_hit", "target_hit", "signal_exit",
        "discretionary", "eod_cancelled",
        name="close_reason",
        create_type=False,
    )
    close_reason.create(op.get_bind(), checkfirst=True)

    # --- traders ---
    op.create_table(
        "traders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("extra", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_traders_slug", "traders", ["slug"], unique=True)

    # --- signals ---
    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", mode, nullable=False, server_default="paper"),
        sa.Column("source", signal_source, nullable=False),
        sa.Column("source_external_id", sa.String(256), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("action", action, nullable=False),
        sa.Column("trader_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("traders.id"), nullable=True),
        sa.Column("disclosed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("size_low_usd", sa.Numeric(18, 2), nullable=True),
        sa.Column("size_high_usd", sa.Numeric(18, 2), nullable=True),
        sa.Column("score", sa.Numeric(6, 2), nullable=True),
        sa.Column("score_breakdown", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("gate_blocked", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("gates_failed", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("status", signal_status, nullable=False, server_default="new"),
        sa.Column("raw", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("vault_path", sa.Text, nullable=True),
    )
    op.create_index("ix_signals_mode", "signals", ["mode"])
    op.create_index("ix_signals_ticker", "signals", ["ticker"])
    op.create_index("ix_signals_disclosed_at", "signals", ["disclosed_at"])
    op.create_index("ix_signals_status", "signals", ["status"])
    op.create_index("ix_signals_trader_id", "signals", ["trader_id"])
    op.create_index("ix_signals_gate_blocked", "signals", ["gate_blocked"])
    op.create_index("ix_signals_source_extid", "signals",
                    ["source", "source_external_id"], unique=True)
    op.create_index("ix_signals_ticker_disclosed", "signals", ["ticker", "disclosed_at"])

    # --- indicators ---
    op.create_table(
        "indicators",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("price", sa.Numeric(14, 4), nullable=True),
        sa.Column("sma_50", sa.Numeric(14, 4), nullable=True),
        sa.Column("sma_200", sa.Numeric(14, 4), nullable=True),
        sa.Column("atr_20", sa.Numeric(14, 4), nullable=True),
        sa.Column("rsi_14", sa.Numeric(6, 2), nullable=True),
        sa.Column("iv_percentile_30d", sa.Numeric(6, 2), nullable=True),
        sa.Column("adv_30d_usd", sa.Numeric(18, 2), nullable=True),
        sa.Column("rs_vs_spy_60d", sa.Numeric(6, 4), nullable=True),
        sa.Column("rs_vs_sector_60d", sa.Numeric(6, 4), nullable=True),
        sa.Column("swing_high_90d", sa.Numeric(14, 4), nullable=True),
        sa.Column("swing_low_90d", sa.Numeric(14, 4), nullable=True),
        sa.Column("sector_etf", sa.String(8), nullable=True),
        sa.Column("trend", sa.String(16), nullable=True),
    )
    op.create_index("ix_indicators_ticker", "indicators", ["ticker"])
    op.create_index("ix_indicators_ticker_computed", "indicators", ["ticker", "computed_at"])

    # --- earnings_calendar ---
    op.create_table(
        "earnings_calendar",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("earnings_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("when", sa.String(8), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_earnings_ticker", "earnings_calendar", ["ticker"])
    op.create_index("ix_earnings_date", "earnings_calendar", ["earnings_date"])
    op.create_index("ix_earnings_ticker_date", "earnings_calendar",
                    ["ticker", "earnings_date"], unique=True)

    # --- draft_orders ---
    op.create_table(
        "draft_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", mode, nullable=False, server_default="paper"),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", action, nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("entry_limit", sa.Numeric(14, 4), nullable=False),
        sa.Column("stop", sa.Numeric(14, 4), nullable=False),
        sa.Column("target", sa.Numeric(14, 4), nullable=False),
        sa.Column("setup", sa.String(32), nullable=True),
        sa.Column("score_at_creation", sa.Numeric(6, 2), nullable=True),
        sa.Column("source_signal_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("status", order_status, nullable=False, server_default="draft"),
        sa.Column("skip_reason", sa.Text, nullable=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discord_message_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_draft_orders_mode", "draft_orders", ["mode"])
    op.create_index("ix_draft_orders_ticker", "draft_orders", ["ticker"])
    op.create_index("ix_draft_orders_status", "draft_orders", ["status"])

    # --- positions ---
    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", mode, nullable=False, server_default="paper"),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", action, nullable=False),
        sa.Column("draft_order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("draft_orders.id"), nullable=True),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("entry_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("stop", sa.Numeric(14, 4), nullable=False),
        sa.Column("target", sa.Numeric(14, 4), nullable=False),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("pnl_usd", sa.Numeric(14, 2), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("status", position_status, nullable=False, server_default="open"),
        sa.Column("close_reason", close_reason, nullable=True),
        sa.Column("setup_at_entry", sa.String(32), nullable=True),
        sa.Column("trend_at_entry", sa.String(16), nullable=True),
        sa.Column("market_regime", sa.String(32), nullable=True),
        sa.Column("indicators_at_entry", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("source_signal_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("vault_path", sa.Text, nullable=True),
    )
    op.create_index("ix_positions_mode", "positions", ["mode"])
    op.create_index("ix_positions_ticker", "positions", ["ticker"])
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_index("ix_positions_broker_order_id", "positions", ["broker_order_id"])

    # --- equity_snapshots ---
    op.create_table(
        "equity_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("mode", mode, nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("equity", sa.Numeric(14, 2), nullable=False),
        sa.Column("cash", sa.Numeric(14, 2), nullable=False),
        sa.Column("positions_value", sa.Numeric(14, 2), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("daily_pnl_pct", sa.Numeric(8, 4), nullable=False, server_default="0"),
    )
    op.create_index("ix_equity_mode_date", "equity_snapshots",
                    ["mode", "snapshot_date"], unique=True)

    # --- snoozes ---
    op.create_table(
        "snoozes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticker", sa.String(16), unique=True, nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_snoozes_ticker", "snoozes", ["ticker"], unique=True)

    # --- ingestor_heartbeats ---
    op.create_table(
        "ingestor_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ingestor", sa.String(64), unique=True, nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("signals_in_last_run", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_heartbeat_ingestor", "ingestor_heartbeats", ["ingestor"], unique=True)

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_audit_occurred", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_action", "audit_log", ["action"])


def downgrade() -> None:
    for t in [
        "audit_log", "ingestor_heartbeats", "snoozes", "equity_snapshots",
        "positions", "draft_orders", "earnings_calendar", "indicators",
        "signals", "traders",
    ]:
        op.drop_table(t)
    for e in [
        "close_reason", "position_status", "order_status", "action",
        "signal_status", "signal_source", "mode",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {e}")
