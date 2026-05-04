"""Bot worker entry point — runs the Discord slash-command bot."""

from __future__ import annotations

import asyncio

from src.discord_bot.bot import run as run_bot
from src.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


async def main() -> None:
    configure_logging()
    logger.info("bot_worker_starting")
    await run_bot()


if __name__ == "__main__":
    asyncio.run(main())
