"""Mini-backtester: replay historical signals under a proposed scoring rule set.

Inputs:
  - db: AsyncSession into the Signal cache.
  - proposed_weights: dict overriding current Scorer weights.
  - sim_config kwargs (entry rule = T+1 OPEN, stop loss %, target R-multiple,
    max holding days, slippage bps, out-of-sample window in days).

Outputs:
  - ReplayReport with in-sample / out-of-sample Sharpe, win rate, profit factor,
    Sharpe delta vs baseline, and an ADOPT / REJECT / INCONCLUSIVE recommendation.

Hard limits to prevent overfitting:
  - REQUIRES an out-of-sample window (default last 30 days) reserved.
  - Reports both in-sample and out-of-sample metrics.
  - Refuses to recommend ADOPT if out-of-sample Sharpe ≤ baseline.

LIMITATIONS / CAVEATS (advisory only):
  - Uses historical OHLCV bars only — no bid/ask depth, no level-2.
  - Slippage is a CONSTANT in basis points; real-world slippage varies with
    liquidity, regime, and order size.
  - No survivorship-bias correction. yfinance / Polygon adjusted history
    silently drops delisted tickers.
  - No corporate-action handling beyond what the price feed already adjusts.
  - Sizing is fixed (qty=1); position-sizing rules from src/orders are NOT
    re-applied in the simulation.
  - Re-scoring uses the CURRENT prior-signals set in the DB, not a
    point-in-time reconstruction. Bias is small for short windows, large for
    long ones.
  - This is a sanity check, not a Monte Carlo. Treat the recommendation as
    advisory; the human still confirms every rule change.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Iterable, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Signal
from src.scoring.scorer import Scorer


class Bar(NamedTuple):
    """Minimal OHLCV bar used by the replay simulator."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


# A bar provider takes (ticker, end_date, days) and returns an ascending list
# of Bar objects ending at or before end_date. Default impl wraps the live
# OHLCV fetcher; tests inject a deterministic stub.
BarProvider = Callable[[str, date, int], Awaitable[list[Bar]]]


@dataclass
class SimTrade:
    ticker: str
    signal_date: date
    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    qty: int
    pnl_pct: float
    exit_reason: str  # stop | target | timeout


@dataclass
class ReplayReport:
    n_signals_in_scope: int
    n_signals_above_threshold: int
    n_trades_simulated: int
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    win_rate: float
    profit_factor: float
    total_return_pct: float
    sharpe_delta_vs_baseline: float
    recommendation: str  # ADOPT | REJECT | INCONCLUSIVE


# ---------------- public entry point ----------------


async def replay(
    db: AsyncSession,
    proposed_weights: dict,
    *,
    score_threshold: float = 7.0,
    stop_loss_pct: float = 0.10,
    target_r_multiple: float = 1.5,
    holding_days_max: int = 30,
    slippage_bps: float = 10,
    out_of_sample_days: int = 30,
    lookback_days: int = 180,
    bar_provider: BarProvider | None = None,
) -> ReplayReport:
    """See module docstring."""
    today = date.today()
    cutoff = today - timedelta(days=lookback_days)
    out_sample_start = today - timedelta(days=out_of_sample_days)

    cutoff_dt = datetime.combine(cutoff, datetime.min.time())

    stmt = select(Signal).where(
        Signal.disclosed_at >= cutoff_dt,
        Signal.gate_blocked.is_(False),
    )
    signals = list((await db.execute(stmt)).scalars().all())

    proposed_scorer = Scorer(weights=proposed_weights)
    baseline_scorer = Scorer()

    if bar_provider is None:
        bar_provider = _default_bar_provider

    in_sample_trades: list[SimTrade] = []
    out_sample_trades: list[SimTrade] = []
    baseline_in_sample: list[SimTrade] = []
    baseline_out_sample: list[SimTrade] = []
    n_above_threshold = 0

    for sig in signals:
        proposed_score = await _score_signal(proposed_scorer, sig, db)
        baseline_score = await _score_signal(baseline_scorer, sig, db)

        sig_date = _to_date(sig.disclosed_at)
        is_oos = sig_date >= out_sample_start

        if proposed_score >= score_threshold:
            n_above_threshold += 1
            trade = await _simulate_trade(
                sig, stop_loss_pct, target_r_multiple,
                holding_days_max, slippage_bps, bar_provider,
            )
            if trade is not None:
                (out_sample_trades if is_oos else in_sample_trades).append(trade)

        if baseline_score >= score_threshold:
            trade = await _simulate_trade(
                sig, stop_loss_pct, target_r_multiple,
                holding_days_max, slippage_bps, bar_provider,
            )
            if trade is not None:
                (baseline_out_sample if is_oos else baseline_in_sample).append(trade)

    in_sharpe = _sharpe([t.pnl_pct for t in in_sample_trades])
    oos_sharpe = _sharpe([t.pnl_pct for t in out_sample_trades])
    baseline_oos_sharpe = _sharpe([t.pnl_pct for t in baseline_out_sample])

    delta = oos_sharpe - baseline_oos_sharpe

    # Tiebreak on mean PnL when sharpes are equal (e.g., constant returns
    # produce stdev=0 → sharpe=0 for both arms).
    proposed_mean = (
        statistics.mean([t.pnl_pct for t in out_sample_trades])
        if out_sample_trades else 0.0
    )
    baseline_mean = (
        statistics.mean([t.pnl_pct for t in baseline_out_sample])
        if baseline_out_sample else 0.0
    )
    mean_delta = proposed_mean - baseline_mean

    if oos_sharpe > baseline_oos_sharpe and oos_sharpe > 1.0:
        rec = "ADOPT"
    elif oos_sharpe < baseline_oos_sharpe or (
        oos_sharpe == baseline_oos_sharpe and mean_delta < 0
    ):
        rec = "REJECT"
    else:
        rec = "INCONCLUSIVE"

    n_oos = len(out_sample_trades)
    win_rate = (
        sum(1 for t in out_sample_trades if t.pnl_pct > 0) / n_oos if n_oos else 0.0
    )

    return ReplayReport(
        n_signals_in_scope=len(signals),
        n_signals_above_threshold=n_above_threshold,
        n_trades_simulated=len(in_sample_trades) + len(out_sample_trades),
        in_sample_sharpe=in_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        win_rate=win_rate,
        profit_factor=_profit_factor(out_sample_trades),
        total_return_pct=sum(t.pnl_pct for t in out_sample_trades),
        sharpe_delta_vs_baseline=delta,
        recommendation=rec,
    )


# ---------------- helpers ----------------


def _sharpe(returns: Iterable[float]) -> float:
    """Annualised Sharpe (assumes ~50 trades/yr cadence). Returns 0 for tiny samples."""
    rs = list(returns)
    if len(rs) < 5:
        return 0.0
    mean = statistics.mean(rs)
    sd = statistics.stdev(rs)
    if sd == 0:
        return 0.0
    return (mean / sd) * (50 ** 0.5)


def _profit_factor(trades: list[SimTrade]) -> float:
    wins = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
    losses = abs(sum(t.pnl_pct for t in trades if t.pnl_pct < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


async def _score_signal(scorer: Scorer, sig: Signal, db: AsyncSession) -> float:
    """Re-score a stored Signal under the given Scorer.

    For the mini-backtester we approximate context: prior signals on the same
    ticker in the 30 days before disclosure, no live indicators, no track
    record. This intentionally underweights features that need live context;
    weight changes that survive *despite* that handicap are real.
    """
    from src.schemas import RawSignal  # local import to avoid cycle at import time

    window_start = sig.disclosed_at - timedelta(days=30)
    prior_stmt = select(Signal).where(
        Signal.ticker == sig.ticker,
        Signal.disclosed_at >= window_start,
        Signal.disclosed_at < sig.disclosed_at,
    )
    prior = list((await db.execute(prior_stmt)).scalars().all())

    raw = RawSignal(
        source=sig.source,
        source_external_id=sig.source_external_id,
        ticker=sig.ticker,
        action=sig.action,
        disclosed_at=sig.disclosed_at,
        trade_date=sig.trade_date,
        size_low_usd=sig.size_low_usd,
        size_high_usd=sig.size_high_usd,
    )
    total, _ = scorer.score(raw, prior, indicators=None, trader_track_record=None)
    return total


async def _simulate_trade(
    sig: Signal,
    stop_pct: float,
    r_mult: float,
    max_days: int,
    slip_bps: float,
    bar_provider: BarProvider,
) -> SimTrade | None:
    """T+1 OPEN entry, OCO stop & target, exit at first hit or timeout.

    Stop checked before target on intraday hits (conservative)."""
    sig_date = _to_date(sig.disclosed_at)
    end = sig_date + timedelta(days=max_days + 5)
    bars = await bar_provider(sig.ticker, end, max_days + 10)
    if not bars or len(bars) < 2:
        return None

    t_plus_1 = next((b for b in bars if b.date > sig_date), None)
    if t_plus_1 is None:
        return None

    entry = t_plus_1.open * (1 + slip_bps / 10000)
    stop = entry * (1 - stop_pct)
    target = entry * (1 + stop_pct * r_mult)

    start_idx = bars.index(t_plus_1)
    sliced = bars[start_idx : start_idx + max_days]

    for b in sliced:
        if b.low <= stop:
            return SimTrade(
                ticker=sig.ticker,
                signal_date=sig_date,
                entry_date=t_plus_1.date,
                entry_price=Decimal(str(entry)),
                exit_date=b.date,
                exit_price=Decimal(str(stop)),
                qty=1,
                pnl_pct=-stop_pct * 100,
                exit_reason="stop",
            )
        if b.high >= target:
            return SimTrade(
                ticker=sig.ticker,
                signal_date=sig_date,
                entry_date=t_plus_1.date,
                entry_price=Decimal(str(entry)),
                exit_date=b.date,
                exit_price=Decimal(str(target)),
                qty=1,
                pnl_pct=stop_pct * r_mult * 100,
                exit_reason="target",
            )

    last = sliced[-1]
    return SimTrade(
        ticker=sig.ticker,
        signal_date=sig_date,
        entry_date=t_plus_1.date,
        entry_price=Decimal(str(entry)),
        exit_date=last.date,
        exit_price=Decimal(str(last.close)),
        qty=1,
        pnl_pct=float((last.close / entry - 1) * 100),
        exit_reason="timeout",
    )


def _to_date(d) -> date:
    return d.date() if isinstance(d, datetime) else d


async def _default_bar_provider(ticker: str, end_date: date, days: int) -> list[Bar]:
    """Default bar provider — wraps src.ingestors.ohlcv.OHLCVFetcher.

    Imported lazily so tests / non-network callers can avoid the dependency.
    """
    from src.ingestors.ohlcv import OHLCVFetcher

    fetcher = OHLCVFetcher()
    try:
        df = await fetcher.fetch_daily(ticker, lookback_days=days)
    finally:
        await fetcher.aclose()

    if df is None or df.empty:
        return []

    bars: list[Bar] = []
    for d, row in df.iterrows():
        if d > end_date:
            continue
        bars.append(
            Bar(
                date=d,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars
