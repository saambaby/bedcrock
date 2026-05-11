"""Live monitor — the always-on listener.

Subscribes to the broker's trade-update stream (via ``BrokerAdapter
.subscribe_trade_updates``) and translates fills into:
  - Position rows (on entry fills)
  - Position closure rows (on stop/target fills) — read by the hourly-closure skill
  - Discord #position-alerts pings

Stops and targets live SERVER-SIDE at the broker as bracket orders. This monitor
only OBSERVES — if the VPS dies, the broker still enforces.

Polling fallback: every 30s we reconcile SENT drafts against actual broker state
in case an event was missed.

Broker-agnostic since v0.4 (Wave C): no concrete ``IBKRBroker`` import here.
The event stream is whatever the adapter yields; entry-vs-exit fills are now
distinguished via ``TradeUpdate.raw['parent_id']`` (IBKR side) or by matching
``broker_order_id`` against existing positions in the DB.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker import make_broker
from src.broker.base import BrokerAdapter, BrokerOrderState, TradeUpdate
from src.config import Broker, settings
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


_FILL_EVENTS = {"fill", "partial_fill"}
_TERMINAL_NON_FILL_EVENTS = {"canceled", "cancelled", "rejected", "expired"}


class LiveMonitor:
    """Monitors broker order fills via the adapter's trade-update stream +
    polling fallback."""

    def __init__(self) -> None:
        self._broker: BrokerAdapter = make_broker()
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

        # Broker-aware boot guard.
        # IBKR still requires an account id (the TWS API call wants it);
        # Alpaca infers the account from the API key, so no equivalent check.
        if settings.broker is Broker.IBKR and not settings.ibkr_account:
            logger.warning("live_monitor_no_ibkr_account_skipping")
            return

        # Connect broker
        try:
            await self._broker.connect()
        except Exception as e:
            logger.error("live_monitor_connect_failed", error=str(e))
            return

        # Startup reconciliation — orphans + stales (audit §3.4 + §3.6).
        # Must run before the polling/stream loops so we cannot race fills
        # against an unrepaired DB view.
        try:
            async with db_factory() as db:
                await reconcile_against_broker(self._broker, db)
        except Exception as e:
            logger.error("startup_reconcile_failed", error=str(e))

        async def _stream() -> None:
            while not self._stopped:
                try:
                    async for update in self._broker.subscribe_trade_updates():
                        if self._stopped:
                            break
                        try:
                            await self._handle_update(update, db_factory)
                        except Exception as e:
                            logger.error(
                                "handle_update_failed",
                                error=str(e),
                                broker_order_id=update.broker_order_id,
                            )
                except asyncio.CancelledError:
                    raise
                except NotImplementedError:
                    # Broker has no push stream — fall back to polling only.
                    logger.warning("trade_updates_not_implemented_polling_only")
                    return
                except Exception as e:
                    logger.warning("trade_updates_stream_failed", error=str(e))
                    if self._stopped:
                        return
                    await asyncio.sleep(5)

        async def _poll() -> None:
            while not self._stopped:
                try:
                    async with db_factory() as db:
                        await self._reconcile_orders(db)
                except Exception as e:
                    logger.warning("monitor_poll_failed", error=str(e))
                await asyncio.sleep(30)

        stream_task = asyncio.create_task(_stream())
        poll_task = asyncio.create_task(_poll())
        self._tasks = [stream_task, poll_task]
        try:
            await asyncio.gather(stream_task, poll_task)
        except asyncio.CancelledError:
            logger.info("live_monitor_cancelled")

    # ------------------------------------------------------------------
    # Broker-agnostic event handler
    # ------------------------------------------------------------------

    async def _handle_update(self, update: TradeUpdate, db_factory) -> None:
        """Process one broker ``TradeUpdate``. Idempotent.

        Distinguishes entry vs exit fills by:
          1. Checking ``update.raw['parent_id']`` if present (IBKR carries
             the bracket parent id on every child event).
          2. Falling back to a DB lookup: if a Position already exists with
             ``broker_order_id == update.broker_order_id`` it's our parent
             entry; if a Position exists whose ``broker_order_id`` matches
             some other open order's parent we treat the fill as an exit.
        """
        event = (update.event or "").lower()
        logger.info(
            "broker_trade_update",
            update_event=event,
            broker_order_id=update.broker_order_id,
            ticker=update.ticker,
            qty=str(update.filled_qty),
            price=str(update.filled_avg_price) if update.filled_avg_price else None,
            broker=settings.broker.value,
        )

        if event in _TERMINAL_NON_FILL_EVENTS:
            client_ref = update.client_order_id
            if not client_ref:
                return
            async with db_factory() as db:
                draft = await self._find_draft_by_ref(db, client_ref)
                if draft and draft.status == OrderStatus.SENT:
                    draft.status = (
                        OrderStatus.CANCELLED
                        if event in {"canceled", "cancelled"}
                        else OrderStatus.REJECTED
                    )
                    await db.commit()
            return

        if event not in _FILL_EVENTS:
            return  # "new", "pending_new", etc. — informational only

        # It's a fill (full or partial). Decide entry vs exit.
        parent_id_raw = (update.raw or {}).get("parent_id")
        is_child = bool(parent_id_raw) and str(parent_id_raw) not in {"0", ""}

        async with db_factory() as db:
            if is_child:
                await self._on_exit_fill(
                    db=db,
                    parent_order_id=str(parent_id_raw),
                    ticker=update.ticker,
                    filled_avg=update.filled_avg_price or Decimal("0"),
                )
                return

            # Otherwise treat as a parent / entry fill. Try to resolve the
            # draft via client_order_id (which we set to draft.id on submit).
            draft = None
            if update.client_order_id:
                draft = await self._find_draft_by_ref(db, update.client_order_id)
            await self._on_entry_fill(
                db=db,
                draft=draft,
                broker_order_id=update.broker_order_id,
                ticker=update.ticker,
                filled_qty=update.filled_qty,
                filled_avg=update.filled_avg_price or Decimal("0"),
            )

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
                "broker": settings.broker.value,
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
                "broker": settings.broker.value,
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
                    # broker_order_id (the stream fill won the race), don't try
                    # to insert a duplicate — just heal the draft status.
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
