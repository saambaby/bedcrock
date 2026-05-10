"""Tests for the mini-backtester replay module.

These tests don't touch the real DB or OHLCV feed — they inject stub bar
providers and use lightweight signal stubs so the scorer / SQL paths are not
exercised. The replay() top-level paths that need a DB are tested with a
MockSession that records the SELECT signals query and returns canned rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from src.backtest.replay import (
    Bar,
    ReplayReport,
    SimTrade,
    _profit_factor,
    _score_signal,
    _sharpe,
    _simulate_trade,
    replay,
)
from src.db.models import Action, SignalSource


# ---------------- helpers ----------------


@dataclass
class StubSignal:
    """Minimal Signal-shaped stub for _simulate_trade."""

    ticker: str
    disclosed_at: datetime
    action: Action = Action.BUY
    source: SignalSource = SignalSource.QUIVER_CONGRESS
    source_external_id: str = "stub-1"
    trade_date: datetime | None = None
    size_low_usd: Decimal | None = None
    size_high_usd: Decimal | None = None
    gate_blocked: bool = False
    trader_id: Any = None


def _bars_from(start: date, ohlc_rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    out: list[Bar] = []
    for i, (o, h, l, c) in enumerate(ohlc_rows):
        out.append(Bar(date=start + timedelta(days=i), open=o, high=h, low=l, close=c, volume=1000.0))
    return out


def _make_provider(bars: list[Bar]):
    async def provider(_ticker: str, _end: date, _days: int) -> list[Bar]:
        return list(bars)

    return provider


# ---------------- _sharpe ----------------


def test_sharpe_zero_returns_zero():
    assert _sharpe([]) == 0.0
    assert _sharpe([1.0, 2.0]) == 0.0  # too few samples
    assert _sharpe([0.0] * 10) == 0.0  # zero stdev


def test_sharpe_positive_for_winning_strategy():
    # Mostly-positive returns should give a positive Sharpe.
    returns = [1.0, 2.0, 1.5, 3.0, -0.5, 2.0, 1.0, 2.5, 1.8, 0.5]
    s = _sharpe(returns)
    assert s > 0.0


def test_profit_factor_basic():
    trades = [
        SimTrade("A", date.today(), date.today(), Decimal("10"), date.today(),
                 Decimal("11"), 1, 10.0, "target"),
        SimTrade("B", date.today(), date.today(), Decimal("10"), date.today(),
                 Decimal("9"), 1, -10.0, "stop"),
        SimTrade("C", date.today(), date.today(), Decimal("10"), date.today(),
                 Decimal("12"), 1, 20.0, "target"),
    ]
    assert _profit_factor(trades) == pytest.approx(3.0)
    assert _profit_factor([]) == 0.0


# ---------------- _simulate_trade ----------------


@pytest.mark.asyncio
async def test_replay_simulates_trade_at_t_plus_1_open():
    """T+0 bar (signal date) is skipped; entry happens on T+1's open."""
    sig_date = date(2025, 1, 6)  # Monday
    sig = StubSignal(ticker="AAA", disclosed_at=datetime.combine(sig_date, datetime.min.time(), tzinfo=UTC))
    # Day0 = signal day; Day1 = entry day; rest are flat → timeout exit.
    bars = _bars_from(
        sig_date,
        [
            (100.0, 101.0, 99.0, 100.5),   # T+0
            (102.0, 103.0, 101.5, 102.5),  # T+1 — entry @ open=102
            (102.5, 103.0, 102.0, 102.8),
            (102.8, 103.2, 102.2, 103.0),
            (103.0, 103.5, 102.5, 103.2),
        ],
    )
    trade = await _simulate_trade(
        sig, stop_pct=0.10, r_mult=1.5, max_days=30, slip_bps=0,
        bar_provider=_make_provider(bars),
    )
    assert trade is not None
    assert trade.entry_date == sig_date + timedelta(days=1)
    assert float(trade.entry_price) == pytest.approx(102.0)
    assert trade.exit_reason == "timeout"


@pytest.mark.asyncio
async def test_replay_exits_on_stop():
    """Day 4 (3 bars after T+1) hits the 10% stop -> exit_reason=stop."""
    sig_date = date(2025, 1, 6)
    sig = StubSignal(ticker="BBB", disclosed_at=datetime.combine(sig_date, datetime.min.time(), tzinfo=UTC))
    # Entry on day 1 @ open=100. Stop = 90.
    bars = _bars_from(
        sig_date,
        [
            (100.0, 100.0, 100.0, 100.0),  # T+0
            (100.0, 101.0, 99.0, 100.0),   # T+1 entry @100
            (100.0, 101.0, 99.0, 100.0),
            (99.0, 100.0, 98.0, 99.0),
            (95.0, 96.0, 89.0, 90.0),      # day 4 — low 89 < stop 90
            (90.0, 91.0, 89.0, 90.0),
        ],
    )
    trade = await _simulate_trade(
        sig, stop_pct=0.10, r_mult=1.5, max_days=30, slip_bps=0,
        bar_provider=_make_provider(bars),
    )
    assert trade is not None
    assert trade.exit_reason == "stop"
    assert trade.pnl_pct == pytest.approx(-10.0)
    assert trade.exit_date == sig_date + timedelta(days=4)


@pytest.mark.asyncio
async def test_replay_exits_on_target():
    """Day 6 hits the 1.5R target above entry -> exit_reason=target."""
    sig_date = date(2025, 1, 6)
    sig = StubSignal(ticker="CCC", disclosed_at=datetime.combine(sig_date, datetime.min.time(), tzinfo=UTC))
    # Entry @100 on T+1; target = 100 * (1 + 0.10*1.5) = 115.
    bars = _bars_from(
        sig_date,
        [
            (100.0, 100.0, 100.0, 100.0),  # T+0
            (100.0, 101.0, 99.5, 100.0),   # T+1 entry @100
            (100.0, 102.0, 99.5, 101.0),
            (101.0, 103.0, 100.0, 102.0),
            (102.0, 105.0, 101.0, 104.0),
            (104.0, 110.0, 103.0, 109.0),
            (109.0, 116.0, 108.0, 115.5),  # day 6 — high 116 >= target 115
            (115.0, 116.0, 114.0, 115.0),
        ],
    )
    trade = await _simulate_trade(
        sig, stop_pct=0.10, r_mult=1.5, max_days=30, slip_bps=0,
        bar_provider=_make_provider(bars),
    )
    assert trade is not None
    assert trade.exit_reason == "target"
    assert trade.pnl_pct == pytest.approx(15.0)
    assert trade.exit_date == sig_date + timedelta(days=6)


# ---------------- replay() top-level recommendation gate ----------------


class MockResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class MockSession:
    """Returns the configured signals list for the first SELECT, then empty
    lists for the per-signal "prior signals" lookups inside _score_signal."""

    def __init__(self, signals):
        self._signals = signals
        self._call = 0

    async def execute(self, _stmt):
        self._call += 1
        if self._call == 1:
            return MockResult(self._signals)
        return MockResult([])


@pytest.mark.asyncio
async def test_replay_recommends_reject_when_oos_worse(monkeypatch):
    """Proposed weights produce losing OOS trades while baseline weights produce
    winning OOS trades — replay should recommend REJECT."""

    today = date.today()
    # 12 OOS-eligible signals (within last 30 days) — enough for _sharpe (>=5).
    signals = [
        StubSignal(
            ticker=f"T{i:02d}",
            disclosed_at=datetime.combine(today - timedelta(days=5 + i), datetime.min.time(), tzinfo=UTC),
            source_external_id=f"sig-{i}",
        )
        for i in range(12)
    ]

    # Force both scorers to produce above-threshold scores so trades simulate.
    from src.backtest import replay as replay_mod

    async def fake_score(scorer, sig, db):
        # Distinguish which scorer: proposed has a sentinel weight.
        is_proposed = scorer.weights.get("__sentinel__") == 1.0
        return 99.0  # always above threshold; bucket determined by which scorer

    monkeypatch.setattr(replay_mod, "_score_signal", fake_score)

    # Bar provider: every call returns a quick stop-loss path for proposed,
    # and a quick target-hit path for baseline. We can't distinguish callers
    # from inside the provider, so instead we use a single set of bars that
    # times out to a small flat return — and rely on a custom _simulate_trade
    # to return trades whose pnl depends on which scorer was used.
    call_state = {"n": 0}

    async def fake_simulate(sig, stop_pct, r_mult, max_days, slip_bps, bar_provider):
        # Alternate: proposed call first (loss), baseline call second (win)
        call_state["n"] += 1
        is_proposed_call = call_state["n"] % 2 == 1
        pnl = -10.0 if is_proposed_call else 5.0
        reason = "stop" if is_proposed_call else "target"
        return SimTrade(
            ticker=sig.ticker,
            signal_date=sig.disclosed_at.date(),
            entry_date=sig.disclosed_at.date() + timedelta(days=1),
            entry_price=Decimal("100"),
            exit_date=sig.disclosed_at.date() + timedelta(days=2),
            exit_price=Decimal("90") if is_proposed_call else Decimal("105"),
            qty=1,
            pnl_pct=pnl,
            exit_reason=reason,
        )

    monkeypatch.setattr(replay_mod, "_simulate_trade", fake_simulate)

    db = MockSession(signals)
    report = await replay(
        db,  # type: ignore[arg-type]
        proposed_weights={"__sentinel__": 1.0},
        bar_provider=_make_provider([]),  # unused; _simulate_trade is mocked
    )

    assert isinstance(report, ReplayReport)
    assert report.recommendation == "REJECT"
    assert report.sharpe_delta_vs_baseline < 0
