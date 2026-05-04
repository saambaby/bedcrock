"""API worker entry point — runs uvicorn for the FastAPI confirm/skip + health app.

`python -m src.workers.api_worker` listens on settings.api_host:settings.api_port.

In production behind nginx, terminate TLS at nginx and reverse-proxy to this.
For local dev, just hit http://127.0.0.1:8080/healthz directly.
"""

from __future__ import annotations

import uvicorn

from src.config import settings
from src.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def main() -> None:
    configure_logging()
    logger.info("api_worker_starting", host=settings.api_host, port=settings.api_port)
    uvicorn.run(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # let structlog handle it
        access_log=False,
    )


if __name__ == "__main__":
    main()
