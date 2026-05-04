"""SQLAlchemy 2.0 async ORM models.

This is the cache layer. The vault is the source of truth — DB rows are written
to from the vault writer, and the vault writer also writes the .md files. The
DB is what powers the Discord bot, the API, and the live monitor.

If the DB is wiped, run `python -m src.workers.rehydrate` to rebuild from the
vault. (TODO: rehydrate worker — not in v0.1.)
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Enum as _SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def SAEnum(enum_cls, **kw):  # noqa: N802
    """Wrap SQLAlchemy Enum to use .value (lowercase) instead of .name (uppercase)."""
    return _SAEnum(enum_cls, values_callable=lambda e: [x.value for x in e], **kw)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONB, list: JSONB}


# ---------- Enums ----------


class Mode(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"
    BASELINE = "baseline"  # SPY equal-weight benchmark


class SignalSource(str, enum.Enum):
    SEC_FORM4 = "sec_form4"
    SEC_13D = "sec_13d"
    SEC_13F = "sec_13f"
    QUIVER_CONGRESS = "quiver_congress"
    UW_FLOW = "uw_flow"
    UW_CONGRESS = "uw_congress"
    UW_DARKPOOL = "uw_darkpool"
    MANUAL = "manual"


class SignalStatus(str, enum.Enum):
    NEW = "new"
    PROCESSED = "processed"
    IGNORED = "ignored"
    BLOCKED = "blocked"  # passed scoring but blocked by a hard gate


class Action(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class GateName(str, enum.Enum):
    LIQUIDITY = "liquidity"
    EARNINGS_PROXIMITY = "earnings_proximity"
    EVENT_PROXIMITY = "event_proximity"
    CORRELATION = "correlation"
    STALE_SIGNAL = "stale_signal"
    SNOOZED = "snoozed"
    DAILY_KILL_SWITCH = "daily_kill_switch"
    MAX_OPEN_POSITIONS = "max_open_positions"


class OrderStatus(str, enum.Enum):
    DRAFT = "draft"           # bot built it, awaiting human /confirm
    SENT = "sent"             # sent to broker
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SKIPPED = "skipped"       # human said /skip


class PositionStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class CloseReason(str, enum.Enum):
    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    SIGNAL_EXIT = "signal_exit"
    DISCRETIONARY = "discretionary"
    EOD_CANCELLED = "eod_cancelled"


# ---------- Tables ----------


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Trader(Base):
    """A person we track — politician, hedge fund manager, public investor."""

    __tablename__ = "traders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # "pelosi", "buffett"
    display_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # politician | fund_manager | insider
    extra: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Signal(Base):
    """A single observation from one source. Written by ingestors. Scored by scorer."""

    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

    # Mode tag — paper vs live data path. Same data flows to both.
    mode: Mapped[Mode] = mapped_column(SAEnum(Mode, name="mode"), default=Mode.PAPER, index=True)

    source: Mapped[SignalSource] = mapped_column(SAEnum(SignalSource, name="signal_source"))
    source_external_id: Mapped[str] = mapped_column(String(256), index=True)  # for dedupe
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[Action] = mapped_column(SAEnum(Action, name="action"))

    trader_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("traders.id"), nullable=True, index=True
    )
    trader: Mapped[Trader | None] = relationship("Trader", lazy="joined")

    # Timing
    disclosed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    trade_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Size (range, since disclosures are usually bracketed)
    size_low_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    size_high_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    # Scoring
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    score_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Gates
    gate_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    gates_failed: Mapped[list] = mapped_column(JSONB, default=list)

    status: Mapped[SignalStatus] = mapped_column(
        SAEnum(SignalStatus, name="signal_status"), default=SignalStatus.NEW, index=True
    )

    # Original payload from upstream — kept verbatim for forensic replay
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Vault path of the .md file this signal was written to
    vault_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_signals_source_extid", "source", "source_external_id", unique=True),
        Index("ix_signals_ticker_disclosed", "ticker", "disclosed_at"),
    )


class Indicators(Base):
    """Cached per-ticker indicator/regime snapshot. One row per (ticker, computed_at)."""

    __tablename__ = "indicators"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    sma_50: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    sma_200: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    atr_20: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    rsi_14: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    iv_percentile_30d: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    adv_30d_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    rs_vs_spy_60d: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    rs_vs_sector_60d: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    swing_high_90d: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    swing_low_90d: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    sector_etf: Mapped[str | None] = mapped_column(String(8), nullable=True)
    trend: Mapped[str | None] = mapped_column(String(16), nullable=True)  # uptrend|downtrend|chop

    __table_args__ = (Index("ix_indicators_ticker_computed", "ticker", "computed_at"),)


class EarningsCalendar(Base):
    """Upcoming/recent earnings dates per ticker, refreshed daily from Finnhub."""

    __tablename__ = "earnings_calendar"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    earnings_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    when: Mapped[str | None] = mapped_column(String(8), nullable=True)  # "amc"|"bmo"|"dmt"
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_earnings_ticker_date", "ticker", "earnings_date", unique=True),
    )


class DraftOrder(Base):
    """A bracket order built by the backend, awaiting human /confirm."""

    __tablename__ = "draft_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    mode: Mapped[Mode] = mapped_column(SAEnum(Mode, name="mode"), default=Mode.PAPER, index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[Action] = mapped_column(SAEnum(Action, name="action"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    entry_limit: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stop: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    setup: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score_at_creation: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    # Source signals that led to this draft
    source_signal_ids: Mapped[list] = mapped_column(JSONB, default=list)

    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), default=OrderStatus.DRAFT, index=True
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    discord_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Position(Base):
    """An open or closed position. Created when broker confirms fill."""

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    mode: Mapped[Mode] = mapped_column(SAEnum(Mode, name="mode"), default=Mode.PAPER, index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[Action] = mapped_column(SAEnum(Action, name="action"))

    # From the draft
    draft_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("draft_orders.id"), nullable=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # Entry
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    entry_price: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stop: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 4))

    # Exit (null while open)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)

    status: Mapped[PositionStatus] = mapped_column(
        SAEnum(PositionStatus, name="position_status"), default=PositionStatus.OPEN, index=True
    )
    close_reason: Mapped[CloseReason | None] = mapped_column(
        SAEnum(CloseReason, name="close_reason"), nullable=True
    )

    # Pattern/regime context at entry — for synthesis attribution later
    setup_at_entry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trend_at_entry: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    indicators_at_entry: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Source signals
    source_signal_ids: Mapped[list] = mapped_column(JSONB, default=list)

    # Vault path
    vault_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class EquitySnapshot(Base):
    """Daily equity curve. Written end-of-session."""

    __tablename__ = "equity_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    mode: Mapped[Mode] = mapped_column(SAEnum(Mode, name="mode"), index=True)
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    positions_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    daily_pnl_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"))

    __table_args__ = (
        Index("ix_equity_mode_date", "mode", "snapshot_date", unique=True),
    )


class Snooze(Base):
    """Tickers temporarily ignored by the scorer. Set via Discord /snooze."""

    __tablename__ = "snoozes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    snoozed_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestorHeartbeat(Base):
    """Per-ingestor heartbeat for system-health monitoring."""

    __tablename__ = "ingestor_heartbeats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ingestor: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    signals_in_last_run: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(Base):
    """Append-only log of consequential actions."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))  # "ingestor:sec_form4" | "discord:user_id" | "scheduler"
    action: Mapped[str] = mapped_column(String(64))  # "signal_created", "order_confirmed", etc
    target_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
