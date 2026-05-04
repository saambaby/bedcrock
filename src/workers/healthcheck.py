"""CLI healthcheck.

Exits 0 if:
  - DB reachable
  - Broker reachable (paper or live, per MODE)
  - Every registered ingestor has a heartbeat within `interval * 2`
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from src.broker import get_broker
from src.config import settings
from src.db.models import IngestorHeartbeat
from src.db.session import SessionLocal, dispose
from src.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

EXPECTED_INGESTORS = {
    "sec_form4": 30,
    "quiver_congress": 60,
    "uw_flow": 10,
    "uw_congress": 60,
    "finnhub_earnings": 26 * 60,  # daily ± slack
}


async def main() -> int:
    configure_logging()
    failures: list[str] = []

    # DB
    try:
        async with SessionLocal() as db:
            rows = (await db.execute(select(IngestorHeartbeat))).scalars().all()
            heartbeats = {r.ingestor: r for r in rows}
    except Exception as e:
        print(f"DB: FAIL — {e}", file=sys.stderr)
        await dispose()
        return 2
    print("DB: ok")

    # Broker
    try:
        broker = get_broker()
        await broker.connect()
        ok = await broker.healthcheck()
        await broker.disconnect()
        print(f"Broker ({settings.mode.value}): {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append("broker")
    except Exception as e:
        print(f"Broker: FAIL — {e}")
        failures.append("broker")

    # Ingestor heartbeats
    now = datetime.now(UTC)
    for name, expected_min in EXPECTED_INGESTORS.items():
        hb = heartbeats.get(name)
        if not hb:
            print(f"Ingestor {name}: NO HEARTBEAT")
            failures.append(name)
            continue
        age = (now - hb.last_run_at).total_seconds() / 60
        threshold = expected_min * 2
        ok = age <= threshold
        print(
            f"Ingestor {name}: {'ok' if ok else 'STALE'}  "
            f"age={age:.1f}min  threshold={threshold}min  "
            f"last_signals={hb.signals_in_last_run}  "
            f"last_error={hb.last_error or '—'}"
        )
        if not ok:
            failures.append(name)

    await dispose()

    if failures:
        print(f"\nFailures: {failures}", file=sys.stderr)
        return 1
    print("\nAll healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
