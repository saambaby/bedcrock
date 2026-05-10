"""Tests for `monitor_worker.update_daily_pnl`.

The function is fully mocked at the DB and broker boundary — no Postgres or
IBKR needed. We assert that the upserted DailyState row carries the correct
`daily_pnl_pct` and `equity_at_open` for the supplied broker equity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.broker.base import AccountSnapshot
from src.db.models import EquitySnapshot, Mode
from src.workers import daily_pnl as monitor_worker


def _equity_snapshot(equity: Decimal) -> EquitySnapshot:
    today = date.today()
    return EquitySnapshot(
        mode=Mode.PAPER,
        snapshot_date=datetime.combine(today, datetime.min.time(), tzinfo=UTC),
        equity=equity,
        cash=Decimal("0"),
        positions_value=Decimal("0"),
        daily_pnl=Decimal("0"),
        daily_pnl_pct=Decimal("0"),
    )


def _mock_db(sod: EquitySnapshot | None) -> MagicMock:
    """Return an AsyncSession-like mock.

    First `execute()` call (the SOD lookup) returns `sod`; subsequent calls
    (the upsert + the readback) return a result whose `.scalar_one()` gives a
    DailyState built from the values seen by the upsert.
    """
    db = MagicMock()
    captured: dict = {}

    def make_result_with_sod():
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=sod)
        return r

    def make_result_for_upsert():
        # The upsert path; capture the values for later assertion.
        r = MagicMock()
        return r

    def make_result_for_readback():
        from src.db.models import DailyState
        ds = DailyState(
            date=date.today(),
            mode=Mode.PAPER,
            daily_pnl_pct=captured.get("daily_pnl_pct", Decimal("0")),
            equity_at_open=captured.get("equity_at_open"),
        )
        r = MagicMock()
        r.scalar_one = MagicMock(return_value=ds)
        return r

    call_count = {"n": 0}

    async def execute(stmt):
        call_count["n"] += 1
        # Capture upsert values from the second call (the pg_insert).
        if call_count["n"] == 2:
            try:
                values = stmt.compile().params
                captured["daily_pnl_pct"] = values.get("daily_pnl_pct")
                captured["equity_at_open"] = values.get("equity_at_open")
            except Exception:
                pass
            return make_result_for_upsert()
        if call_count["n"] == 1:
            return make_result_with_sod()
        return make_result_for_readback()

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_update_daily_pnl_computes_correctly():
    """SOD equity 100k, current 98k — daily_pnl_pct should be -2.0."""
    sod = _equity_snapshot(Decimal("100000"))
    db = _mock_db(sod)

    broker = MagicMock()
    broker.get_account = AsyncMock(
        return_value=AccountSnapshot(
            equity=Decimal("98000"),
            cash=Decimal("0"),
            positions_value=Decimal("98000"),
            buying_power=Decimal("0"),
            pattern_day_trader=False,
        )
    )

    result = await monitor_worker.update_daily_pnl(db, broker)

    assert result is not None
    assert result.daily_pnl_pct == Decimal("-2.0000")
    assert result.equity_at_open == Decimal("100000")
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_daily_pnl_returns_none_without_sod():
    """No start-of-day snapshot → no-op, returns None."""
    db = _mock_db(None)
    broker = MagicMock()
    broker.get_account = AsyncMock()

    result = await monitor_worker.update_daily_pnl(db, broker)
    assert result is None
    broker.get_account.assert_not_awaited()
