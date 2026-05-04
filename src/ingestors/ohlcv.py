"""OHLCV fetcher.

Returns a pandas DataFrame indexed by date with columns: open, high, low, close, volume.

Strategy:
- If POLYGON_API_KEY is set, use Polygon's aggs endpoint (fast, reliable, free EOD tier).
- Otherwise fall back to yfinance (free, slow, occasionally flaky).

This is not a BaseIngestor — it's called on-demand by the indicator computer.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx
import pandas as pd

from src.config import settings
from src.logging_config import get_logger

logger = get_logger(__name__)

POLYGON_BASE = "https://api.polygon.io"


class OHLCVFetcher:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0)
        )
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def fetch_daily(self, ticker: str, lookback_days: int = 250) -> pd.DataFrame:
        """Daily bars for `lookback_days` calendar days ending today."""
        end = date.today()
        start = end - timedelta(days=lookback_days * 2)  # buffer for non-trading days

        polygon_key = settings.polygon_api_key.get_secret_value()
        if polygon_key:
            try:
                return await self._fetch_polygon(ticker, start, end, polygon_key)
            except Exception as e:
                logger.warning("polygon_fetch_failed_falling_back", ticker=ticker, error=str(e))

        return await self._fetch_yfinance(ticker, start, end)

    async def _fetch_polygon(
        self, ticker: str, start: date, end: date, api_key: str
    ) -> pd.DataFrame:
        url = (
            f"{POLYGON_BASE}/v2/aggs/ticker/{ticker.upper()}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") not in ("OK", "DELAYED"):
            raise RuntimeError(f"Polygon error: {body.get('error') or body.get('status')}")

        results = body.get("results", [])
        if not results:
            return pd.DataFrame()

        df = pd.DataFrame(results)
        df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York").dt.date
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        return df.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)

    async def _fetch_yfinance(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        # yfinance is sync; run in thread to keep the async loop clean
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._yf_sync, ticker, start, end)

    @staticmethod
    def _yf_sync(ticker: str, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf

        df = yf.download(
            ticker, start=start.isoformat(), end=end.isoformat(),
            progress=False, auto_adjust=True, threads=False,
        )
        if df.empty:
            return df
        # yfinance sometimes returns a MultiIndex column when given a single ticker;
        # normalise to lowercase single-level
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index = df.index.date  # type: ignore[assignment]
        return df[["open", "high", "low", "close", "volume"]]
