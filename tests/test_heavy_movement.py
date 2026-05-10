"""Tests for the heavy-movement ingestor.

These tests mock out the DB session and the OHLCV fetcher to isolate the
trigger logic (volume spike, gap, 52-week breakout, gap-down kill) from
upstream data and persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

from src.db.models import Action, SignalSource
from src.ingestors.heavy_movement import GAP_DOWN_KILL, HeavyMovementIngestor


# ---- Test helpers ----------------------------------------------------------


def _mid_market_now() -> datetime:
    """A weekday at 11:00 ET in UTC — well inside market hours."""
    # 2025-05-12 is a Monday. 11:00 ET = 15:00 UTC (EDT).
    return datetime(2025, 5, 12, 15, 0, tzinfo=UTC)


def _make_bars(n: int, *, base_close: float = 100.0, base_vol: float = 1_000_000) -> pd.DataFrame:
    """Build n consecutive boring daily bars ending today."""
    end = datetime(2025, 5, 12).date()
    dates = [end - timedelta(days=(n - 1 - i)) for i in range(n)]
    rows = []
    for _ in range(n):
        rows.append({
            "open": base_close,
            "high": base_close,
            "low": base_close,
            "close": base_close,
            "volume": base_vol,
        })
    df = pd.DataFrame(rows, index=dates)
    return df


def _build_ingestor(df: pd.DataFrame, *, watchlist: set[str] | None = None):
    """Create an ingestor whose OHLCV returns the given DataFrame for any ticker."""
    ohlcv = MagicMock()
    ohlcv.fetch_daily = AsyncMock(return_value=df)

    ing = HeavyMovementIngestor(ohlcv=ohlcv, now_fn=_mid_market_now)
    # Stub watchlist
    ing._build_watchlist = AsyncMock(return_value=set(watchlist or {"NVDA"}))
    # Stub heartbeat (no DB)
    ing._heartbeat = AsyncMock()
    return ing


class _FakeStmt:
    """Stand-in for a pg_insert statement that records the values passed."""

    def __init__(self, values: dict, captured: list[dict]):
        self._values = values
        self._captured = captured

    def on_conflict_do_nothing(self, *args, **kwargs):
        # Only record values when the pipeline is fully built (mirrors
        # the real pg_insert(...).values(...).on_conflict_do_nothing(...)).
        self._captured.append(self._values)
        return self


class _FakeInsertChain:
    def __init__(self, captured: list[dict]):
        self._captured = captured

    def values(self, **kw) -> _FakeStmt:
        return _FakeStmt(kw, self._captured)


def _make_db_and_capture():
    """An AsyncSession mock + a captured-inserts list.

    Returns (db, captured) where `captured` is the list of values dicts
    passed to pg_insert(Signal).values(**kw).
    """
    captured: list[dict] = []
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        result.rowcount = 1
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db, captured


def _patched_pg_insert(captured: list[dict]):
    """A patcher that intercepts pg_insert(Signal).values(...).on_conflict_do_nothing(...)
    inside src.ingestors.heavy_movement and records the values."""
    return patch(
        "src.ingestors.heavy_movement.pg_insert",
        lambda _model: _FakeInsertChain(captured),
    )


# ---- Tests ----------------------------------------------------------------


async def test_heavy_movement_emits_on_volume_spike():
    """Bar 22 with 4x average volume triggers a MARKET_MOVEMENT signal."""
    df = _make_bars(22)
    # 4x volume on the last bar — well above the 3x threshold
    df.iloc[-1, df.columns.get_loc("volume")] = 4_000_000

    ing = _build_ingestor(df)
    db, captured = _make_db_and_capture()

    with _patched_pg_insert(captured):
        n = await ing.run(db)

    assert n == 1
    assert len(captured) == 1
    params = captured[0]
    assert params["source"] == SignalSource.MARKET_MOVEMENT
    assert params["ticker"] == "NVDA"
    # Volume spike with no gap → action defaults to BUY (gap_pct == 0 → >= 0)
    assert params["action"] == Action.BUY
    raw = params["raw"]
    assert raw["volume_ratio"] >= 3.0
    assert any(t.startswith("vol") for t in raw["triggers"])


async def test_heavy_movement_skips_below_threshold():
    """All-normal bars produce zero signals."""
    df = _make_bars(22)  # everything constant — no triggers
    ing = _build_ingestor(df)
    db, captured = _make_db_and_capture()

    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 0
    assert captured == []


async def test_heavy_movement_kills_major_gap_down():
    """A gap of -15% on the latest bar must NOT emit a signal."""
    df = _make_bars(22)
    # Make today's open 15% below yesterday's close
    df.iloc[-1, df.columns.get_loc("open")] = 85.0
    df.iloc[-1, df.columns.get_loc("low")] = 85.0
    df.iloc[-1, df.columns.get_loc("high")] = 85.0
    df.iloc[-1, df.columns.get_loc("close")] = 85.0
    # Also spike volume so it would otherwise trigger
    df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000

    # Sanity: the gap exceeds the kill threshold
    gap = (85.0 / 100.0) - 1.0
    assert gap <= GAP_DOWN_KILL

    ing = _build_ingestor(df)
    db, captured = _make_db_and_capture()
    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 0
    assert captured == []


async def test_heavy_movement_only_for_watchlist():
    """An empty watchlist short-circuits — no scans, no signals."""
    df = _make_bars(22)
    df.iloc[-1, df.columns.get_loc("volume")] = 10_000_000  # would trigger

    ing = _build_ingestor(df, watchlist=set())
    db, captured = _make_db_and_capture()

    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 0
    # The OHLCV fetcher should not have been called at all
    ing._ohlcv.fetch_daily.assert_not_called()
    assert captured == []


async def test_heavy_movement_outside_market_hours_skipped():
    """Outside RTH the ingestor is a no-op even if the watchlist is non-empty."""
    df = _make_bars(22)
    df.iloc[-1, df.columns.get_loc("volume")] = 10_000_000

    # Saturday 11:00 ET
    weekend = datetime(2025, 5, 17, 15, 0, tzinfo=UTC)
    ohlcv = MagicMock()
    ohlcv.fetch_daily = AsyncMock(return_value=df)
    ing = HeavyMovementIngestor(ohlcv=ohlcv, now_fn=lambda: weekend)
    ing._build_watchlist = AsyncMock(return_value={"NVDA"})
    ing._heartbeat = AsyncMock()

    db, captured = _make_db_and_capture()
    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 0
    ohlcv.fetch_daily.assert_not_called()


async def test_heavy_movement_emits_on_positive_gap():
    """+6% gap with normal volume still triggers (gap-only path), action=BUY."""
    df = _make_bars(22)
    df.iloc[-1, df.columns.get_loc("open")] = 106.0
    df.iloc[-1, df.columns.get_loc("high")] = 107.0
    df.iloc[-1, df.columns.get_loc("low")] = 105.5
    df.iloc[-1, df.columns.get_loc("close")] = 106.5

    ing = _build_ingestor(df)
    db, captured = _make_db_and_capture()
    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 1
    params = captured[0]
    assert params["action"] == Action.BUY
    assert any(t.startswith("gap") for t in params["raw"]["triggers"])


async def test_heavy_movement_emits_on_negative_gap_above_kill():
    """-7% gap (above -10% kill threshold) triggers with action=SELL."""
    df = _make_bars(22)
    df.iloc[-1, df.columns.get_loc("open")] = 93.0
    df.iloc[-1, df.columns.get_loc("high")] = 94.0
    df.iloc[-1, df.columns.get_loc("low")] = 92.0
    df.iloc[-1, df.columns.get_loc("close")] = 92.5

    ing = _build_ingestor(df)
    db, captured = _make_db_and_capture()
    with _patched_pg_insert(captured):
        n = await ing.run(db)
    assert n == 1
    assert captured[0]["action"] == Action.SELL
