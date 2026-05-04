"""Hard gates.

Gates are binary blocks applied after scoring. A signal can have a perfect
score and still be blocked by a gate (e.g., earnings in 2 days). Blocked
signals are still persisted with `gate_blocked=True` and `gates_failed`
populated, so the weekly synthesis can ask "would the blocked trades have
won anyway?"
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

        # Correlation, event-proximity, daily-kill require richer context;
        # deferred to v0.2. For now they pass.
        results.append(GateResult(gate=GateName.CORRELATION, blocked=False))
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
