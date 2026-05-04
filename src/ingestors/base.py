"""Base class for ingestors.

Every concrete ingestor implements `fetch()` returning an async iterator of
RawSignal. The base handles:
  - dedupe via (source, source_external_id) unique constraint
  - heartbeat updates
  - retry with exponential backoff
  - bulk insert
  - heartbeat record on success/failure
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import settings
from src.db.models import IngestorHeartbeat, Signal, SignalSource, Trader
from src.logging_config import get_logger
from src.schemas import RawSignal

logger = get_logger(__name__)


class BaseIngestor(abc.ABC):
    """Abstract base for all data-source ingestors."""

    name: ClassVar[str]  # e.g., "sec_form4", "quiver_congress"
    source: ClassVar[SignalSource]  # tag for inserted Signal rows
    interval_seconds: ClassVar[int]  # how often the orchestrator runs this
    requires_market_hours: ClassVar[bool] = False

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10),
        )
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @abc.abstractmethod
    async def fetch(self) -> AsyncIterator[RawSignal]:
        """Yield RawSignals from the upstream source.

        Implementations should yield each signal as it's parsed; the base
        will batch-write them.
        """
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    async def run(self, db: AsyncSession) -> int:
        """One execution. Returns the number of NEW signals persisted.

        Idempotent — re-runs over the same upstream window will dedupe via the
        (source, source_external_id) unique index.
        """
        started = datetime.now(UTC)
        new_count = 0
        last_error: str | None = None

        try:
            async for raw in self._with_retry(self.fetch):
                if await self._persist(db, raw):
                    new_count += 1
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
            duration_s=(datetime.now(UTC) - started).total_seconds(),
        )
        return new_count

    async def _persist(self, db: AsyncSession, raw: RawSignal) -> bool:
        """Insert RawSignal as a Signal row. Returns True if newly inserted."""
        # Resolve trader if any
        trader_id = None
        if raw.trader_slug:
            trader_id = await self._upsert_trader(
                db, raw.trader_slug, raw.trader_display_name or raw.trader_slug,
                raw.trader_kind or "unknown",
            )

        stmt = pg_insert(Signal).values(
            mode=settings.mode,
            source=raw.source,
            source_external_id=raw.source_external_id,
            ticker=raw.ticker.upper(),
            action=raw.action,
            trader_id=trader_id,
            disclosed_at=raw.disclosed_at,
            trade_date=raw.trade_date,
            size_low_usd=raw.size_low_usd,
            size_high_usd=raw.size_high_usd,
            raw=raw.raw,
        ).on_conflict_do_nothing(index_elements=["source", "source_external_id"])

        result = await db.execute(stmt)
        return result.rowcount > 0

    async def _upsert_trader(
        self, db: AsyncSession, slug: str, display_name: str, kind: str
    ) -> str:
        """Create or get a trader row, return id."""
        existing = await db.execute(select(Trader).where(Trader.slug == slug))
        row = existing.scalar_one_or_none()
        if row:
            return row.id
        trader = Trader(slug=slug, display_name=display_name, kind=kind)
        db.add(trader)
        await db.flush()  # populate id without committing
        return trader.id

    async def _heartbeat(
        self,
        db: AsyncSession,
        started: datetime,
        last_error: str | None,
        new_count: int,
    ) -> None:
        """Upsert heartbeat record."""
        stmt = pg_insert(IngestorHeartbeat).values(
            ingestor=self.name,
            last_run_at=started,
            last_success_at=None if last_error else started,
            last_error=last_error,
            signals_in_last_run=new_count,
        ).on_conflict_do_update(
            index_elements=["ingestor"],
            set_={
                "last_run_at": started,
                "last_success_at": (
                    IngestorHeartbeat.last_success_at if last_error
                    else started  # type: ignore[arg-type]
                ),
                "last_error": last_error,
                "signals_in_last_run": new_count,
            },
        )
        try:
            await db.execute(stmt)
            await db.commit()
        except Exception:
            await db.rollback()

    async def _with_retry(self, fn):
        """Run an async generator function with exponential-backoff retries."""
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=30),
                retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
                reraise=True,
            ):
                with attempt:
                    async for item in fn():
                        yield item
                    return
        except RetryError as e:
            raise e.last_attempt.exception() from e


class IngestorRegistry:
    """Holds all enabled ingestors. Worker iterates this on each tick."""

    def __init__(self) -> None:
        self._ingestors: list[BaseIngestor] = []

    def register(self, ingestor: BaseIngestor) -> None:
        self._ingestors.append(ingestor)

    def __iter__(self):
        return iter(self._ingestors)

    async def aclose(self) -> None:
        for ing in self._ingestors:
            await ing.aclose()
