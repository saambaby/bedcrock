"""Ingest worker — the orchestration loop.

Schedules each ingestor at its native cadence (settings.ingest_interval_*),
runs the scorer + gates on every new signal, builds drafts for high-score
non-blocked signals, writes everything to the vault, and pings Discord.

Runs forever. Crash-safe — each tick is atomic at the DB level.
"""

from __future__ import annotations

import asyncio
import signal as _signal
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import Action, SignalStatus
from src.db.models import Signal as SignalRow
from src.db.session import SessionLocal, dispose
from src.discord_bot.webhooks import (
    HIGH_SCORE_THRESHOLD,
    post_firehose,
    post_high_score,
    post_system_health,
)
from src.indicators import IndicatorComputer
from src.ingestors import (
    BaseIngestor,
    FinnhubEarningsIngestor,
    HeavyMovementIngestor,
    QuiverCongressIngestor,
    SECForm4Ingestor,
    UWCongressIngestor,
    UWFlowIngestor,
)
from src.logging_config import configure_logging, get_logger
from src.orders.builder import BracketBuilder
from src.schemas import RawSignal, ScoredSignal
from src.scoring import GateEvaluator, Scorer

logger = get_logger(__name__)


def _build_ingestors() -> list[BaseIngestor]:
    return [
        SECForm4Ingestor(),
        QuiverCongressIngestor(),
        UWFlowIngestor(),
        UWCongressIngestor(),
        FinnhubEarningsIngestor(),
        HeavyMovementIngestor(),
    ]


class IngestOrchestrator:
    def __init__(self) -> None:
        self.ingestors = _build_ingestors()
        self.indicator_computer = IndicatorComputer()
        self.scorer = Scorer()
        self.gates = GateEvaluator()
        self.builder = BracketBuilder()

    async def run_once(self) -> None:
        logger.info("orchestrator_tick_start")
        async with SessionLocal() as db:
            for ing in self.ingestors:
                try:
                    await ing.run(db)
                except Exception as e:
                    logger.error("ingestor_failed", ingestor=ing.name, error=str(e))
            await self._score_new_signals(db)
        logger.info("orchestrator_tick_end")

    async def _score_new_signals(self, db: AsyncSession) -> None:
        stmt = (
            select(SignalRow)
            .where(SignalRow.status == SignalStatus.NEW, SignalRow.mode == settings.mode)
            .order_by(SignalRow.disclosed_at.desc())
            .limit(200)
        )
        signals = (await db.execute(stmt)).scalars().all()
        for sig in signals:
            try:
                await self._process_signal(db, sig)
            except Exception as e:
                logger.error("score_signal_failed", id=str(sig.id), error=str(e))

    async def _process_signal(self, db: AsyncSession, sig: SignalRow) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=30)
        prior_stmt = (
            select(SignalRow)
            .where(SignalRow.ticker == sig.ticker, SignalRow.disclosed_at >= cutoff)
            .order_by(SignalRow.disclosed_at.desc())
            .limit(200)
        )
        prior = (await db.execute(prior_stmt)).scalars().all()

        indicators = await self.indicator_computer.get_or_compute(db, sig.ticker)

        raw = RawSignal(
            source=sig.source,
            source_external_id=sig.source_external_id,
            ticker=sig.ticker,
            action=sig.action,
            disclosed_at=sig.disclosed_at,
            trade_date=sig.trade_date,
            trader_slug=sig.trader.slug if sig.trader else None,
            trader_display_name=sig.trader.display_name if sig.trader else None,
            trader_kind=sig.trader.kind if sig.trader else None,
            size_low_usd=sig.size_low_usd,
            size_high_usd=sig.size_high_usd,
            raw=sig.raw,
        )

        score, breakdown = self.scorer.score(raw, list(prior), indicators)
        gate_results = await self.gates.evaluate(db, raw, indicators)
        scored = ScoredSignal(
            raw_signal=raw, score=score, breakdown=breakdown, gate_results=gate_results
        )

        sig.score = score
        sig.score_breakdown = breakdown.model_dump()
        sig.gate_blocked = scored.gate_blocked
        sig.gates_failed = scored.gates_failed
        sig.status = SignalStatus.BLOCKED if scored.gate_blocked else SignalStatus.PROCESSED
        await db.commit()

        try:
            await post_firehose(
                ticker=sig.ticker,
                action=sig.action.value,
                source=sig.source.value,
                score=score,
                trader=(sig.trader.display_name if sig.trader else None),
                gate_blocked=scored.gate_blocked,
            )
        except Exception:
            pass

        if score >= HIGH_SCORE_THRESHOLD and not scored.gate_blocked and indicators is not None:
            # Derive entry zone from current price ±0.5%
            price = indicators.price or Decimal("0")
            half_pct = price * Decimal("0.005")
            entry_low = price - half_pct
            entry_high = price + half_pct
            entry_mid = price

            # Derive stop/target from ATR if available
            atr = indicators.atr_20 or price * Decimal("0.02")
            if sig.action == Action.BUY:
                stop_raw = entry_mid - atr * Decimal("2")
                target_raw = entry_mid + atr * Decimal("3")
            else:
                stop_raw = entry_mid + atr * Decimal("2")
                target_raw = entry_mid - atr * Decimal("3")

            draft = await self.builder.build_draft(
                ticker=sig.ticker,
                side=sig.action,
                entry_zone_low=entry_low,
                entry_zone_high=entry_high,
                stop=stop_raw,
                target=target_raw,
                setup=sig.source.value,
                score=score,
                source_signal_ids=[sig.id],
                indicators=indicators,
                db=db,
            )
            if draft:
                try:
                    await post_high_score(
                        ticker=sig.ticker,
                        action=sig.action.value,
                        source=sig.source.value,
                        score=score,
                        trader=(sig.trader.display_name if sig.trader else None),
                        breakdown=breakdown.model_dump(),
                        draft_id=str(draft.id),
                    )
                except Exception:
                    pass

    async def aclose(self) -> None:
        await self.indicator_computer.aclose()
        for ing in self.ingestors:
            await ing.aclose()


async def main() -> None:
    configure_logging()
    logger.info("ingest_worker_starting", mode=settings.mode.value)

    orchestrator = IngestOrchestrator()
    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        orchestrator.run_once,
        trigger="interval",
        minutes=settings.ingest_interval_fast_min,
        next_run_time=datetime.now(UTC),
        id="ingest_loop",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    stop = asyncio.Event()

    def _on_signal(*_: Any) -> None:
        stop.set()

    loop = asyncio.get_event_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _on_signal)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    finally:
        try:
            await post_system_health(
                title="🛑 Ingest worker stopped",
                body="Shutdown signal received",
                ok=False,
            )
        except Exception:
            pass
        scheduler.shutdown(wait=False)
        await orchestrator.aclose()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
