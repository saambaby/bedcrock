"""Hard gates.

Gates are binary blocks applied after scoring. A signal can have a perfect
score and still be blocked by a gate (e.g., earnings in 2 days). Blocked
signals are still persisted with `gate_blocked=True` and `gates_failed`
populated, so the weekly synthesis can ask "would the blocked trades have
won anyway?"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import get_broker
from src.config import settings
from src.db.models import (
    EarningsCalendar,
    GateName,
    Position,
    PositionStatus,
    Snooze,
)
from src.logging_config import get_logger
from src.schemas import GateResult, IndicatorSnapshot, RawSignal

logger = get_logger(__name__)

STALE_DAYS = 14

# Sector clustering map for the correlation gate. Bedcrock's universe is
# structurally cluster-prone (defense, biotech, etc.), so we map tickers to
# a representative sector ETF and cap aggregate exposure per sector. Tickers
# not listed fall back to "OTHER" — extend as the watchlist evolves.
SECTOR_ETF_MAP: dict[str, str] = {
    # Defense
    "LMT": "ITA", "RTX": "ITA", "NOC": "ITA", "GD": "ITA", "BA": "ITA",
    "LHX": "ITA", "HII": "ITA", "TXT": "ITA",
    # Biotech
    "MRNA": "XBI", "BNTX": "XBI", "CRSP": "XBI", "VRTX": "XBI", "REGN": "XBI",
    "BIIB": "XBI", "GILD": "XBI", "ALNY": "XBI",
    # Mega-cap tech
    "NVDA": "XLK", "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLK", "GOOG": "XLK",
    "META": "XLK", "AVGO": "XLK", "ORCL": "XLK", "CRM": "XLK", "AMD": "XLK",
    # Consumer discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY", "MCD": "XLY",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "WFC": "XLF", "MS": "XLF",
    "C": "XLF", "BLK": "XLF",
}


class GateEvaluator:
    """Stateless evaluator. Pass DB session per call."""

    async def evaluate(
        self,
        db: AsyncSession,
        signal: RawSignal,
        indicators: IndicatorSnapshot | None,
    ) -> list[GateResult]:
        results: list[GateResult] = []

        results.append(await self._gate_liquidity(indicators))
        results.append(await self._gate_earnings(db, signal))
        results.append(await self._gate_stale_signal(signal))
        results.append(await self._gate_snoozed(db, signal))
        results.append(await self._gate_max_open_positions(db))
        results.append(await self._gate_correlation(db, signal, indicators))

        # Event-proximity and daily-kill require richer context;
        # deferred to v0.2. For now they pass.
        results.append(GateResult(gate=GateName.EVENT_PROXIMITY, blocked=False))
        results.append(GateResult(gate=GateName.DAILY_KILL_SWITCH, blocked=False))

        return results

    async def _gate_liquidity(
        self, indicators: IndicatorSnapshot | None
    ) -> GateResult:
        min_adv = settings.risk_min_adv_usd
        if indicators is None or indicators.adv_30d_usd is None:
            return GateResult(
                gate=GateName.LIQUIDITY,
                blocked=True,
                reason="No indicator data — failing closed",
                overrideable=True,
            )
        if float(indicators.adv_30d_usd) < min_adv:
            return GateResult(
                gate=GateName.LIQUIDITY,
                blocked=True,
                reason=f"30d ADV ${float(indicators.adv_30d_usd):,.0f} < ${min_adv:,.0f}",
                overrideable=False,
            )
        return GateResult(gate=GateName.LIQUIDITY, blocked=False)

    async def _gate_earnings(
        self, db: AsyncSession, signal: RawSignal
    ) -> GateResult:
        days = settings.risk_earnings_blackout_days
        cutoff_lo = datetime.now(UTC) - timedelta(days=days)
        cutoff_hi = datetime.now(UTC) + timedelta(days=days)

        stmt = select(EarningsCalendar).where(
            EarningsCalendar.ticker == signal.ticker.upper(),
            EarningsCalendar.earnings_date >= cutoff_lo,
            EarningsCalendar.earnings_date <= cutoff_hi,
        )
        rows = (await db.execute(stmt)).scalars().all()
        if rows:
            dates = ", ".join(r.earnings_date.date().isoformat() for r in rows)
            return GateResult(
                gate=GateName.EARNINGS_PROXIMITY,
                blocked=True,
                reason=f"Earnings within ±{days} days: {dates}",
                overrideable=True,
            )
        return GateResult(gate=GateName.EARNINGS_PROXIMITY, blocked=False)

    async def _gate_stale_signal(self, signal: RawSignal) -> GateResult:
        age = datetime.now(UTC) - signal.disclosed_at
        if age > timedelta(days=STALE_DAYS):
            return GateResult(
                gate=GateName.STALE_SIGNAL,
                blocked=True,
                reason=f"Signal disclosed {age.days}d ago (limit {STALE_DAYS}d)",
                overrideable=True,
            )
        return GateResult(gate=GateName.STALE_SIGNAL, blocked=False)

    async def _gate_snoozed(self, db: AsyncSession, signal: RawSignal) -> GateResult:
        stmt = select(Snooze).where(
            Snooze.ticker == signal.ticker.upper(),
            Snooze.snoozed_until > datetime.now(UTC),
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row:
            return GateResult(
                gate=GateName.SNOOZED,
                blocked=True,
                reason=f"Snoozed until {row.snoozed_until.isoformat()}: {row.reason or ''}",
                overrideable=True,
            )
        return GateResult(gate=GateName.SNOOZED, blocked=False)

    async def _gate_correlation(
        self,
        db: AsyncSession,
        signal: RawSignal,
        indicators: IndicatorSnapshot | None,
    ) -> GateResult:
        """Block when proposed trade would push a sector's exposure over the cap.

        Fail-open when we lack the data needed to compute exposure (no
        indicators, no last_price, or zero/unknown account equity) — the
        liquidity gate already catches missing-indicator cases as blocking,
        so failing open here is safe.
        """
        if indicators is None or indicators.price is None:
            return GateResult(gate=GateName.CORRELATION, blocked=False)

        proposed_sector = SECTOR_ETF_MAP.get(signal.ticker.upper(), "OTHER")

        open_positions = (
            await db.execute(
                select(Position).where(
                    Position.mode == settings.mode,
                    Position.status == PositionStatus.OPEN,
                )
            )
        ).scalars().all()

        broker = get_broker()
        try:
            await broker.connect()
            account = await broker.get_account()
        except Exception as e:  # noqa: BLE001 — fail-open, log it
            logger.warning("correlation_gate_account_fetch_failed", error=str(e))
            try:
                await broker.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return GateResult(gate=GateName.CORRELATION, blocked=False)
        finally:
            try:
                await broker.disconnect()
            except Exception:  # noqa: BLE001
                pass

        if account.equity <= 0:
            return GateResult(gate=GateName.CORRELATION, blocked=False)

        sector_exposure: dict[str, Decimal] = {}
        for pos in open_positions:
            sec = SECTOR_ETF_MAP.get(pos.ticker.upper(), "OTHER")
            sector_exposure[sec] = sector_exposure.get(sec, Decimal(0)) + (
                pos.entry_price * pos.quantity
            )

        # Worst-case projection: assume the new position consumes the full
        # half-Kelly cap (V2.7). This intentionally over-estimates so the gate
        # binds *before* the order is sized, not after.
        max_position_pct = Decimal(str(settings.risk_max_position_size_pct))
        estimated_proposed = account.equity * max_position_pct
        existing = sector_exposure.get(proposed_sector, Decimal(0))
        projected = existing + estimated_proposed
        projected_pct = float(projected / account.equity)

        limit = float(settings.risk_sector_concentration_limit)
        if projected_pct > limit:
            return GateResult(
                gate=GateName.CORRELATION,
                blocked=True,
                reason=(
                    f"Sector {proposed_sector} would be {projected_pct * 100:.1f}% of equity "
                    f"(limit {limit * 100:.0f}%). Existing: ${float(existing):,.0f}"
                ),
                overrideable=True,
            )
        return GateResult(gate=GateName.CORRELATION, blocked=False)

    async def _gate_max_open_positions(self, db: AsyncSession) -> GateResult:
        stmt = select(Position).where(
            Position.mode == settings.mode,
            Position.status == PositionStatus.OPEN,
        )
        open_count = len((await db.execute(stmt)).scalars().all())
        cap = settings.risk_max_open_positions
        if open_count >= cap:
            return GateResult(
                gate=GateName.MAX_OPEN_POSITIONS,
                blocked=True,
                reason=f"{open_count}/{cap} open positions",
                overrideable=False,
            )
        return GateResult(gate=GateName.MAX_OPEN_POSITIONS, blocked=False)
