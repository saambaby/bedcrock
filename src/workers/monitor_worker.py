"""Monitor worker entry point — runs LiveMonitor.run_forever().

Also runs the intraday `update_daily_pnl` loop every 60s so the
`daily_kill_switch` gate has live P&L to read.
"""

from __future__ import annotations

import asyncio

from src.broker import make_broker
from src.db.session import SessionLocal, dispose
from src.logging_config import configure_logging, get_logger
from src.orders.monitor import LiveMonitor
from src.workers.daily_pnl import (
    DAILY_PNL_INTERVAL_SECONDS,
    update_daily_pnl,
)

logger = get_logger(__name__)


async def _daily_pnl_loop(stopped: asyncio.Event) -> None:
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
    pnl_task = asyncio.create_task(_daily_pnl_loop(stopped))
    try:
        await monitor.run_forever()
    finally:
        stopped.set()
        pnl_task.cancel()
        try:
            await pnl_task
        except (asyncio.CancelledError, Exception):
            pass
        await monitor.stop()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
