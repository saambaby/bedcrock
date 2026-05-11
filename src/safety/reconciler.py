"""Broker safety reconciler — periodic and startup invariants.

Two responsibilities:

1. ``audit_open_order_tifs(broker)`` — walks all open IBKR trades and re-issues
   any bracket child whose ``tif`` drifted off ``GTC``. Called on a 30s loop
   from ``monitor_worker`` so a manual TWS edit cannot silently expose us to
   overnight gap risk.

2. ``reconcile_against_broker(broker, db)`` — startup-time sweep that compares
   IBKR's current positions against our DB. Orphans (broker has, DB doesn't)
   raise an alert. Stales (DB has, broker doesn't) are marked
   ``CloseReason.EXTERNAL``. See audit §3.4 + §3.6 and plan v2 §V2.4.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.ibkr import IBKRBroker
from src.config import settings
from src.db.models import (
    AuditLog,
    CloseReason,
    Position,
    PositionStatus,
)
from src.discord_bot.webhooks import post_position_alert, post_system_health
from src.logging_config import get_logger

logger = get_logger(__name__)


async def audit_open_order_tifs(broker: IBKRBroker) -> list[int]:
    """Walk all open orders. Re-issue any child with ``tif != 'GTC'``.

    Returns the list of order IDs that were repaired.
    """
    ib = broker._ib
    repaired: list[int] = []
    if not ib.isConnected():
        return repaired

    for trade in ib.openTrades():
        order = trade.order
        if not getattr(order, "parentId", 0):
            continue  # parent orders may legitimately be DAY
        if order.orderType not in ("STP", "STP LMT", "TRAIL", "LMT"):
            continue
        if order.tif == "GTC":
            continue

        original_tif = order.tif
        logger.error(
            "child_order_not_gtc",
            order_id=order.orderId,
            tif=original_tif,
            parent_id=order.parentId,
            order_type=order.orderType,
        )
        # Cancel + reissue with GTC + outsideRth
        ib.cancelOrder(order)
        order.tif = "GTC"
        order.outsideRth = True
        order.orderId = 0  # force IBKR to assign a new ID
        ib.placeOrder(trade.contract, order)
        repaired.append(order.orderId)
        try:
            await post_system_health(
                title=f"Repaired non-GTC child order on {trade.contract.symbol}",
                body=(
                    f"Was tif={original_tif}, parent={order.parentId}. "
                    "Re-issued as GTC."
                ),
                ok=False,
            )
        except Exception as e:
            logger.warning("repair_alert_failed", error=str(e))
    return repaired


async def reconcile_against_broker(
    broker: IBKRBroker, db: AsyncSession
) -> None:
    """On startup: any IBKR position not in our DB → orphan alert.
    Any DB position not in IBKR → mark closed-externally."""
    ib = broker._ib
    if not ib.isConnected():
        logger.warning("reconcile_skipped_not_connected")
        return

    # Force a fresh refresh, do not trust cache
    await ib.reqPositionsAsync()
    ibkr = {p.contract.symbol: p for p in ib.positions() if p.position != 0}

    db_open = {
        p.ticker: p
        for p in (
            await db.execute(
                select(Position).where(
                    Position.mode == settings.mode,
                    Position.status == PositionStatus.OPEN,
                )
            )
        )
        .scalars()
        .all()
    }

    # Orphans in IBKR (entered manually, or DB row was lost)
    for sym, ibp in ibkr.items():
        if sym not in db_open:
            logger.error("orphan_ibkr_position", symbol=sym, qty=ibp.position)
            db.add(
                AuditLog(
                    actor="reconciler",
                    action="orphan_ibkr_detected",
                    target_kind="position",
                    target_id=sym,
                    details={
                        "qty": str(ibp.position),
                        "avg_cost": str(ibp.avgCost),
                    },
                )
            )
            try:
                await post_position_alert(
                    title=f"ORPHAN: {sym}",
                    description=(
                        f"IBKR shows {ibp.position} @ ${ibp.avgCost} — "
                        "no DB record."
                    ),
                    color=0xFBBF24,
                )
            except Exception as e:
                logger.warning("orphan_alert_failed", error=str(e))

    # Stale in DB (closed externally, e.g. via IBKR mobile app)
    for sym, dbp in db_open.items():
        if sym not in ibkr:
            logger.warning("stale_db_position", symbol=sym, db_id=str(dbp.id))
            dbp.status = PositionStatus.CLOSED
            dbp.close_reason = CloseReason.EXTERNAL
            dbp.exit_at = datetime.now(UTC)
            db.add(
                AuditLog(
                    actor="reconciler",
                    action="closed_externally",
                    target_kind="position",
                    target_id=str(dbp.id),
                    details={"ticker": sym},
                )
            )

    await db.commit()
