"""Monitor worker entry point — runs LiveMonitor.run_forever()."""

from __future__ import annotations

import asyncio

from src.broker.ibkr import IBKRBroker
from src.db.session import dispose
from src.logging_config import configure_logging, get_logger
from src.orders.monitor import LiveMonitor
from src.safety.reconciler import audit_open_order_tifs

logger = get_logger(__name__)

TIF_AUDIT_INTERVAL_SEC = 30


async def _tif_audit_loop(monitor: LiveMonitor) -> None:
    """Periodically re-issue any bracket child whose TIF drifted off GTC.

    Runs alongside ``LiveMonitor._poll`` (the order-reconcile loop) on the
    same 30s cadence. See plan v2 §V2.2.
    """
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


async def main() -> None:
    configure_logging()
    logger.info("monitor_worker_starting")
    monitor = LiveMonitor()
    audit_task = asyncio.create_task(_tif_audit_loop(monitor))
    try:
        await monitor.run_forever()
    finally:
        audit_task.cancel()
        try:
            await audit_task
        except (asyncio.CancelledError, Exception):
            pass
        await monitor.stop()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
