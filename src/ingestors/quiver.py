"""Quiver Quantitative — congressional trades ingestor.

Real endpoint: https://api.quiverquant.com/beta/live/congresstrading
Auth: Authorization: Bearer <token>

Quiver also exposes a `quiverquant` Python package, but it's sync and not
ergonomic for our async stack. We hit the REST API directly.

Reference: https://api.quiverquant.com/docs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from src.config import settings
from src.db.models import Action, SignalSource
from src.ingestors.base import BaseIngestor
from src.logging_config import get_logger
from src.schemas import RawSignal

logger = get_logger(__name__)

QUIVER_BASE = "https://api.quiverquant.com/beta"
RECENT_CONGRESS_PATH = "/live/congresstrading"
HISTORICAL_CONGRESS_PATH = "/historical/congresstrading"


class QuiverCongressIngestor(BaseIngestor):
    name = "quiver_congress"
    source = SignalSource.QUIVER_CONGRESS
    interval_seconds = 30 * 60  # 30 minutes

    async def fetch(self) -> AsyncIterator[RawSignal]:
        token = settings.quiver_api_key.get_secret_value()
        if not token:
            logger.warning("quiver_no_api_key_skipping")
            return

        url = QUIVER_BASE + RECENT_CONGRESS_PATH
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        resp = await self._http.get(url, headers=headers)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            logger.error("quiver_unexpected_payload", payload_type=type(rows).__name__)
            return

        for row in rows:
            try:
                signal = self._row_to_signal(row)
                if signal:
                    yield signal
            except Exception as e:
                logger.debug("quiver_row_skip", error=str(e), row=row)

    def _row_to_signal(self, row: dict) -> RawSignal | None:
        ticker = (row.get("Ticker") or "").upper()
        rep = row.get("Representative") or row.get("Senator") or ""
        if not ticker or not rep:
            return None

        # Quiver's Transaction field examples: "Purchase", "Sale (Full)", "Sale (Partial)", "Exchange"
        tx = (row.get("Transaction") or "").lower()
        if "purchase" in tx:
            action = Action.BUY
        elif "sale" in tx:
            action = Action.SELL
        else:
            return None

        # Trade date (when the politician traded)
        trade_date_str = row.get("TransactionDate") or row.get("Traded")
        trade_date = self._parse_date(trade_date_str)

        # Disclosure date (when filed) — we treat this as disclosed_at
        disclosed_str = row.get("ReportDate") or row.get("Filed") or row.get("Disclosed")
        disclosed_at = self._parse_date(disclosed_str) or datetime.now(UTC)

        # Quiver typically returns size as Range or Amount
        size_low, size_high = self._parse_size_range(row)

        slug = self._slug(rep)
        kind = "politician"

        external_id = (
            f"{slug}:{ticker}:{action.value}:"
            f"{trade_date.date().isoformat() if trade_date else 'na'}:"
            f"{disclosed_at.date().isoformat()}"
        )

        return RawSignal(
            source=SignalSource.QUIVER_CONGRESS,
            source_external_id=external_id,
            ticker=ticker,
            action=action,
            disclosed_at=disclosed_at,
            trade_date=trade_date,
            trader_slug=slug,
            trader_display_name=rep,
            trader_kind=kind,
            size_low_usd=size_low,
            size_high_usd=size_high,
            raw=row,
        )

    @staticmethod
    def _slug(name: str) -> str:
        s = name.lower().replace(",", "").replace(".", "")
        s = "-".join(s.split())
        return s[:64]

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_size_range(row: dict) -> tuple[Decimal | None, Decimal | None]:
        """Quiver provides size as either a Range (e.g. '$1,001 - $15,000') or an Amount."""
        rng = row.get("Range") or row.get("Amount") or ""
        if not rng:
            return None, None
        rng = str(rng).replace("$", "").replace(",", "").strip()
        if "-" in rng:
            lo, hi = rng.split("-", 1)
            try:
                return Decimal(lo.strip()), Decimal(hi.strip())
            except Exception:
                return None, None
        try:
            v = Decimal(rng)
            return v, v
        except Exception:
            return None, None
