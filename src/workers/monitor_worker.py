"""Monitor worker entry point — runs LiveMonitor.run_forever().

Side tasks:
- ``_tif_audit_loop`` (B1) — every 30s, re-issues any bracket child
  whose TIF drifted off GTC. See plan v2 §V2.2.
- ``_daily_pnl_loop`` (B3) — every 60s, refreshes ``DailyState`` so the
  ``daily_kill_switch`` gate has live P&L to read. See plan v2 §V2.5.
"""

from __future__ import annotations

import asyncio

from src.broker import make_broker
from src.broker.ibkr import IBKRBroker
from src.db.session import SessionLocal, dispose
from src.logging_config import configure_logging, get_logger
from src.orders.monitor import LiveMonitor
from src.safety.reconciler import audit_open_order_tifs
from src.workers.daily_pnl import (
    DAILY_PNL_INTERVAL_SECONDS,
    update_daily_pnl,
)

logger = get_logger(__name__)

TIF_AUDIT_INTERVAL_SEC = 30


async def _tif_audit_loop(monitor: LiveMonitor) -> None:
    """Periodically re-issue any bracket child whose TIF drifted off GTC."""
    while not monitor._stopped:
        try:
            broker = monitor._broker
            if isinstance(broker, IBKRBroker) and broker._ib.isConnected():
                repaired = await audit_open_order_tifs(broker)
                if repaired:
                    logger.warning("tif_audit_repaired_orders", count=len(repaired))
        except Exception as e:
            logger.warning("tif_audit_loop_failed", error=str(e))
        await asyncio.sleep(TIF_AUDIT_INTERVAL_SEC)


async def _daily_pnl_loop(stopped: asyncio.Event) -> None:
    """Refresh DailyState for the daily kill switch gate."""
    broker = make_broker()
    try:
        await broker.connect()
    except Exception as e:
        logger.warning("daily_pnl_broker_connect_failed", error=str(e))
        return

    try:
        while not stopped.is_set():
            try:
                async with SessionLocal() as db:
                    await update_daily_pnl(db, broker)
            except Exception as e:
                logger.warning("daily_pnl_update_failed", error=str(e))
            try:
                await asyncio.wait_for(
                    stopped.wait(), timeout=DAILY_PNL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                continue
    finally:
        try:
            await broker.disconnect()
        except Exception:
            pass


async def main() -> None:
    configure_logging()
    logger.info("monitor_worker_starting")
    monitor = LiveMonitor()
    stopped = asyncio.Event()
    audit_task = asyncio.create_task(_tif_audit_loop(monitor))
    pnl_task = asyncio.create_task(_daily_pnl_loop(stopped))
    try:
        await monitor.run_forever()
    finally:
        stopped.set()
        audit_task.cancel()
        pnl_task.cancel()
        for task in (audit_task, pnl_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await monitor.stop()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
