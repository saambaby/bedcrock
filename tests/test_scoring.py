"""Smoke tests for scoring + order builder validation logic.

These don't hit the DB or the broker. They verify the pure-logic pieces.

Run: pytest tests/
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.db.models import Action, SignalSource
from src.schemas import IndicatorSnapshot, RawSignal, ScoreBreakdown
from src.scoring.scorer import Scorer


@pytest.fixture
def base_signal():
    return RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="test-1",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
        size_low_usd=Decimal("60000"),
        size_high_usd=Decimal("100000"),
        trader_slug="pelosi",
        trader_display_name="Nancy Pelosi",
        trader_kind="politician",
    )


@pytest.fixture
def uptrend_indicators():
    return IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        price=Decimal("500"),
        sma_50=Decimal("480"),
        sma_200=Decimal("420"),
        atr_20=Decimal("12"),
        rsi_14=Decimal("60"),
        adv_30d_usd=Decimal("10000000000"),
        rs_vs_spy_60d=Decimal("1.15"),
        rs_vs_sector_60d=Decimal("1.05"),
        swing_high_90d=Decimal("520"),
        swing_low_90d=Decimal("400"),
        sector_etf="SMH",
        trend="uptrend",
    )


def test_scorer_pure_signal_no_context(base_signal):
    """No prior signals, no indicators — score is just size + cluster floor (0)."""
    s = Scorer()
    total, breakdown = s.score(base_signal, prior_signals_30d=[], indicators=None)
    # large size triggers size component (>= 50k)
    assert breakdown.size > 0
    assert breakdown.cluster == 0
    assert breakdown.trend_alignment == 0
    assert total == breakdown.total
    assert total > 0


def test_scorer_trend_alignment_buy_in_uptrend(base_signal, uptrend_indicators):
    s = Scorer()
    _, breakdown = s.score(base_signal, prior_signals_30d=[], indicators=uptrend_indicators)
    assert breakdown.trend_alignment > 0  # buy in uptrend = bonus


def test_scorer_trend_alignment_buy_in_downtrend(base_signal):
    downtrend = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        price=Decimal("100"),
        sma_50=Decimal("110"),
        sma_200=Decimal("130"),
        atr_20=Decimal("3"),
        adv_30d_usd=Decimal("100000000"),
        sector_etf="SMH",
        trend="downtrend",
    )
    s = Scorer()
    _, breakdown = s.score(base_signal, prior_signals_30d=[], indicators=downtrend)
    assert breakdown.trend_alignment < 0  # buy in downtrend = penalty


def test_scorer_relative_strength_strong(base_signal, uptrend_indicators):
    s = Scorer()
    _, breakdown = s.score(base_signal, prior_signals_30d=[], indicators=uptrend_indicators)
    # rs_vs_sector = 1.05 > 1.0
    assert breakdown.relative_strength > 0


def test_score_breakdown_total_is_sum():
    b = ScoreBreakdown(
        cluster=2.0,
        insider_corroboration=2.0,
        trend_alignment=1.0,
    )
    assert b.total == 5.0


def test_score_breakdown_to_dict():
    b = ScoreBreakdown(cluster=1.5, trend_alignment=1.0)
    d = b.to_dict()
    assert d["cluster"] == 1.5
    assert d["trend_alignment"] == 1.0
    assert d["insider_corroboration"] == 0.0


def test_indicator_snapshot_stop_floor_long():
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        atr_20=Decimal("10"),
    )
    floor = ind.stop_floor(entry=Decimal("500"))
    # 1.5 * 10 = 15 below entry
    assert floor == Decimal("485")


def test_indicator_snapshot_stop_floor_no_atr():
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
    )
    assert ind.stop_floor(entry=Decimal("500")) is None


# ---------------------------------------------------------------------------
# Cluster scoring
# ---------------------------------------------------------------------------

from datetime import timedelta
from unittest.mock import MagicMock


def make_signal(source, action, ticker="NVDA", trader_id=None, disclosed_at=None):
    s = MagicMock()
    s.source = source
    s.action = action
    s.ticker = ticker
    s.trader_id = trader_id
    s.disclosed_at = disclosed_at or datetime.now(UTC)
    return s


def test_cluster_no_prior_signals(base_signal):
    """Cluster score is 0 with no prior signals."""
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, []) == 0.0


def test_cluster_one_extra_source(base_signal):
    """One different source on same ticker+direction gives +1."""
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA")]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 1.0


def test_cluster_two_extra_sources(base_signal):
    """Two different sources give +2."""
    prior = [
        make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA"),
        make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA"),
    ]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 2.0


def test_cluster_capped_at_max(base_signal):
    """Cluster score never exceeds cluster_max (default 3.0)."""
    prior = [
        make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA"),
        make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA"),
        make_signal(SignalSource.SEC_13D, Action.BUY, "NVDA"),
        make_signal(SignalSource.SEC_13F, Action.BUY, "NVDA"),
        make_signal(SignalSource.UW_DARKPOOL, Action.BUY, "NVDA"),
    ]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 3.0


def test_cluster_ignores_different_direction(base_signal):
    """SELL signals on same ticker don't count for BUY cluster."""
    prior = [make_signal(SignalSource.SEC_FORM4, Action.SELL, "NVDA")]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 0.0


def test_cluster_ignores_different_ticker(base_signal):
    """Signals on different tickers don't count."""
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "AAPL")]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 0.0


def test_cluster_same_source_not_counted(base_signal):
    """Prior signal from same source as the incoming signal is excluded."""
    prior = [make_signal(SignalSource.QUIVER_CONGRESS, Action.BUY, "NVDA")]
    scorer = Scorer()
    assert scorer._score_cluster(base_signal, prior) == 0.0


# ---------------------------------------------------------------------------
# Insider corroboration
# ---------------------------------------------------------------------------


def test_insider_corroboration_buy_with_form4(base_signal):
    """BUY signal with a Form 4 buy in prior returns the weight."""
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA")]
    scorer = Scorer()
    assert scorer._score_insider_corroboration(base_signal, prior) == 2.0


def test_insider_corroboration_sell_returns_zero():
    """SELL signal never gets insider corroboration."""
    sell_signal = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="test-sell",
        ticker="NVDA",
        action=Action.SELL,
        disclosed_at=datetime.now(UTC),
    )
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA")]
    scorer = Scorer()
    assert scorer._score_insider_corroboration(sell_signal, prior) == 0.0


def test_insider_corroboration_no_form4(base_signal):
    """BUY signal without any Form 4 buy returns 0."""
    prior = [make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA")]
    scorer = Scorer()
    assert scorer._score_insider_corroboration(base_signal, prior) == 0.0


def test_insider_corroboration_form4_sell_ignored(base_signal):
    """Form 4 SELL does not corroborate a BUY signal."""
    prior = [make_signal(SignalSource.SEC_FORM4, Action.SELL, "NVDA")]
    scorer = Scorer()
    assert scorer._score_insider_corroboration(base_signal, prior) == 0.0


# ---------------------------------------------------------------------------
# Flow corroboration
# ---------------------------------------------------------------------------


def test_flow_corroboration_recent_uw_flow(base_signal):
    """UW_FLOW signal within 14 days on same direction returns the weight."""
    recent = datetime.now(UTC) - timedelta(days=5)
    prior = [make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA", disclosed_at=recent)]
    scorer = Scorer()
    assert scorer._score_flow_corroboration(base_signal, prior) == 2.0


def test_flow_corroboration_stale_uw_flow(base_signal):
    """UW_FLOW signal older than 14 days returns 0."""
    old = datetime.now(UTC) - timedelta(days=20)
    prior = [make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA", disclosed_at=old)]
    scorer = Scorer()
    assert scorer._score_flow_corroboration(base_signal, prior) == 0.0


def test_flow_corroboration_wrong_direction(base_signal):
    """UW_FLOW on opposite direction returns 0."""
    recent = datetime.now(UTC) - timedelta(days=1)
    prior = [make_signal(SignalSource.UW_FLOW, Action.SELL, "NVDA", disclosed_at=recent)]
    scorer = Scorer()
    assert scorer._score_flow_corroboration(base_signal, prior) == 0.0


def test_flow_corroboration_non_uw_flow_source(base_signal):
    """Non-UW_FLOW source within 14 days returns 0."""
    recent = datetime.now(UTC) - timedelta(days=1)
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA", disclosed_at=recent)]
    scorer = Scorer()
    assert scorer._score_flow_corroboration(base_signal, prior) == 0.0


# ---------------------------------------------------------------------------
# Size scoring
# ---------------------------------------------------------------------------


def test_size_above_100k():
    """size_high_usd >= 100k returns full weight (2.0)."""
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="sz-1",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
        size_high_usd=Decimal("150000"),
    )
    scorer = Scorer()
    assert scorer._score_size(sig) == 2.0


def test_size_exactly_100k():
    """size_high_usd == 100k returns full weight."""
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="sz-2",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
        size_high_usd=Decimal("100000"),
    )
    scorer = Scorer()
    assert scorer._score_size(sig) == 2.0


def test_size_between_50k_and_100k():
    """size_high_usd between 50k and 100k returns half weight (1.0)."""
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="sz-3",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
        size_high_usd=Decimal("75000"),
    )
    scorer = Scorer()
    assert scorer._score_size(sig) == 1.0


def test_size_below_50k():
    """size_high_usd < 50k returns 0."""
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="sz-4",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
        size_high_usd=Decimal("30000"),
    )
    scorer = Scorer()
    assert scorer._score_size(sig) == 0.0


def test_size_none():
    """size_high_usd is None returns 0."""
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="sz-5",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
    )
    scorer = Scorer()
    assert scorer._score_size(sig) == 0.0


# ---------------------------------------------------------------------------
# Track record scoring
# ---------------------------------------------------------------------------


def test_track_record_50pct_win_rate():
    """50% win rate returns 0."""
    scorer = Scorer()
    assert scorer._score_track_record(0.5) == 0.0


def test_track_record_below_50pct():
    """Below 50% returns 0."""
    scorer = Scorer()
    assert scorer._score_track_record(0.3) == 0.0


def test_track_record_70pct():
    """70% win rate gives (0.7-0.5)/0.25 * 2.0 = 1.6."""
    scorer = Scorer()
    result = scorer._score_track_record(0.7)
    assert abs(result - 1.6) < 1e-9


def test_track_record_75pct_is_max():
    """75% win rate gives full max (2.0)."""
    scorer = Scorer()
    assert scorer._score_track_record(0.75) == 2.0


def test_track_record_above_75pct_capped():
    """Win rates above 75% are capped at max (2.0)."""
    scorer = Scorer()
    assert scorer._score_track_record(0.90) == 2.0


def test_track_record_60pct():
    """60% win rate gives linear interpolation: (0.6-0.5)/0.25 * 2.0 = 0.8."""
    scorer = Scorer()
    result = scorer._score_track_record(0.6)
    assert abs(result - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# Custom weights
# ---------------------------------------------------------------------------


def test_custom_weights_override_defaults(base_signal):
    """Scorer uses custom weights when provided."""
    custom = {"size_above_p90": 5.0}
    scorer = Scorer(weights=custom)
    assert scorer._score_size(base_signal) == 5.0  # base_signal has size_high_usd=100k


def test_custom_weights_cluster_max():
    """Custom cluster_max is respected."""
    custom = {"cluster_max": 1.0, "cluster_per_extra_source": 1.0}
    scorer = Scorer(weights=custom)
    sig = RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="cw-1",
        ticker="NVDA",
        action=Action.BUY,
        disclosed_at=datetime.now(UTC),
    )
    prior = [
        make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA"),
        make_signal(SignalSource.UW_FLOW, Action.BUY, "NVDA"),
        make_signal(SignalSource.SEC_13D, Action.BUY, "NVDA"),
    ]
    assert scorer._score_cluster(sig, prior) == 1.0


def test_custom_weights_insider_corroboration(base_signal):
    """Custom insider_corroboration weight is used."""
    custom = {"insider_corroboration": 10.0}
    scorer = Scorer(weights=custom)
    prior = [make_signal(SignalSource.SEC_FORM4, Action.BUY, "NVDA")]
    assert scorer._score_insider_corroboration(base_signal, prior) == 10.0
