"""Broker safety reconciler — periodic and startup invariants.

Two responsibilities:

1. ``audit_open_order_tifs(broker)`` — walks all open broker orders and re-issues
   any bracket child whose ``tif`` drifted off ``GTC``. Called on a 30s loop
   from ``monitor_worker`` so a manual broker-UI edit cannot silently expose us
   to overnight gap risk.

2. ``reconcile_against_broker(broker, db)`` — startup-time sweep that compares
   the broker's current positions against our DB. Orphans (broker has, DB
   doesn't) raise an alert. Stales (DB has, broker doesn't) are marked
   ``CloseReason.EXTERNAL``. See audit §3.4 + §3.6 and plan v2 §V2.4.

Broker-agnostic since v0.4 (Wave C): no concrete ``IBKRBroker`` import; all
state is reached through the ``BrokerAdapter`` contract (``iter_open_orders``,
``iter_positions``, ``repair_child_to_gtc``). The IBKR-specific quirk of
calling ``ib.reqPositionsAsync`` to force a refresh now lives inside
``IBKRBroker.iter_positions``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.base import BrokerAdapter
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


_REPAIRABLE_ORDER_TYPES = {"limit", "stop", "stop_limit", "trailing_stop"}


async def audit_open_order_tifs(broker: BrokerAdapter) -> list[str]:
    """Walk all open orders. Re-issue any child with ``tif != 'gtc'``.

    Returns the list of new broker_order_ids that were created by repair.

    Skips:
      - Orders with ``parent_order_id is None`` (parents may legitimately be DAY).
      - Orders whose ``order_type`` is not a stop/limit/trailing variant.
      - Orders already on ``tif == 'gtc'``.
    """
    repaired: list[str] = []

    async for o in broker.iter_open_orders():
        if o.parent_order_id is None:
            continue  # parents are intentionally exempt
        if o.order_type not in _REPAIRABLE_ORDER_TYPES:
            continue
        if (o.tif or "").lower() == "gtc":
            continue

        original_tif = o.tif
        logger.error(
            "child_order_not_gtc",
            broker_order_id=o.broker_order_id,
            tif=original_tif,
            parent_id=o.parent_order_id,
            order_type=o.order_type,
            broker=settings.broker.value,
        )
        try:
            new_id = await broker.repair_child_to_gtc(o.broker_order_id)
        except Exception as e:
            logger.error(
                "child_repair_failed",
                broker_order_id=o.broker_order_id,
                error=str(e),
            )
            continue
        repaired.append(new_id)
        try:
            await post_system_health(
                title=f"Repaired non-GTC child order on {o.ticker}",
                body=(
                    f"Was tif={original_tif}, parent={o.parent_order_id}. "
                    f"Re-issued as GTC. broker={settings.broker.value}"
                ),
                ok=False,
            )
        except Exception as e:
            logger.warning("repair_alert_failed", error=str(e))
    return repaired


async def reconcile_against_broker(
    broker: BrokerAdapter, db: AsyncSession
) -> None:
    """On startup: any broker position not in our DB → orphan alert.
    Any DB position not on broker → mark closed-externally."""
    broker_positions: dict[str, object] = {}
    try:
        async for p in broker.iter_positions():
            if p.quantity == 0:
                continue
            broker_positions[p.ticker] = p
    except Exception as e:
        # IBKRBroker raises if the underlying ib_async socket is down; treat
        # that as "skip this cycle" rather than poisoning startup.
        logger.warning("reconcile_skipped_broker_unavailable", error=str(e))
        return

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

    broker_label = settings.broker.value

    # Orphans on the broker (entered manually, or DB row was lost)
    for sym, bp in broker_positions.items():
        if sym not in db_open:
            qty = getattr(bp, "quantity", None)
            avg = getattr(bp, "avg_entry_price", None)
            logger.error(
                "orphan_broker_position",
                symbol=sym,
                qty=str(qty),
                broker=broker_label,
            )
            db.add(
                AuditLog(
                    actor="reconciler",
                    action="orphan_broker_detected",
                    target_kind="position",
                    target_id=sym,
                    details={
                        "qty": str(qty),
                        "avg_cost": str(avg),
                        "broker": broker_label,
                    },
                )
            )
            try:
                await post_position_alert(
                    title=f"ORPHAN: {sym}",
                    description=(
                        f"{broker_label} shows {qty} @ ${avg} — "
                        "no DB record."
                    ),
                    color=0xFBBF24,
                )
            except Exception as e:
                logger.warning("orphan_alert_failed", error=str(e))

    # Stale in DB (closed externally, e.g. via broker mobile app)
    for sym, dbp in db_open.items():
        if sym not in broker_positions:
            logger.warning(
                "stale_db_position",
                symbol=sym,
                db_id=str(dbp.id),
                broker=broker_label,
            )
            dbp.status = PositionStatus.CLOSED
            dbp.close_reason = CloseReason.EXTERNAL
            dbp.exit_at = datetime.now(UTC)
            db.add(
                AuditLog(
                    actor="reconciler",
                    action="closed_externally",
                    target_kind="position",
                    target_id=str(dbp.id),
                    details={"ticker": sym, "broker": broker_label},
                )
            )

    await db.commit()
