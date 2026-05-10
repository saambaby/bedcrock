"""Intraday daily P&L tracker.

Provides `update_daily_pnl(db, broker)` which computes today's P&L vs.
the start-of-day `EquitySnapshot` and upserts a `DailyState` row so the
`daily_kill_switch` gate can read live data.

The 60s refresh loop lives in `monitor_worker`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.base import BrokerAdapter
from src.config import settings
from src.db.models import DailyState, EquitySnapshot
from src.logging_config import get_logger

logger = get_logger(__name__)

DAILY_PNL_INTERVAL_SECONDS = 60


async def update_daily_pnl(
    db: AsyncSession, broker: BrokerAdapter
) -> DailyState | None:
    """Compute today's intraday P&L vs. the start-of-day equity snapshot
    and upsert into `DailyState`.

    Returns the resulting DailyState row, or None if there is no SOD
    snapshot yet (e.g. before the pre-open snapshot has been written).
    """
    today = date.today()
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    sod_stmt = (
        select(EquitySnapshot)
        .where(
            EquitySnapshot.mode == settings.mode,
            EquitySnapshot.snapshot_date >= today_dt,
        )
        .order_by(EquitySnapshot.snapshot_date.asc())
        .limit(1)
    )
    sod = (await db.execute(sod_stmt)).scalar_one_or_none()
    if sod is None or sod.equity <= 0:
        logger.debug("update_daily_pnl_no_sod_snapshot", date=today.isoformat())
        return None

    account = await broker.get_account()
    pnl_pct = (account.equity - sod.equity) / sod.equity * Decimal("100")
    pnl_pct = pnl_pct.quantize(Decimal("0.0001"))

    stmt = pg_insert(DailyState).values(
        date=today,
        mode=settings.mode,
        daily_pnl_pct=pnl_pct,
        equity_at_open=sod.equity,
    ).on_conflict_do_update(
        index_elements=["date", "mode"],
        set_={
            "daily_pnl_pct": pnl_pct,
            "equity_at_open": sod.equity,
        },
    )
    await db.execute(stmt)
    await db.commit()

    logger.info(
        "update_daily_pnl",
        date=today.isoformat(),
        mode=settings.mode.value,
        equity=str(account.equity),
        equity_at_open=str(sod.equity),
        daily_pnl_pct=str(pnl_pct),
    )

    return (
        await db.execute(
            select(DailyState).where(
                DailyState.date == today, DailyState.mode == settings.mode
            )
        )
    ).scalar_one()
