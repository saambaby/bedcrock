"""Tests for hard gates (non-DB gates only).

These test the stale signal and liquidity gates which don't require a DB session.

Run: pytest tests/test_gates.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.db.models import Action, GateName, SignalSource
from src.schemas import IndicatorSnapshot, RawSignal
from src.scoring.gates import GateEvaluator


@pytest.fixture
def evaluator():
    return GateEvaluator()


def _make_raw_signal(disclosed_at=None, ticker="NVDA"):
    return RawSignal(
        source=SignalSource.QUIVER_CONGRESS,
        source_external_id="gate-test-1",
        ticker=ticker,
        action=Action.BUY,
        disclosed_at=disclosed_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Stale signal gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_signal_fresh_passes(evaluator):
    """Signal disclosed just now is not stale."""
    signal = _make_raw_signal(disclosed_at=datetime.now(UTC))
    result = await evaluator._gate_stale_signal(signal)
    assert result.gate == GateName.STALE_SIGNAL
    assert result.blocked is False


@pytest.mark.asyncio
async def test_stale_signal_13_days_passes(evaluator):
    """Signal disclosed 13 days ago is still fresh."""
    signal = _make_raw_signal(
        disclosed_at=datetime.now(UTC) - timedelta(days=13)
    )
    result = await evaluator._gate_stale_signal(signal)
    assert result.blocked is False


@pytest.mark.asyncio
async def test_stale_signal_15_days_blocked(evaluator):
    """Signal disclosed 15 days ago is blocked."""
    signal = _make_raw_signal(
        disclosed_at=datetime.now(UTC) - timedelta(days=15)
    )
    result = await evaluator._gate_stale_signal(signal)
    assert result.gate == GateName.STALE_SIGNAL
    assert result.blocked is True
    assert "15d ago" in result.reason


@pytest.mark.asyncio
async def test_stale_signal_30_days_blocked(evaluator):
    """Signal disclosed 30 days ago is blocked."""
    signal = _make_raw_signal(
        disclosed_at=datetime.now(UTC) - timedelta(days=30)
    )
    result = await evaluator._gate_stale_signal(signal)
    assert result.blocked is True


@pytest.mark.asyncio
async def test_stale_signal_exactly_14_days_blocked(evaluator):
    """Signal disclosed exactly 14 days ago is blocked.

    Due to microsecond elapsed between disclosed_at and now() inside the gate,
    the age is slightly over 14 days, so it is blocked.
    """
    signal = _make_raw_signal(
        disclosed_at=datetime.now(UTC) - timedelta(days=14)
    )
    result = await evaluator._gate_stale_signal(signal)
    assert result.blocked is True


# ---------------------------------------------------------------------------
# Liquidity gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liquidity_no_indicators_blocked(evaluator):
    """No indicator data means blocked (fail closed)."""
    result = await evaluator._gate_liquidity(None)
    assert result.gate == GateName.LIQUIDITY
    assert result.blocked is True
    assert "No indicator data" in result.reason


@pytest.mark.asyncio
async def test_liquidity_no_adv_blocked(evaluator):
    """Indicators present but adv_30d_usd is None means blocked."""
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
    )
    result = await evaluator._gate_liquidity(ind)
    assert result.blocked is True


@pytest.mark.asyncio
async def test_liquidity_below_threshold_blocked(evaluator):
    """ADV below risk_min_adv_usd (default 5M) is blocked."""
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        adv_30d_usd=Decimal("1000000"),  # 1M < 5M threshold
    )
    result = await evaluator._gate_liquidity(ind)
    assert result.gate == GateName.LIQUIDITY
    assert result.blocked is True
    assert result.overrideable is False


@pytest.mark.asyncio
async def test_liquidity_above_threshold_passes(evaluator):
    """ADV above risk_min_adv_usd passes."""
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        adv_30d_usd=Decimal("10000000000"),  # 10B >> 5M threshold
    )
    result = await evaluator._gate_liquidity(ind)
    assert result.gate == GateName.LIQUIDITY
    assert result.blocked is False


@pytest.mark.asyncio
async def test_liquidity_exactly_at_threshold_passes(evaluator):
    """ADV exactly at threshold passes (>= comparison)."""
    # Default threshold is 5_000_000
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        adv_30d_usd=Decimal("5000000"),
    )
    result = await evaluator._gate_liquidity(ind)
    assert result.blocked is False


@pytest.mark.asyncio
async def test_liquidity_no_indicators_is_overrideable(evaluator):
    """When no indicator data, the gate is overrideable."""
    result = await evaluator._gate_liquidity(None)
    assert result.overrideable is True


@pytest.mark.asyncio
async def test_liquidity_below_threshold_not_overrideable(evaluator):
    """When ADV is known but too low, the gate is NOT overrideable."""
    ind = IndicatorSnapshot(
        ticker="NVDA",
        computed_at=datetime.now(UTC),
        adv_30d_usd=Decimal("100000"),
    )
    result = await evaluator._gate_liquidity(ind)
    assert result.overrideable is False
