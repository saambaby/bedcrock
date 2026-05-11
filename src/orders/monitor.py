"""Live monitor — the always-on listener.

Subscribes to IBKR's order/trade events via ib_async and translates fills into:
  - Position rows (on entry fills)
  - Closure inbox events (on stop/target fills) — for Cowork's hourly run
  - Discord #position-alerts pings

Stops and targets live SERVER-SIDE at the broker as bracket orders. This monitor
only OBSERVES — if the VPS dies, the broker still enforces.

Polling fallback: every 30s we reconcile SENT drafts against actual broker state
in case an event was missed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import make_broker
from src.broker.base import BrokerOrderState
from src.broker.ibkr import IBKRBroker
from src.config import settings
from src.db.models import (
    Action,
    AuditLog,
    CloseReason,
    DraftOrder,
    OrderStatus,
    Position,
    PositionStatus,
)
from src.discord_bot.webhooks import post_position_alert
from src.logging_config import get_logger
from src.safety.reconciler import reconcile_against_broker

logger = get_logger(__name__)


class LiveMonitor:
    """Monitors broker order fills via ib_async events + polling fallback."""

    def __init__(self) -> None:
        self._broker = make_broker()
        self._tasks: list[asyncio.Task] = []
        self._stopped = False
        self._db_factory = None

    async def aclose(self) -> None:
        await self._broker.aclose()

    async def stop(self) -> None:
        """Cancel running tasks and clean up. Idempotent."""
        self._stopped = True
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await self.aclose()

    async def run_forever(self) -> None:
        """Entry point used by the monitor_worker."""
        from src.db.session import SessionLocal

        await self.start(SessionLocal)

    async def start(self, db_factory) -> None:  # noqa: C901
        """db_factory: a callable returning an AsyncSession context manager."""
        self._db_factory = db_factory

        if not settings.ibkr_account:
            logger.warning("live_monitor_no_ibkr_account_skipping")
            return

        # Connect broker
        try:
            await self._broker.connect()
        except Exception as e:
            logger.error("live_monitor_connect_failed", error=str(e))
            return

        # Subscribe to ib_async events if using IBKR
        if isinstance(self._broker, IBKRBroker):
            ib = self._broker._ib
            ib.orderStatusEvent += self._on_order_status
            ib.execDetailsEvent += self._on_exec_details
            logger.info("live_monitor_subscribed_to_ibkr_events")

            # Startup reconciliation — orphans + stales (audit §3.4 + §3.6).
            # Must run before _poll/_keep_alive so we cannot race fills against
            # an unrepaired DB view.
            try:
                async with db_factory() as db:
                    await reconcile_against_broker(self._broker, db)
            except Exception as e:
                logger.error("startup_reconcile_failed", error=str(e))

        async def _poll():
            while not self._stopped:
                try:
                    async with db_factory() as db:
                        await self._reconcile_orders(db)
                except Exception as e:
                    logger.warning("monitor_poll_failed", error=str(e))
                await asyncio.sleep(30)

        async def _keep_alive():
            """ib_async is pure asyncio — no event-loop bridging needed.

            Just keep the task alive so cancellation is observed and we exit
            cleanly when the broker disconnects.
            """
            while not self._stopped and self._broker._ib.isConnected():
                await asyncio.sleep(5)

        poll_task = asyncio.create_task(_poll())
        alive_task = asyncio.create_task(_keep_alive())
        self._tasks = [poll_task, alive_task]
        try:
            await asyncio.gather(poll_task, alive_task)
        except asyncio.CancelledError:
            logger.info("live_monitor_cancelled")

    def _on_order_status(self, trade) -> None:
        """ib_async orderStatusEvent callback (sync — schedule async handler)."""
        if self._db_factory is None:
            return
        status = trade.orderStatus.status.lower() if trade.orderStatus else ""
        if status in ("filled", "cancelled", "inactive"):
            asyncio.create_task(self._handle_order_status(trade))

    def _on_exec_details(self, trade, fill) -> None:
        """ib_async execDetailsEvent callback (sync — schedule async handler)."""
        if self._db_factory is None:
            return
        asyncio.create_task(self._handle_fill(trade, fill))

    async def _handle_order_status(self, trade) -> None:
        """Process order status changes from ib_async."""
        try:
            order = trade.order
            status = trade.orderStatus.status.lower() if trade.orderStatus else ""
            order_ref = getattr(order, "orderRef", None)

            logger.info(
                "ibkr_order_status",
                order_id=order.orderId,
                status=status,
                order_ref=order_ref,
            )

            if status in ("cancelled", "inactive") and order_ref:
                async with self._db_factory() as db:
                    draft = await self._find_draft_by_ref(db, order_ref)
                    if draft and draft.status == OrderStatus.SENT:
                        draft.status = (
                            OrderStatus.CANCELLED
                            if status == "cancelled"
                            else OrderStatus.REJECTED
                        )
                        await db.commit()
        except Exception as e:
            logger.error("handle_order_status_failed", error=str(e))

    async def _handle_fill(self, trade, fill) -> None:
        """Process execution details from ib_async."""
        try:
            order = trade.order
            execution = fill.execution
            order_ref = getattr(order, "orderRef", None)
            parent_id = getattr(order, "parentId", 0)

            logger.info(
                "ibkr_fill",
                order_id=order.orderId,
                parent_id=parent_id,
                shares=execution.shares,
                price=execution.avgPrice,
                order_ref=order_ref,
            )

            async with self._db_factory() as db:
                if parent_id == 0:
                    # Entry fill — this is the parent order
                    draft = await self._find_draft_by_ref(db, order_ref) if order_ref else None
                    await self._on_entry_fill(
                        db=db,
                        draft=draft,
                        broker_order_id=str(order.orderId),
                        ticker=fill.contract.symbol,
                        filled_qty=Decimal(str(execution.shares)),
                        filled_avg=Decimal(str(execution.avgPrice)),
                    )
                else:
                    # Child fill (stop or target hit)
                    await self._on_exit_fill(
                        db=db,
                        parent_order_id=str(parent_id),
                        ticker=fill.contract.symbol,
                        filled_avg=Decimal(str(execution.avgPrice)),
                    )
        except Exception as e:
            logger.error("handle_fill_failed", error=str(e))

    async def _find_draft_by_ref(self, db: AsyncSession, order_ref: str) -> DraftOrder | None:
        """Find a DraftOrder by its orderRef (which we set to draft.id)."""
        try:
            stmt = select(DraftOrder).where(DraftOrder.id == order_ref)
            return (await db.execute(stmt)).scalar_one_or_none()
        except Exception:
            return None

    async def _on_entry_fill(
        self,
        db: AsyncSession,
        draft: DraftOrder | None,
        broker_order_id: str,
        ticker: str,
        filled_qty: Decimal,
        filled_avg: Decimal,
    ) -> None:
        """Handle an entry fill — create a Position row."""
        if draft is None:
            logger.warning("entry_fill_no_draft", ticker=ticker)
            return

        # Idempotency: if a Position already exists for this broker_order_id, skip.
        # Both the ib_async event stream and the 30s reconciler can deliver the
        # same fill — the UNIQUE(broker_order_id) constraint on Position protects
        # at the DB layer, but we check first so we don't waste a transaction.
        existing = (await db.execute(
            select(Position).where(Position.broker_order_id == broker_order_id)
        )).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "entry_fill_already_processed",
                broker_order_id=broker_order_id,
                ticker=ticker,
            )
            if draft.status != OrderStatus.FILLED:
                draft.status = OrderStatus.FILLED
                await db.commit()
            return

        position = Position(
            mode=draft.mode,
            ticker=ticker,
            side=draft.side,
            draft_order_id=draft.id,
            broker_order_id=broker_order_id,
            entry_at=datetime.now(UTC),
            entry_price=filled_avg or draft.entry_limit,
            quantity=filled_qty,
            stop=draft.stop,
            target=draft.target,
            status=PositionStatus.OPEN,
            source_signal_ids=draft.source_signal_ids,
            setup_at_entry=draft.setup,
        )
        db.add(position)
        draft.status = OrderStatus.FILLED

        db.add(AuditLog(
            actor="live_monitor",
            action="position_opened",
            target_kind="position",
            target_id=str(position.id),
            details={
                "ticker": ticker,
                "qty": str(filled_qty),
                "price": str(filled_avg),
            },
        ))
        await db.commit()

        await post_position_alert(
            title=f"ENTRY: {ticker}",
            description=(
                f"Filled {filled_qty} @ ${filled_avg}\n"
                f"Stop: ${draft.stop} | Target: ${draft.target}"
            ),
            color=0x22C55E,
        )

    async def _on_exit_fill(
        self,
        db: AsyncSession,
        parent_order_id: str,
        ticker: str,
        filled_avg: Decimal,
    ) -> None:
        """Handle a child fill (stop or target) — close the Position."""
        stmt = select(Position).where(
            Position.broker_order_id == parent_order_id,
            Position.status == PositionStatus.OPEN,
        )
        position = (await db.execute(stmt)).scalar_one_or_none()
        if position is None:
            logger.warning("child_fill_no_position", parent=parent_order_id, ticker=ticker)
            return

        position.exit_at = datetime.now(UTC)
        position.exit_price = filled_avg
        position.status = PositionStatus.CLOSED

        # Determine reason — distance from stop vs target
        if position.stop is not None and position.target is not None:
            stop_dist = abs(filled_avg - position.stop)
            target_dist = abs(filled_avg - position.target)
            position.close_reason = (
                CloseReason.STOP_HIT if stop_dist < target_dist else CloseReason.TARGET_HIT
            )

        # P&L
        if position.entry_price is not None:
            if position.side == Action.BUY:
                position.pnl_usd = (filled_avg - position.entry_price) * position.quantity
                position.pnl_pct = (filled_avg / position.entry_price - 1) * 100
            else:
                position.pnl_usd = (position.entry_price - filled_avg) * position.quantity
                position.pnl_pct = (1 - filled_avg / position.entry_price) * 100

        db.add(AuditLog(
            actor="live_monitor",
            action="position_closed",
            target_kind="position",
            target_id=str(position.id),
            details={
                "ticker": ticker,
                "exit_price": str(filled_avg),
                "pnl_usd": str(position.pnl_usd) if position.pnl_usd else None,
                "pnl_pct": str(position.pnl_pct) if position.pnl_pct else None,
                "close_reason": position.close_reason.value if position.close_reason else None,
            },
        ))
        await db.commit()

        color = 0x22C55E if (position.pnl_usd or 0) > 0 else 0xEF4444
        reason = position.close_reason.value if position.close_reason else "unknown"
        await post_position_alert(
            title=f"CLOSE: {ticker} ({reason})",
            description=(
                f"Exit: ${filled_avg} | "
                f"P&L: ${position.pnl_usd:.2f} ({position.pnl_pct:.2f}%)"
            ),
            color=color,
        )

    async def _reconcile_orders(self, db: AsyncSession) -> None:
        """Polling fallback. Find SENT drafts whose broker says are filled but DB hasn't caught up."""
        stmt = select(DraftOrder).where(DraftOrder.status == OrderStatus.SENT)
        sent = (await db.execute(stmt)).scalars().all()
        for draft in sent:
            if not draft.broker_order_id:
                continue
            try:
                bo = await self._broker.get_order(draft.broker_order_id)
                if bo.state == BrokerOrderState.FILLED and bo.filled_avg_price:
                    # Drift repair: if a Position already exists for this
                    # broker_order_id (the ws fill won the race), don't try to
                    # insert a duplicate — just heal the draft status.
                    existing = (await db.execute(
                        select(Position).where(
                            Position.broker_order_id == bo.broker_order_id
                        )
                    )).scalar_one_or_none()
                    if existing is not None:
                        if draft.status != OrderStatus.FILLED:
                            draft.status = OrderStatus.FILLED
                            await db.commit()
                            logger.info(
                                "reconcile_repaired_draft_status",
                                draft_id=str(draft.id),
                                broker_order_id=bo.broker_order_id,
                            )
                        continue

                    await self._on_entry_fill(
                        db=db,
                        draft=draft,
                        broker_order_id=bo.broker_order_id,
                        ticker=draft.ticker,
                        filled_qty=bo.filled_qty,
                        filled_avg=bo.filled_avg_price,
                    )
            except Exception as e:
                logger.debug("reconcile_skip", draft_id=str(draft.id), error=str(e))
