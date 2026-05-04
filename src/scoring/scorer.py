"""Signal scorer.

Reads a RawSignal plus contextual data (other recent signals on the same ticker,
indicators, the trader's track record) and returns a ScoreBreakdown + total.

Initial weights from `99-Meta/scoring-rules.md`. The weekly synthesis updates
that file via the proposed → adopted flow; the scorer reads the live values
on each invocation so changes take effect without redeploy.

This is pure logic — no I/O. The orchestrator passes in pre-fetched context.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.db.models import Action, Signal, SignalSource
from src.schemas import IndicatorSnapshot, RawSignal, ScoreBreakdown

# Default weights — overridden by 99-Meta/scoring-rules.md if present.
DEFAULT_WEIGHTS = {
    "cluster_per_extra_source": 1.0,
    "cluster_max": 3.0,
    "size_above_p90": 2.0,
    "insider_corroboration": 2.0,
    "options_flow_corroboration": 2.0,
    "trader_track_record_bonus_max": 2.0,
    "trend_alignment": 1.0,
    "relative_strength_strong": 1.0,
}


class Scorer:
    def __init__(self, weights: dict | None = None) -> None:
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}

    def score(
        self,
        signal: RawSignal,
        prior_signals_30d: list[Signal],
        indicators: IndicatorSnapshot | None,
        trader_track_record: float | None = None,
    ) -> tuple[float, ScoreBreakdown]:
        """Returns (total_score, breakdown).

        - prior_signals_30d: signals on the same ticker in the last 30 days,
          INCLUDING from other sources/traders. Used for cluster and corroboration.
        - indicators: cached IndicatorSnapshot for this ticker.
        - trader_track_record: rolling win-rate or excess-return for this trader,
          if known. None on first observation.
        """
        b = ScoreBreakdown()

        # 1. Cluster — distinct sources/traders agreeing on direction in 30d
        b.cluster = self._score_cluster(signal, prior_signals_30d)

        # 2. Insider corroboration — Form 4 buys in same window for buy signals
        b.insider_corroboration = self._score_insider_corroboration(signal, prior_signals_30d)

        # 3. Options flow corroboration — UW flow in same direction in 14d
        b.options_flow_corroboration = self._score_flow_corroboration(signal, prior_signals_30d)

        # 4. Size — only for politicians and 13F (where size matters)
        b.size = self._score_size(signal)

        # 5. Trader track record
        if trader_track_record is not None:
            b.trader_track_record = self._score_track_record(trader_track_record)

        # 6. Indicator-driven modifiers — only valid if we have indicators
        if indicators is not None:
            b.trend_alignment = self._score_trend_alignment(signal, indicators)
            b.relative_strength = self._score_relative_strength(indicators)

        # committee_match, public_statement, sentiment, regime_overlay are
        # populated by upstream enrichers before scoring runs (TODO v0.2).

        return b.total, b

    # --- Component scorers ---

    def _score_cluster(self, signal: RawSignal, prior: list[Signal]) -> float:
        """+1 per additional independent source on this ticker, same direction, 30d."""
        same_dir = [s for s in prior if s.action == signal.action and s.ticker == signal.ticker]
        # Distinct sources excluding our own ingestion
        sources = {s.source for s in same_dir} - {signal.source}
        # Distinct traders
        traders = {s.trader_id for s in same_dir if s.trader_id is not None}
        independent_count = len(sources) + max(0, len(traders) - len(sources))
        score = independent_count * self.weights["cluster_per_extra_source"]
        return min(score, self.weights["cluster_max"])

    def _score_insider_corroboration(self, signal: RawSignal, prior: list[Signal]) -> float:
        if signal.action != Action.BUY:
            return 0.0
        if any(s.source == SignalSource.SEC_FORM4 and s.action == Action.BUY for s in prior):
            return self.weights["insider_corroboration"]
        return 0.0

    def _score_flow_corroboration(self, signal: RawSignal, prior: list[Signal]) -> float:
        cutoff = datetime.now(UTC) - timedelta(days=14)
        for s in prior:
            if s.source == SignalSource.UW_FLOW and s.disclosed_at >= cutoff and s.action == signal.action:
                return self.weights["options_flow_corroboration"]
        return 0.0

    def _score_size(self, signal: RawSignal) -> float:
        # Politicians: size matters; flag big trades as more conviction.
        if signal.size_high_usd is None:
            return 0.0
        if signal.size_high_usd >= Decimal("100000"):
            return self.weights["size_above_p90"]
        if signal.size_high_usd >= Decimal("50000"):
            return self.weights["size_above_p90"] * 0.5
        return 0.0

    def _score_track_record(self, win_rate: float) -> float:
        # Linear scale: 50% win rate -> 0, 70% -> max
        if win_rate <= 0.5:
            return 0.0
        bounded = min(win_rate, 0.75)
        return ((bounded - 0.5) / 0.25) * self.weights["trader_track_record_bonus_max"]

    def _score_trend_alignment(self, signal: RawSignal, ind: IndicatorSnapshot) -> float:
        if ind.trend is None:
            return 0.0
        if signal.action == Action.BUY and ind.trend == "uptrend":
            return self.weights["trend_alignment"]
        if signal.action == Action.SELL and ind.trend == "downtrend":
            return self.weights["trend_alignment"]
        if signal.action == Action.BUY and ind.trend == "downtrend":
            return -self.weights["trend_alignment"]
        return 0.0

    def _score_relative_strength(self, ind: IndicatorSnapshot) -> float:
        if ind.rs_vs_sector_60d is None:
            return 0.0
        if float(ind.rs_vs_sector_60d) >= 1.0:
            return self.weights["relative_strength_strong"]
        return 0.0
