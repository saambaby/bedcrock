"""Heavy-movement detector. Runs every 5 minutes during market hours.

Computes for each ticker in the watchlist (built from open positions plus
recent high-score signals):
  - volume_spike: today's volume vs 20-day average
  - gap_pct: today's open vs prior close
  - is_52w_breakout: today's high >= prior 52-week high

Writes a Signal row with source=MARKET_MOVEMENT, action inferred from
gap direction. The scorer treats MARKET_MOVEMENT signals as
corroboration for any existing signal on the same ticker in the
last 14 days; they cannot trigger drafts on their own.

Design constraint: this is *not* a primary signal source. It must never
trigger a draft order on its own. The scorer enforces this by returning
score=0 for MARKET_MOVEMENT signals lacking a recent fundamental signal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import (
    Action,
    Position,
    PositionStatus,
    Signal,
    SignalSource,
)
from src.ingestors.base import BaseIngestor
from src.ingestors.ohlcv import OHLCVFetcher
from src.logging_config import get_logger
from src.schemas import RawSignal

logger = get_logger(__name__)

# Hard exclusion — major gap down is panic, not corroboration.
GAP_DOWN_KILL = -0.10
# Lookback bars (trading days) for the 20-day average volume comparison.
LOOKBACK_BARS = 21
# 52-week breakout window (trading days)
WINDOW_52W = 252

NY = ZoneInfo("America/New_York")


class HeavyMovementIngestor(BaseIngestor):
    """Volume / gap / breakout detector for the active watchlist.

    Unlike the disclosure-driven ingestors, this one does not yield
    RawSignals from fetch() — it requires DB access to build the watchlist
    *before* fetching market data, so it overrides `run()` directly.
    """

    name: ClassVar[str] = "heavy_movement"
    source: ClassVar[SignalSource] = SignalSource.MARKET_MOVEMENT
    interval_seconds: ClassVar[int] = settings.movement_check_interval_seconds
    requires_market_hours: ClassVar[bool] = True

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        ohlcv: OHLCVFetcher | None = None,
        now_fn=None,
    ) -> None:
        super().__init__(http_client=http_client)
        self._ohlcv = ohlcv or OHLCVFetcher(http_client=self._http)
        # Injectable clock for tests
        self._now = now_fn or (lambda: datetime.now(UTC))

    async def fetch(self) -> AsyncIterator[RawSignal]:
        # Not used — we override run() directly. Keep this for ABC compat.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    # ------------------------------------------------------------------

    def _is_market_hours(self) -> bool:
        now = self._now().astimezone(NY)
        if now.weekday() >= 5:  # Sat/Sun
            return False
        return time(9, 30) <= now.time() <= time(16, 0)

    async def _build_watchlist(self, db: AsyncSession) -> set[str]:
        """Tickers worth monitoring: open positions + recent high-score signals."""
        cutoff = self._now() - timedelta(days=14)
        open_pos = (await db.execute(
            select(Position.ticker).where(Position.status == PositionStatus.OPEN)
        )).scalars().all()
        recent_high = (await db.execute(
            select(Signal.ticker).where(
                Signal.disclosed_at >= cutoff,
                Signal.score >= 5.0,
                Signal.source != SignalSource.MARKET_MOVEMENT,
            )
        )).scalars().all()
        return {t.upper() for t in open_pos} | {t.upper() for t in recent_high}

    # ------------------------------------------------------------------

    async def run(self, db: AsyncSession) -> int:
        """One execution. Returns the number of NEW signals persisted.

        Idempotent — re-runs in the same day will dedupe via the
        (source, source_external_id) unique index because the external id
        encodes ticker + date + triggers.
        """
        started = self._now()
        new_count = 0
        last_error: str | None = None

        try:
            if not self._is_market_hours():
                logger.debug("heavy_movement_outside_market_hours")
                return 0

            watchlist = await self._build_watchlist(db)
            if not watchlist:
                logger.debug("heavy_movement_empty_watchlist")
                return 0

            for ticker in sorted(watchlist):
                try:
                    inserted = await self._scan_ticker(db, ticker)
                    new_count += inserted
                except Exception as e:
                    logger.warning(
                        "heavy_movement_ticker_failed",
                        ticker=ticker,
                        error=f"{type(e).__name__}: {e}",
                    )
                    continue

            await db.commit()
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.error("ingestor_failed", ingestor=self.name, error=last_error)
            await db.rollback()
            raise
        finally:
            await self._heartbeat(db, started, last_error, new_count)

        logger.info(
            "ingestor_run_complete",
            ingestor=self.name,
            new_signals=new_count,
            duration_s=(self._now() - started).total_seconds(),
        )
        return new_count

    async def _scan_ticker(self, db: AsyncSession, ticker: str) -> int:
        """Fetch bars, compute triggers, persist a Signal if any fired."""
        df = await self._ohlcv.fetch_daily(ticker, lookback_days=WINDOW_52W + 5)
        if df is None or df.empty or len(df) < LOOKBACK_BARS:
            return 0

        today = df.iloc[-1]
        prior_20 = df.iloc[-LOOKBACK_BARS:-1]
        avg_vol = float(prior_20["volume"].mean())
        prior_close = float(prior_20["close"].iloc[-1])
        if not avg_vol or not prior_close:
            return 0

        window = df.iloc[-min(WINDOW_52W, len(df) - 1):-1]  # exclude today
        prior_high_52w = float(window["high"].max()) if not window.empty else 0.0

        today_open = float(today["open"])
        today_high = float(today["high"])
        today_volume = float(today["volume"])
        today_date = df.index[-1]

        volume_ratio = today_volume / avg_vol
        gap_pct = (today_open / prior_close) - 1.0
        is_breakout = prior_high_52w > 0 and today_high >= prior_high_52w

        # Hard exclusion — major gap down is panic, not corroboration
        if gap_pct <= GAP_DOWN_KILL:
            logger.info(
                "heavy_movement_gap_down_kill",
                ticker=ticker,
                gap_pct=gap_pct,
            )
            return 0

        triggers: list[str] = []
        if volume_ratio >= settings.movement_volume_spike_threshold:
            triggers.append(f"vol{volume_ratio:.1f}x")
        if abs(gap_pct) >= settings.movement_gap_threshold:
            triggers.append(f"gap{gap_pct * 100:+.1f}%")
        if is_breakout:
            triggers.append("52w_high")
        if not triggers:
            return 0

        # Direction inferred from gap; breakout-only defaults to BUY
        action = Action.BUY if gap_pct >= 0 else Action.SELL

        # Stable per-day external id (idempotent across re-runs)
        ext_id = f"{ticker}-{today_date.isoformat()}-{'-'.join(triggers)}"

        stmt = pg_insert(Signal).values(
            mode=settings.mode,
            source=SignalSource.MARKET_MOVEMENT,
            source_external_id=ext_id,
            ticker=ticker,
            action=action,
            disclosed_at=self._now(),
            trade_date=datetime.combine(today_date, time(0, 0), tzinfo=UTC),
            raw={
                "volume_ratio": volume_ratio,
                "gap_pct": gap_pct,
                "is_52w_breakout": bool(is_breakout),
                "triggers": triggers,
                "today_open": today_open,
                "today_high": today_high,
                "today_volume": today_volume,
                "prior_close": prior_close,
                "avg_volume_20d": avg_vol,
                "prior_high_52w": prior_high_52w,
            },
        ).on_conflict_do_nothing(index_elements=["source", "source_external_id"])

        result = await db.execute(stmt)
        return int(result.rowcount or 0)

    async def aclose(self) -> None:
        # OHLCVFetcher shares our http client; let base close it.
        await super().aclose()
