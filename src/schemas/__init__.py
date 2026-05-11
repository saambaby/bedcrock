"""Pydantic schemas — the wire format between modules.

These are *not* the DB models. DB models live in src/db/models.py.
Schemas are used for: ingestor outputs, scorer inputs, vault frontmatter,
Discord embeds, and FastAPI request/response.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.db.models import Action, GateName, Mode, SignalSource

# ---------- Ingestion ----------


class RawSignal(BaseModel):
    """What every ingestor returns. Fully sourced, not yet scored."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: SignalSource
    source_external_id: str = Field(..., description="Stable upstream id for dedupe")
    ticker: str
    action: Action
    disclosed_at: datetime
    trade_date: datetime | None = None
    trader_slug: str | None = None
    trader_display_name: str | None = None
    trader_kind: str | None = None
    size_low_usd: Decimal | None = None
    size_high_usd: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------- Scoring ----------


class ScoreBreakdown(BaseModel):
    cluster: float = 0.0
    committee_match: float = 0.0
    size: float = 0.0
    insider_corroboration: float = 0.0
    options_flow_corroboration: float = 0.0
    flow_corroboration_market: float = 0.0
    trader_track_record: float = 0.0
    public_statement: float = 0.0
    trend_alignment: float = 0.0
    relative_strength: float = 0.0
    sentiment: float = 0.0
    regime_overlay: float = 0.0

    @property
    def total(self) -> float:
        return sum(getattr(self, f) for f in self.__class__.model_fields)

    def to_dict(self) -> dict[str, float]:
        return self.model_dump()


class GateResult(BaseModel):
    gate: GateName
    blocked: bool
    reason: str | None = None
    overrideable: bool = False


class ScoredSignal(BaseModel):
    raw_signal: RawSignal
    score: float
    breakdown: ScoreBreakdown
    gate_results: list[GateResult]

    @property
    def gate_blocked(self) -> bool:
        return any(g.blocked for g in self.gate_results)

    @property
    def gates_failed(self) -> list[str]:
        return [g.gate.value for g in self.gate_results if g.blocked]


# ---------- Indicators ----------


class IndicatorSnapshot(BaseModel):
    ticker: str
    computed_at: datetime
    price: Decimal | None = None
    sma_50: Decimal | None = None
    sma_200: Decimal | None = None
    atr_20: Decimal | None = None
    rsi_14: Decimal | None = None
    iv_percentile_30d: Decimal | None = None
    adv_30d_usd: Decimal | None = None
    rs_vs_spy_60d: Decimal | None = None
    rs_vs_sector_60d: Decimal | None = None
    swing_high_90d: Decimal | None = None
    swing_low_90d: Decimal | None = None
    sector_etf: str | None = None
    trend: str | None = None  # "uptrend" | "downtrend" | "chop"

    def stop_floor(self, entry: Decimal) -> Decimal | None:
        """Minimum stop distance: 1.5x ATR below entry (for longs)."""
        if self.atr_20 is None:
            return None
        return entry - self.atr_20 * Decimal("1.5")


# ---------- Orders ----------


class BracketOrderSpec(BaseModel):
    mode: Mode
    ticker: str
    side: Action
    quantity: Decimal
    entry_limit: Decimal
    stop: Decimal
    target: Decimal
    time_in_force: str = "day"
    setup: str | None = None
    client_order_id: str | None = None

    def risk_per_share(self) -> Decimal:
        return abs(self.entry_limit - self.stop)

    def reward_per_share(self) -> Decimal:
        return abs(self.target - self.entry_limit)

    def reward_to_risk(self) -> float:
        risk = self.risk_per_share()
        if risk == 0:
            return 0.0
        return float(self.reward_per_share() / risk)


class FillEvent(BaseModel):
    broker_order_id: str
    ticker: str
    side: Action
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    is_close: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class DraftOrderPayload(BaseModel):
    """A bracket order with our internal DB id + risk metadata.

    Returned by OrderBuilder.build_draft so callers can post to Discord
    with one-click confirm UX.
    """

    id: UUID
    mode: Mode
    ticker: str
    side: Action
    quantity: Decimal
    entry_limit: Decimal
    stop: Decimal
    target: Decimal
    setup: str | None = None
    score_at_creation: float | None = None
    risk_pct: float = Field(..., description="% of equity at risk if stop hits")
    rr_ratio: float = Field(..., description="reward:risk ratio")
    source_signal_ids: list[UUID] = Field(default_factory=list)


# Aliases — older code uses IngestedSignal interchangeably with RawSignal
IngestedSignal = RawSignal


# ---------- API ----------


class ConfirmRequest(BaseModel):
    draft_order_id: UUID


class SkipRequest(BaseModel):
    draft_order_id: UUID
    reason: str = ""


class HealthResponse(BaseModel):
    status: str
    mode: Mode
    db_ok: bool
    broker_ok: bool
    ingestors: dict[str, dict[str, Any]]
