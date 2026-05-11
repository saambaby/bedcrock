"""End-of-session worker.

Runs once per US trading day, after the close (default 16:30 ET):

  1. Fetch broker account snapshot (equity, cash, positions value)
  2. Compute daily P&L delta from yesterday's snapshot
  3. Persist EquitySnapshot row
  4. Sunday only: scan pending ScoringProposal rows, run replay() per proposal,
     persist a ScoringReplayReport row and link it back to the proposal.
  5. Post #system-health EOD summary embed to Discord

v0.3.0 dropped all markdown / file-tree writes. The canonical store is Postgres; the reasoning
surface is Claude Code Routines reading via FastAPI; Discord is the alert and
control plane.

Cron: 30 16 * * 1-5 (4:30 PM ET, weekdays). Or run via `python -m src.workers.eod_worker`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.backtest.replay import replay
from src.broker import get_broker
from src.config import settings
from src.db.models import (
    DraftOrder,
    EquitySnapshot,
    OrderStatus,
    Position,
    PositionStatus,
    ScoringProposal,
    ScoringReplayReport,
    Signal,
)
from src.db.session import SessionLocal, dispose
from src.discord_bot.webhooks import COLOR_INFO, post_system_health
from src.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    today = datetime.now(UTC).date()
    logger.info("eod_worker_starting", date=today.isoformat(), mode=settings.mode.value)

    broker = get_broker()
    try:
        await broker.connect()
        account = await broker.get_account()
    except Exception as e:
        logger.error("eod_get_account_failed", error=str(e))
        await broker.disconnect()
        await dispose()
        return
    finally:
        await broker.disconnect()

    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    async with SessionLocal() as db:
        # Look up yesterday's snapshot for delta
        prev_stmt = (
            select(EquitySnapshot)
            .where(
                EquitySnapshot.mode == settings.mode,
                EquitySnapshot.snapshot_date < today_dt,
            )
            .order_by(EquitySnapshot.snapshot_date.desc())
            .limit(1)
        )
        prev = (await db.execute(prev_stmt)).scalar_one_or_none()

        if prev is not None:
            daily_pnl = account.equity - prev.equity
            daily_pnl_pct = (daily_pnl / prev.equity * 100) if prev.equity > 0 else Decimal("0")
        else:
            daily_pnl = Decimal("0")
            daily_pnl_pct = Decimal("0")

        # Upsert today's snapshot
        stmt = pg_insert(EquitySnapshot).values(
            mode=settings.mode,
            snapshot_date=today_dt,
            equity=account.equity,
            cash=account.cash,
            positions_value=account.positions_value,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
        ).on_conflict_do_update(
            index_elements=["mode", "snapshot_date"],
            set_={
                "equity": account.equity,
                "cash": account.cash,
                "positions_value": account.positions_value,
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
            },
        )
        await db.execute(stmt)
        await db.commit()

        # Pull today's open positions and signals for the EOD summary
        open_positions = (await db.execute(
            select(Position).where(
                Position.status == PositionStatus.OPEN,
                Position.mode == settings.mode,
            )
        )).scalars().all()

        # Signals from today (UTC day)
        today_signals = (await db.execute(
            select(Signal).where(
                Signal.disclosed_at >= today_dt,
            )
        )).scalars().all()

        # Today's draft orders, broken out by lifecycle status for the summary.
        today_drafts = (await db.execute(
            select(DraftOrder).where(
                DraftOrder.created_at >= today_dt,
                DraftOrder.mode == settings.mode,
            )
        )).scalars().all()

    drafts_created = len(today_drafts)
    drafts_confirmed = sum(
        1 for d in today_drafts
        if d.status in (OrderStatus.SENT, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
    )
    drafts_skipped = sum(1 for d in today_drafts if d.status == OrderStatus.SKIPPED)

    desc = (
        f"**Equity:** ${account.equity:,.2f}\n"
        f"**Daily P&L:** ${daily_pnl:,.2f} ({daily_pnl_pct:.2f}%)\n"
        f"**Open positions:** {len(open_positions)}\n"
        f"**Signals today:** {len(today_signals)}\n"
        f"**Drafts:** {drafts_created} created / {drafts_confirmed} confirmed / {drafts_skipped} skipped\n"
    )
    await post_system_health(
        title=f"EOD — {today.isoformat()} ({settings.mode.value})",
        description=desc,
        color=COLOR_INFO,
    )

    # Sunday (UTC weekday 6) — replay any pending scoring proposals.
    if datetime.now(UTC).weekday() == 6:
        try:
            await run_weekly_replay()
        except Exception as e:
            logger.error("weekly_replay_failed", error=str(e))

    await dispose()
    logger.info("eod_worker_done")


async def run_weekly_replay() -> None:
    """For each `ScoringProposal` row with status='pending', run `replay()` and
    persist a `ScoringReplayReport`. Link the report back to the proposal via
    `replay_report_id` and stamp `evaluated_at`.

    Replaces the v0.2 markdown flow (read a proposed-rules note, write a
    per-rule replay note). Source of truth is now the
    `scoring_proposals` and `scoring_replay_reports` tables.
    """
    async with SessionLocal() as db:
        pending_stmt = (
            select(ScoringProposal)
            .where(ScoringProposal.status == "pending")
            .order_by(ScoringProposal.proposed_at.asc())
        )
        proposals = (await db.execute(pending_stmt)).scalars().all()

        if not proposals:
            logger.info("weekly_replay_no_pending_proposals")
            return

        logger.info("weekly_replay_starting", n_pending=len(proposals))

        for proposal in proposals:
            weights = {k: float(v) for k, v in (proposal.weights or {}).items()}
            if not weights:
                logger.warning(
                    "weekly_replay_skipping_empty_weights", proposal_id=str(proposal.id)
                )
                continue

            logger.info("weekly_replay_running", proposal_id=str(proposal.id))
            report = await replay(db, weights)

            replay_row = ScoringReplayReport(
                proposal_id=proposal.id,
                in_sample_sharpe=float(report.in_sample_sharpe),
                out_of_sample_sharpe=float(report.out_of_sample_sharpe),
                win_rate=float(report.win_rate),
                profit_factor=float(report.profit_factor),
                total_return_pct=float(report.total_return_pct),
                sharpe_delta_vs_baseline=float(report.sharpe_delta_vs_baseline),
                recommendation=report.recommendation,
            )
            db.add(replay_row)
            await db.flush()  # populate replay_row.id

            proposal.replay_report_id = replay_row.id
            proposal.evaluated_at = datetime.now(UTC)
            # Status remains 'pending' until a human reviews + accepts/rejects.

            await db.commit()

            logger.info(
                "weekly_replay_persisted",
                proposal_id=str(proposal.id),
                report_id=str(replay_row.id),
                recommendation=report.recommendation,
            )


async def write_start_of_day_snapshot() -> None:
    """Write a pre-open `EquitySnapshot` for today so `update_daily_pnl`
    has a baseline.

    Idempotent: if a snapshot already exists for today (e.g. EOD ran for
    a prior session and rolled forward, or this was already invoked),
    it is left untouched. Schedule via cron at ~09:25 ET on weekdays:

        25 9 * * 1-5  python -m src.workers.eod_worker --sod
    """
    configure_logging()
    today = datetime.now(UTC).date()
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    logger.info("sod_snapshot_starting", date=today.isoformat(), mode=settings.mode.value)

    broker = get_broker()
    try:
        await broker.connect()
        account = await broker.get_account()
    except Exception as e:
        logger.error("sod_snapshot_get_account_failed", error=str(e))
        await broker.disconnect()
        await dispose()
        return
    finally:
        await broker.disconnect()

    async with SessionLocal() as db:
        existing = (await db.execute(
            select(EquitySnapshot).where(
                EquitySnapshot.mode == settings.mode,
                EquitySnapshot.snapshot_date == today_dt,
            )
        )).scalar_one_or_none()
        if existing is not None:
            logger.info("sod_snapshot_already_present", date=today.isoformat())
            await dispose()
            return

        stmt = pg_insert(EquitySnapshot).values(
            mode=settings.mode,
            snapshot_date=today_dt,
            equity=account.equity,
            cash=account.cash,
            positions_value=account.positions_value,
            daily_pnl=Decimal("0"),
            daily_pnl_pct=Decimal("0"),
        ).on_conflict_do_nothing(index_elements=["mode", "snapshot_date"])
        await db.execute(stmt)
        await db.commit()

    await dispose()
    logger.info("sod_snapshot_done", equity=str(account.equity))


if __name__ == "__main__":
    import sys
    if "--sod" in sys.argv:
        asyncio.run(write_start_of_day_snapshot())
    else:
        asyncio.run(main())
