"""Unusual Whales ingestors.

Two endpoints used:

1. /api/option-trades/flow-alerts
   Real-time options flow with size/conviction filters.
   We pull the recent feed and emit one signal per alert.

2. /api/congress/recent-trades
   UW's congress feed — overlaps with Quiver but is sometimes faster.

Auth: Authorization: Bearer <token>
Base: https://api.unusualwhales.com

Reference: https://api.unusualwhales.com/docs
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

UW_BASE = "https://api.unusualwhales.com"


class _UWIngestorMixin:
    """Shared auth setup for UW ingestors."""

    def _headers(self) -> dict[str, str]:
        token = settings.unusual_whales_api_key.get_secret_value()
        if not token:
            return {}
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }


class UWFlowIngestor(_UWIngestorMixin, BaseIngestor):
    """Options flow alerts. We treat aggressive call openers as bullish, puts as bearish."""

    name = "uw_flow"
    source = SignalSource.UW_FLOW
    interval_seconds = 5 * 60  # 5 minutes
    requires_market_hours = True

    # Minimum total premium ($) to consider — filters noise
    MIN_PREMIUM = 100_000

    async def fetch(self) -> AsyncIterator[RawSignal]:
        headers = self._headers()
        if not headers:
            logger.warning("uw_no_api_key_skipping", ingestor=self.name)
            return

        url = f"{UW_BASE}/api/option-trades/flow-alerts"
        params = {
            "limit": 200,
            "min_premium": self.MIN_PREMIUM,
            "size_greater_oi": "true",  # opening positions only (new money)
            "is_otm": "true",
        }
        resp = await self._http.get(url, headers=headers, params=params)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", []) if isinstance(body, dict) else []

        for row in rows:
            try:
                signal = self._row_to_signal(row)
                if signal:
                    yield signal
            except Exception as e:
                logger.debug("uw_flow_row_skip", error=str(e))

    def _row_to_signal(self, row: dict) -> RawSignal | None:
        ticker = (row.get("ticker") or row.get("underlying_symbol") or "").upper()
        if not ticker:
            return None

        opt_type = (row.get("type") or row.get("option_type") or "").lower()
        if "call" in opt_type:
            action = Action.BUY
        elif "put" in opt_type:
            action = Action.SELL
        else:
            return None

        # UW timestamps in seconds-since-epoch or ISO depending on field
        ts = row.get("created_at") or row.get("executed_at") or row.get("timestamp")
        disclosed_at = self._parse_ts(ts) or datetime.now(UTC)

        premium_str = row.get("total_premium") or row.get("premium") or "0"
        try:
            premium = Decimal(str(premium_str))
        except Exception:
            premium = Decimal("0")

        external_id = (
            row.get("id")
            or row.get("flow_alert_id")
            or f"{ticker}:{disclosed_at.isoformat()}:{premium}"
        )

        return RawSignal(
            source=SignalSource.UW_FLOW,
            source_external_id=str(external_id),
            ticker=ticker,
            action=action,
            disclosed_at=disclosed_at,
            trade_date=disclosed_at,
            trader_slug=None,  # anonymous flow
            size_low_usd=premium,
            size_high_usd=premium,
            raw=row,
        )

    @staticmethod
    def _parse_ts(value) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        if isinstance(value, str):
            try:
                if value.replace(".", "").isdigit():
                    return datetime.fromtimestamp(float(value), tz=UTC)
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None


class UWCongressIngestor(_UWIngestorMixin, BaseIngestor):
    """UW's congressional trades feed. Often slightly faster than Quiver."""

    name = "uw_congress"
    source = SignalSource.UW_CONGRESS
    interval_seconds = 30 * 60

    async def fetch(self) -> AsyncIterator[RawSignal]:
        headers = self._headers()
        if not headers:
            logger.warning("uw_no_api_key_skipping", ingestor=self.name)
            return

        url = f"{UW_BASE}/api/congress/recent-trades"
        resp = await self._http.get(url, headers=headers, params={"limit": 100})
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", []) if isinstance(body, dict) else []

        for row in rows:
            try:
                signal = self._row_to_signal(row)
                if signal:
                    yield signal
            except Exception as e:
                logger.debug("uw_congress_row_skip", error=str(e))

    def _row_to_signal(self, row: dict) -> RawSignal | None:
        ticker = (row.get("ticker") or "").upper()
        rep = row.get("politician") or row.get("reporter") or ""
        tx = (row.get("type") or row.get("transaction") or "").lower()

        if not ticker or not rep:
            return None

        if "purchase" in tx or "buy" in tx:
            action = Action.BUY
        elif "sale" in tx or "sell" in tx:
            action = Action.SELL
        else:
            return None

        disclosed = self._parse_date(row.get("filed_at") or row.get("filed"))
        traded = self._parse_date(row.get("traded_at") or row.get("traded"))
        if not disclosed:
            disclosed = datetime.now(UTC)

        slug = rep.lower().replace(",", "").replace(".", "").replace(" ", "-")[:64]

        amount = row.get("amount") or row.get("range") or ""
        size_low, size_high = self._parse_amount(str(amount))

        external_id = (
            row.get("id")
            or f"uwc:{slug}:{ticker}:{action.value}:"
              f"{traded.date().isoformat() if traded else 'na'}"
        )

        return RawSignal(
            source=SignalSource.UW_CONGRESS,
            source_external_id=str(external_id),
            ticker=ticker,
            action=action,
            disclosed_at=disclosed,
            trade_date=traded,
            trader_slug=slug,
            trader_display_name=rep,
            trader_kind="politician",
            size_low_usd=size_low,
            size_high_usd=size_high,
            raw=row,
        )

    @staticmethod
    def _parse_date(value) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            s = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except ValueError:
            try:
                return datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return None

    @staticmethod
    def _parse_amount(s: str) -> tuple[Decimal | None, Decimal | None]:
        s = s.replace("$", "").replace(",", "").strip()
        if not s:
            return None, None
        if "-" in s:
            lo, hi = s.split("-", 1)
            try:
                return Decimal(lo.strip()), Decimal(hi.strip())
            except Exception:
                return None, None
        try:
            v = Decimal(s)
            return v, v
        except Exception:
            return None, None
