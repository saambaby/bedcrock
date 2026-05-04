"""Finnhub earnings calendar ingestor.

Free tier covers US earnings calendar at /calendar/earnings.

Endpoint:  https://finnhub.io/api/v1/calendar/earnings?from=YYYY-MM-DD&to=YYYY-MM-DD&token=<key>
Response:  {"earningsCalendar": [{"symbol": "AAPL", "date": "2026-05-01", "hour": "amc", ...}]}

This is not a "signal ingestor" — it populates the EarningsCalendar table that
the proximity gate reads. We run it once daily at 06:00 ET.

Reference: https://finnhub.io/docs/api/earnings-calendar
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import EarningsCalendar
from src.ingestors.base import BaseIngestor
from src.logging_config import get_logger
from src.schemas import RawSignal

logger = get_logger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubEarningsIngestor(BaseIngestor):
    """Special-case ingestor: writes to earnings_calendar instead of signals.

    We override `run()` to redirect persistence. The `fetch()` method still
    yields RawSignal-shaped objects only because of the base class contract,
    but they are not used.
    """

    name = "finnhub_earnings"
    source = None  # type: ignore[assignment]  — not used
    interval_seconds = 24 * 60 * 60  # daily

    LOOKAHEAD_DAYS = 60

    async def fetch(self) -> AsyncIterator[RawSignal]:
        # Required by base class; we yield nothing — `run` handles everything.
        if False:
            yield  # type: ignore[unreachable]

    async def run(self, db: AsyncSession) -> int:
        token = settings.finnhub_api_key.get_secret_value()
        if not token:
            logger.warning("finnhub_no_api_key_skipping")
            return 0

        today = datetime.now(UTC).date()
        params = {
            "from": today.isoformat(),
            "to": (today + timedelta(days=self.LOOKAHEAD_DAYS)).isoformat(),
            "token": token,
        }
        url = f"{FINNHUB_BASE}/calendar/earnings"
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("earningsCalendar", []) if isinstance(body, dict) else []

        new_count = 0
        for row in rows:
            symbol = (row.get("symbol") or "").upper()
            date_str = row.get("date") or ""
            if not symbol or not date_str:
                continue
            try:
                earnings_dt = datetime.fromisoformat(date_str + "T00:00:00+00:00")
            except ValueError:
                continue

            stmt = pg_insert(EarningsCalendar).values(
                ticker=symbol,
                earnings_date=earnings_dt,
                when=row.get("hour"),  # "amc" | "bmo" | "dmt"
            ).on_conflict_do_update(
                index_elements=["ticker", "earnings_date"],
                set_={"when": row.get("hour"), "fetched_at": datetime.now(UTC)},
            )
            result = await db.execute(stmt)
            new_count += result.rowcount

        await db.commit()
        await self._heartbeat(db, datetime.now(UTC), None, new_count)
        logger.info("earnings_refresh_complete", upserted=new_count)
        return new_count
