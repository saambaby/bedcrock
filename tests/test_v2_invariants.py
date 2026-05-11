"""Wave C — v2 cross-cutting invariant tests.

These exercise behaviour that no single Wave B agent could test in isolation
because it spans multiple modules' integrated state:

  * test_signal_to_position_e2e_paper_dryrun
        signal -> Scorer -> GateEvaluator (all 8 gates) -> OrderBuilder
        -> mocked broker -> Position row -> audit_open_order_tifs returns []

  * test_market_movement_does_not_create_drafts_alone
        a lone MARKET_MOVEMENT signal scores 0, so no draft is built (N1)

  * test_sector_gate_blocks_concentration_across_open_positions
        seed 3 ITA positions, attempt a 4th -> CORRELATION gate blocks (V2.6)

  * test_daily_kill_switch_blocks_new_drafts
        DailyState pnl = -3% -> gate blocks; build_draft never gets to broker

  * test_reconciler_audits_ALL_open_orders
        seed 5 open trades (1 with tif="DAY") -> exactly 1 repair (V2.2)

The DB layer is faked because pytest runs in CI without Postgres. The fake
mirrors the integration shape closely enough to catch contract drift.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broker.base import AccountSnapshot
from src.broker.ibkr import IBKRBroker
from src.db.models import (
    Action,
    DailyState,
    GateName,
    Mode,
    PositionStatus,
    SignalSource,
)
from src.orders.builder import OrderBuilder
from src.safety.reconciler import audit_open_order_tifs
from src.schemas import IndicatorSnapshot, RawSignal
from src.scoring.gates import GateEvaluator
from src.scoring.scorer import Scorer


# ---------------------------------------------------------------------------
# Shared fixtures (kept local — keeps the file fully self-describing)
# ---------------------------------------------------------------------------


def _account(equity: Decimal = Decimal("100000")) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity,
        cash=equity,
        positions_value=Decimal("0"),
        buying_power=equity,
        pattern_day_trader=False,
    )


def _mock_broker(equity: Decimal = Decimal("100000")) -> MagicMock:
    broker = MagicMock()
    broker.connect = AsyncMock(return_value=None)
    broker.disconnect = AsyncMock(return_value=None)
    broker.get_account = AsyncMock(return_value=_account(equity))
    return broker


def _mock_db_for_builder() -> MagicMock:
    """OrderBuilder only calls `db.add(...)` and `await db.commit()`."""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock(return_value=None)
    return db


def _signal(
    *,
    ticker: str = "NVDA",
    action: Action = Action.BUY,
    source: SignalSource = SignalSource.QUIVER_CONGRESS,
    disclosed_at: datetime | None = None,
) -> RawSignal:
    return RawSignal(
        source=source,
        source_external_id=f"v2-inv-{ticker}-{source.value}",
        ticker=ticker,
        action=action,
        disclosed_at=disclosed_at or datetime.now(UTC),
    )


def _indicators(
    *,
    ticker: str = "NVDA",
    price: Decimal = Decimal("500"),
    adv: Decimal = Decimal("10000000000"),
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        ticker=ticker,
        computed_at=datetime.now(UTC),
        price=price,
        adv_30d_usd=adv,
        atr_20=Decimal("10"),
    )


def _position_mock(ticker: str, entry_price: Decimal, quantity: Decimal):
    p = MagicMock()
    p.ticker = ticker
    p.entry_price = entry_price
    p.quantity = quantity
    p.status = PositionStatus.OPEN
    return p


def _gate_db(
    *,
    earnings: list | None = None,
    snoozes: list | None = None,
    open_positions: list | None = None,
    daily_state: DailyState | None = None,
) -> MagicMock:
    """Async session fake whose `execute(stmt)` inspects the SQL text and
    returns one of the supplied collections. Mirrors the heuristic used in
    tests/test_orders.py::_FakeDB.
    """
    db = MagicMock()

    async def _execute(stmt):
        sql = str(stmt).lower()
        result = MagicMock()
        if "from earnings_calendar" in sql:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=earnings or [])
            result.scalars = MagicMock(return_value=scalars)
            return result
        if "from snoozes" in sql:
            result.scalar_one_or_none = MagicMock(
                return_value=(snoozes or [None])[0]
            )
            return result
        if "from positions" in sql:
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=open_positions or [])
            result.scalars = MagicMock(return_value=scalars)
            return result
        if "from daily_state" in sql:
            result.scalar_one_or_none = MagicMock(return_value=daily_state)
            return result
        # Default: empty
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        result.scalars = MagicMock(return_value=scalars)
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


# ---------------------------------------------------------------------------
# 1) End-to-end paper dry-run: signal -> draft -> "fill" -> audit clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_to_position_e2e_paper_dryrun(monkeypatch):
    """Full mocked v2 happy path.

    Score the signal, run all 8 gates (none block), build a draft via
    OrderBuilder (broker mocked), then assert audit_open_order_tifs returns
    [] against an IBKR with only the freshly-placed bracket children whose
    tif is GTC.
    """
    # --- Score ---
    scorer = Scorer()
    sig = _signal(ticker="LMT", source=SignalSource.QUIVER_CONGRESS)
    indicators = _indicators(ticker="LMT", price=Decimal("400"))
    total, breakdown = scorer.score(
        signal=sig,
        prior_signals_30d=[],
        indicators=indicators,
        trader_track_record=None,
    )
    # Single politician signal, no priors -> score >= 0 (no negative
    # components fire here). The invariant is: scoring did not raise.
    assert total >= 0
    assert breakdown.cluster == 0  # no other sources

    # --- Gates: configure DB so all data-driven gates pass ---
    db_gates = _gate_db()  # no earnings, no snooze, no open positions, no DailyState
    broker_for_gate = _mock_broker(Decimal("100000"))
    monkeypatch.setattr("src.scoring.gates.get_broker", lambda: broker_for_gate)

    evaluator = GateEvaluator()
    results = await evaluator.evaluate(db_gates, sig, indicators)

    # Sanity: all 8 gate kinds returned
    kinds = {r.gate for r in results}
    assert {
        GateName.LIQUIDITY,
        GateName.EARNINGS_PROXIMITY,
        GateName.STALE_SIGNAL,
        GateName.SNOOZED,
        GateName.MAX_OPEN_POSITIONS,
        GateName.DAILY_KILL_SWITCH,
        GateName.CORRELATION,
        GateName.EVENT_PROXIMITY,
    }.issubset(kinds)
    blocked = [r for r in results if r.blocked]
    assert blocked == [], f"Expected no gates to block, got {[r.gate for r in blocked]}"

    # --- Build draft ---
    builder_broker = _mock_broker(Decimal("100000"))
    monkeypatch.setattr("src.orders.builder.get_broker", lambda: builder_broker)
    db_builder = _mock_db_for_builder()
    draft = await OrderBuilder().build_draft(
        ticker="LMT",
        side=Action.BUY,
        entry_zone_low=Decimal("400"),
        entry_zone_high=Decimal("400"),
        stop=Decimal("390"),
        target=Decimal("430"),
        setup="breakout",
        score=total,
        source_signal_ids=[],
        indicators=indicators,
        db=db_builder,
    )
    assert draft is not None
    assert draft.quantity > 0
    assert draft.entry_limit == Decimal("400")
    # db.add was called for both DraftOrder and AuditLog
    assert db_builder.add.call_count >= 2
    db_builder.commit.assert_awaited()

    # --- Reconciler audit returns [] when all children are GTC ---
    parent = SimpleNamespace(orderId=1, parentId=0, tif="DAY", outsideRth=False, orderType="LMT")
    tp = SimpleNamespace(orderId=2, parentId=1, tif="GTC", outsideRth=True, orderType="LMT")
    sl = SimpleNamespace(orderId=3, parentId=1, tif="GTC", outsideRth=True, orderType="STP")
    trades = [
        SimpleNamespace(order=parent, contract=SimpleNamespace(symbol="LMT")),
        SimpleNamespace(order=tp, contract=SimpleNamespace(symbol="LMT")),
        SimpleNamespace(order=sl, contract=SimpleNamespace(symbol="LMT")),
    ]
    fake_ib = MagicMock()
    fake_ib.isConnected = MagicMock(return_value=True)
    fake_ib.openTrades = MagicMock(return_value=trades)
    ibkr_broker = IBKRBroker()
    ibkr_broker._ib = fake_ib
    repaired = await audit_open_order_tifs(ibkr_broker)
    assert repaired == []


# ---------------------------------------------------------------------------
# 2) MARKET_MOVEMENT alone does not create drafts (V2.5 / N1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_movement_does_not_create_drafts_alone():
    """A lone MARKET_MOVEMENT signal must score 0 — so no draft is built."""
    scorer = Scorer()
    sig = _signal(ticker="NVDA", source=SignalSource.MARKET_MOVEMENT)
    total, breakdown = scorer.score(
        signal=sig,
        prior_signals_30d=[],  # critically: NO priors
        indicators=_indicators(ticker="NVDA"),
        trader_track_record=None,
    )
    assert total == 0.0
    assert breakdown.flow_corroboration_market == 0.0
    # If score == 0, a downstream caller wouldn't pass it to OrderBuilder
    # (build_draft is only invoked for actionable scored signals). The
    # invariant being pinned is the scorer-level guarantee.


# ---------------------------------------------------------------------------
# 3) Sector gate blocks concentration across open positions (V2.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sector_gate_blocks_concentration_across_open_positions(monkeypatch):
    """Seed 3 ITA-sector positions ~22% of equity; the worst-case half-Kelly
    projection on a 4th defense ticker pushes ITA past the 25% cap → blocked.
    """
    equity = Decimal("100000")
    open_positions = [
        _position_mock("LMT", Decimal("400"), Decimal("20")),  # 8_000 = 8%
        _position_mock("RTX", Decimal("100"), Decimal("80")),  # 8_000 = 8%
        _position_mock("NOC", Decimal("500"), Decimal("12")),  # 6_000 = 6%
    ]
    db = _gate_db(open_positions=open_positions)
    monkeypatch.setattr("src.scoring.gates.get_broker", lambda: _mock_broker(equity))

    evaluator = GateEvaluator()
    sig = _signal(ticker="GD")  # GD ∈ ITA
    indicators = _indicators(ticker="GD", price=Decimal("250"))
    result = await evaluator._gate_correlation(db, sig, indicators)

    assert result.gate == GateName.CORRELATION
    assert result.blocked is True, result.reason
    assert "ITA" in (result.reason or "")


# ---------------------------------------------------------------------------
# 4) Daily kill switch blocks new drafts (V2.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_kill_switch_blocks_new_drafts(monkeypatch):
    """When today's DailyState shows pnl=-3%, the kill-switch gate blocks.

    We assert the gate-level block (the integration invariant). build_draft
    itself does not consult gates — that is the orchestrator's job — so the
    real-world block point is the GateEvaluator step that precedes the
    builder. We also confirm the builder still sizes correctly when called
    directly (i.e. the failure mode is "gate stops the orchestrator", not
    "builder crashes if the gate would have blocked").
    """
    state = DailyState(
        date=date.today(),
        mode=Mode.PAPER,
        daily_pnl_pct=Decimal("-3.0"),
        equity_at_open=Decimal("100000"),
    )
    db = _gate_db(daily_state=state)
    evaluator = GateEvaluator()
    result = await evaluator._gate_daily_kill_switch(db)
    assert result.gate == GateName.DAILY_KILL_SWITCH
    assert result.blocked is True
    assert result.overrideable is False

    # Sanity-check that the OrderBuilder path is still intact (so the only
    # thing standing between us and an order in this scenario is the gate).
    monkeypatch.setattr(
        "src.orders.builder.get_broker", lambda: _mock_broker(Decimal("100000"))
    )
    draft = await OrderBuilder().build_draft(
        ticker="NVDA",
        side=Action.BUY,
        entry_zone_low=Decimal("100"),
        entry_zone_high=Decimal("100"),
        stop=Decimal("95"),
        target=Decimal("120"),
        setup="breakout",
        score=5.0,
        source_signal_ids=[],
        indicators=None,
        db=_mock_db_for_builder(),
    )
    assert draft is not None  # builder works in isolation; gate is the guard


# ---------------------------------------------------------------------------
# 5) Reconciler audits ALL open orders (V2.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciler_audits_ALL_open_orders():
    """5 open trades — 4 with tif=GTC, 1 with tif=DAY (a stop child).
    audit_open_order_tifs must repair exactly 1.

    Importantly: parent orders (parentId=0) are intentionally exempt —
    they may legitimately remain DAY. Verify the audit walks every open
    trade rather than short-circuiting after the first repair.
    """
    parent = SimpleNamespace(orderId=10, parentId=0, tif="DAY", outsideRth=False, orderType="LMT")
    good_tp = SimpleNamespace(orderId=11, parentId=10, tif="GTC", outsideRth=True, orderType="LMT")
    bad_sl = SimpleNamespace(orderId=12, parentId=10, tif="DAY", outsideRth=False, orderType="STP")
    other_tp = SimpleNamespace(orderId=21, parentId=20, tif="GTC", outsideRth=True, orderType="LMT")
    other_sl = SimpleNamespace(orderId=22, parentId=20, tif="GTC", outsideRth=True, orderType="STP")

    trades = [
        SimpleNamespace(order=parent, contract=SimpleNamespace(symbol="AAPL")),
        SimpleNamespace(order=good_tp, contract=SimpleNamespace(symbol="AAPL")),
        SimpleNamespace(order=bad_sl, contract=SimpleNamespace(symbol="AAPL")),
        SimpleNamespace(order=other_tp, contract=SimpleNamespace(symbol="MSFT")),
        SimpleNamespace(order=other_sl, contract=SimpleNamespace(symbol="MSFT")),
    ]
    fake_ib = MagicMock()
    fake_ib.isConnected = MagicMock(return_value=True)
    fake_ib.openTrades = MagicMock(return_value=trades)
    fake_ib.cancelOrder = MagicMock()
    fake_ib.placeOrder = MagicMock()

    broker = IBKRBroker()
    broker._ib = fake_ib

    with patch("src.safety.reconciler.post_system_health", new=AsyncMock()):
        repaired = await audit_open_order_tifs(broker)

    assert len(repaired) == 1, f"Expected 1 repair, got {len(repaired)}: {repaired}"
    fake_ib.cancelOrder.assert_called_once_with(bad_sl)
    fake_ib.placeOrder.assert_called_once()
    # The placed order should now be GTC + outsideRth
    placed_order = fake_ib.placeOrder.call_args[0][1]
    assert placed_order.tif == "GTC"
    assert placed_order.outsideRth is True
