"""Monitor worker entry point — runs LiveMonitor.run_forever()."""

from __future__ import annotations

import asyncio

from src.db.session import dispose
from src.logging_config import configure_logging, get_logger
from src.orders.monitor import LiveMonitor

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    logger.info("monitor_worker_starting")
    monitor = LiveMonitor()
    try:
        await monitor.run_forever()
    finally:
        await monitor.stop()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
