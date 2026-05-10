"""End-of-session worker.

Runs once per US trading day, after the close (default 16:30 ET):

  1. Fetch broker account snapshot (equity, cash, positions value)
  2. Compute daily P&L delta from yesterday's snapshot
  3. Persist EquitySnapshot row
  4. Compute SPY benchmark return (for that day) — for relative comparison
  5. Write 05 Daily/{YYYY-MM-DD}.md with positions + P&L + signal cluster
  6. Post #system-health summary to Discord

Cron: 30 16 * * 1-5 (4:30 PM ET, weekdays). Or run via `python -m src.workers.eod_worker`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.broker import get_broker
from src.config import settings
from src.db.models import (
    EquitySnapshot,
    Position,
    PositionStatus,
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

        # Pull today's open positions and signals for the daily note
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

    write_daily_note(
        date=today,
        equity=account.equity,
        cash=account.cash,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
        open_positions=open_positions,
        signal_count=len(today_signals),
    )

    color = COLOR_INFO
    desc = (
        f"**Equity:** ${account.equity:,.2f}\n"
        f"**Daily P&L:** ${daily_pnl:,.2f} ({daily_pnl_pct:.2f}%)\n"
        f"**Open positions:** {len(open_positions)}\n"
        f"**New signals today:** {len(today_signals)}\n"
    )
    await post_system_health(
        title=f"📊 EOD — {today.isoformat()} ({settings.mode.value})",
        description=desc,
        color=color,
    )

    await dispose()
    logger.info("eod_worker_done")


def write_daily_note(
    *,
    date,
    equity: Decimal,
    cash: Decimal,
    daily_pnl: Decimal,
    daily_pnl_pct: Decimal,
    open_positions: list,
    signal_count: int,
) -> Path:
    """Write the 05 Daily/{date}.md note. Cowork's morning prompt reads this."""
    base = settings.vault_path / "05 Daily"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{date.isoformat()}.md"

    fm = {
        "type": "daily",
        "mode": settings.mode.value,
        "date": date.isoformat(),
        "equity": float(equity),
        "cash": float(cash),
        "daily_pnl": float(daily_pnl),
        "daily_pnl_pct": float(daily_pnl_pct),
        "open_positions_count": len(open_positions),
        "signal_count": signal_count,
    }
    body = (
        f"# {date.isoformat()} — Daily ({settings.mode.value})\n\n"
        f"**Equity:** ${equity:,.2f}\n"
        f"**Daily P&L:** ${daily_pnl:,.2f} ({daily_pnl_pct:.2f}%)\n\n"
        f"## Open Positions ({len(open_positions)})\n\n"
    )
    for p in open_positions:
        body += f"- [[02 Open Positions/{p.ticker}-{p.entry_at.strftime('%Y-%m-%d')}|{p.ticker}]] — {p.side.value} @ ${p.entry_price}\n"
    body += f"\n## Signals today: {signal_count}\n"

    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body,
        encoding="utf-8",
    )
    return path


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
