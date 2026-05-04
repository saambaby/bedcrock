"""Indicator computer.

For each ticker we want to score, we:
  1. Fetch OHLCV (250 bars of daily, plus SPY and the sector ETF for relative strength)
  2. Compute SMAs, ATR, RSI, IV percentile (deferred — requires options data),
     ADV, RS vs SPY/sector, swing high/low.
  3. Tag trend regime.
  4. Persist to `indicators` table and return an IndicatorSnapshot.

Cached for 24 hours per ticker. Cache hit returns the persisted row directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Indicators as IndicatorsRow
from src.ingestors.ohlcv import OHLCVFetcher
from src.logging_config import get_logger
from src.schemas import IndicatorSnapshot

logger = get_logger(__name__)

CACHE_HOURS = 24

# Crude sector-ETF map; expand as you watch more names
SECTOR_ETF: dict[str, str] = {
    # Tech / semis
    "NVDA": "SMH", "AMD": "SMH", "AVGO": "SMH", "INTC": "SMH", "TSM": "SMH",
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLK", "META": "XLK", "ORCL": "XLK",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "OXY": "XLE",
    # Healthcare
    "UNH": "XLV", "JNJ": "XLV", "LLY": "XLV", "PFE": "XLV",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    # Defense
    "LMT": "ITA", "RTX": "ITA", "NOC": "ITA", "GD": "ITA", "BA": "ITA",
}


class IndicatorComputer:
    def __init__(self, fetcher: OHLCVFetcher | None = None) -> None:
        self._fetcher = fetcher or OHLCVFetcher()
        self._owns_fetcher = fetcher is None

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self._fetcher.aclose()

    async def get_or_compute(
        self, db: AsyncSession, ticker: str, force_refresh: bool = False
    ) -> IndicatorSnapshot | None:
        ticker = ticker.upper()

        if not force_refresh:
            cached = await self._latest_cached(db, ticker)
            if cached and self._is_fresh(cached):
                return self._row_to_snapshot(cached)

        snap = await self._compute(ticker)
        if snap is None:
            return None

        # Persist
        row = IndicatorsRow(
            ticker=ticker,
            computed_at=snap.computed_at,
            price=snap.price,
            sma_50=snap.sma_50,
            sma_200=snap.sma_200,
            atr_20=snap.atr_20,
            rsi_14=snap.rsi_14,
            iv_percentile_30d=snap.iv_percentile_30d,
            adv_30d_usd=snap.adv_30d_usd,
            rs_vs_spy_60d=snap.rs_vs_spy_60d,
            rs_vs_sector_60d=snap.rs_vs_sector_60d,
            swing_high_90d=snap.swing_high_90d,
            swing_low_90d=snap.swing_low_90d,
            sector_etf=snap.sector_etf,
            trend=snap.trend,
        )
        db.add(row)
        await db.commit()
        return snap

    async def _latest_cached(self, db: AsyncSession, ticker: str) -> IndicatorsRow | None:
        stmt = (
            select(IndicatorsRow)
            .where(IndicatorsRow.ticker == ticker)
            .order_by(IndicatorsRow.computed_at.desc())
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _is_fresh(row: IndicatorsRow) -> bool:
        age = datetime.now(UTC) - row.computed_at
        return age < timedelta(hours=CACHE_HOURS)

    @staticmethod
    def _row_to_snapshot(row: IndicatorsRow) -> IndicatorSnapshot:
        return IndicatorSnapshot(
            ticker=row.ticker,
            computed_at=row.computed_at,
            price=row.price,
            sma_50=row.sma_50,
            sma_200=row.sma_200,
            atr_20=row.atr_20,
            rsi_14=row.rsi_14,
            iv_percentile_30d=row.iv_percentile_30d,
            adv_30d_usd=row.adv_30d_usd,
            rs_vs_spy_60d=row.rs_vs_spy_60d,
            rs_vs_sector_60d=row.rs_vs_sector_60d,
            swing_high_90d=row.swing_high_90d,
            swing_low_90d=row.swing_low_90d,
            sector_etf=row.sector_etf,
            trend=row.trend,
        )

    async def _compute(self, ticker: str) -> IndicatorSnapshot | None:
        sector_etf = SECTOR_ETF.get(ticker, "SPY")

        df = await self._fetcher.fetch_daily(ticker, lookback_days=250)
        if df.empty or len(df) < 50:
            logger.warning("insufficient_ohlcv", ticker=ticker, rows=len(df))
            return None

        spy_df = await self._fetcher.fetch_daily("SPY", lookback_days=250)
        sector_df = (
            df if sector_etf == ticker
            else await self._fetcher.fetch_daily(sector_etf, lookback_days=250)
        )

        return self._calculate(ticker, df, spy_df, sector_df, sector_etf)

    @staticmethod
    def _calculate(
        ticker: str,
        df: pd.DataFrame,
        spy_df: pd.DataFrame,
        sector_df: pd.DataFrame,
        sector_etf: str,
    ) -> IndicatorSnapshot:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        _volume = df["volume"]  # noqa: F841 — reserved for future ADV calc

        # SMAs
        sma_50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else None
        sma_200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None

        # ATR(20) — Wilder's smoothing
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_20 = tr.rolling(20).mean().iloc[-1] if len(tr) >= 20 else None

        # RSI(14)
        delta = close.diff()
        up = delta.clip(lower=0).rolling(14).mean()
        down = (-delta.clip(upper=0)).rolling(14).mean()
        rs = up / down.replace(0, pd.NA)
        rsi_14 = (100 - (100 / (1 + rs))).iloc[-1] if len(close) >= 15 else None

        # ADV(30) in USD
        last_30 = df.tail(30)
        adv_30 = float((last_30["close"] * last_30["volume"]).mean()) if len(last_30) > 0 else None

        # Swing high/low (90 days)
        last_90 = df.tail(90)
        swing_high = float(last_90["high"].max()) if len(last_90) > 0 else None
        swing_low = float(last_90["low"].min()) if len(last_90) > 0 else None

        # Relative strength: 60-day return ratio
        def _rs(target: pd.DataFrame, bench: pd.DataFrame) -> float | None:
            if len(target) < 60 or len(bench) < 60:
                return None
            t_ret = float(target["close"].iloc[-1] / target["close"].iloc[-60])
            b_ret = float(bench["close"].iloc[-1] / bench["close"].iloc[-60])
            if b_ret == 0:
                return None
            return t_ret / b_ret

        rs_vs_spy = _rs(df, spy_df)
        rs_vs_sector = _rs(df, sector_df) if sector_etf != ticker else 1.0

        # Trend regime
        price = float(close.iloc[-1])
        if sma_50 is not None and sma_200 is not None:
            if price > float(sma_50) > float(sma_200):
                trend = "uptrend"
            elif price < float(sma_50) < float(sma_200):
                trend = "downtrend"
            else:
                trend = "chop"
        else:
            trend = None

        def _dec(v) -> Decimal | None:
            if v is None or pd.isna(v):
                return None
            return Decimal(str(round(float(v), 6)))

        return IndicatorSnapshot(
            ticker=ticker,
            computed_at=datetime.now(UTC),
            price=_dec(price),
            sma_50=_dec(sma_50),
            sma_200=_dec(sma_200),
            atr_20=_dec(atr_20),
            rsi_14=_dec(rsi_14),
            iv_percentile_30d=None,  # requires options data; deferred
            adv_30d_usd=_dec(adv_30),
            rs_vs_spy_60d=_dec(rs_vs_spy),
            rs_vs_sector_60d=_dec(rs_vs_sector),
            swing_high_90d=_dec(swing_high),
            swing_low_90d=_dec(swing_low),
            sector_etf=sector_etf,
            trend=trend,
        )
