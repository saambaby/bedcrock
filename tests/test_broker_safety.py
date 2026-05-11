"""Tests for Wave B1 — broker safety (GTC TIF, reconciler, connect retry)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broker.base import BrokerError
from src.broker.ibkr import IBKRBroker
from src.db.models import (
    Action,
    AuditLog,
    CloseReason,
    Mode,
    Position,
    PositionStatus,
)
from src.safety.reconciler import (
    audit_open_order_tifs,
    reconcile_against_broker,
)
from src.schemas import BracketOrderSpec


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_order(
    *,
    order_id: int,
    parent_id: int = 0,
    tif: str = "DAY",
    order_type: str = "STP",
    outside_rth: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        orderId=order_id,
        parentId=parent_id,
        tif=tif,
        outsideRth=outside_rth,
        orderType=order_type,
        orderRef=None,
    )


def _make_trade(order, symbol: str = "AAPL") -> SimpleNamespace:
    return SimpleNamespace(
        order=order,
        contract=SimpleNamespace(symbol=symbol),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0),
    )


class _FakeIB:
    """Minimal IB stub capturing placeOrder + cancelOrder calls."""

    def __init__(self, open_trades=None, positions=None):
        self.placed_orders: list = []
        self.cancelled_orders: list = []
        self._open_trades = open_trades or []
        self._positions = positions or []
        self._connected = True
        self.reqPositionsAsync = AsyncMock(return_value=None)

    def isConnected(self) -> bool:
        return self._connected

    def openTrades(self):
        return list(self._open_trades)

    def positions(self):
        return list(self._positions)

    def placeOrder(self, contract, order):
        self.placed_orders.append(order)
        return _make_trade(order, symbol=getattr(contract, "symbol", "AAPL"))

    def cancelOrder(self, order):
        self.cancelled_orders.append(order)

    async def qualifyContractsAsync(self, contract):
        return [contract]

    def bracketOrder(self, *, action, quantity, limitPrice, takeProfitPrice, stopLossPrice):
        parent = _make_order(order_id=1, parent_id=0, tif="DAY", order_type="LMT")
        tp = _make_order(order_id=2, parent_id=1, tif="DAY", order_type="LMT")
        sl = _make_order(order_id=3, parent_id=1, tif="DAY", order_type="STP")
        return [parent, tp, sl]


# ---------------------------------------------------------------------------
# 1) TIF tests
# ---------------------------------------------------------------------------


def test_bracket_children_are_gtc():
    fake_ib = _FakeIB()
    broker = IBKRBroker()
    broker._ib = fake_ib
    broker._connected = True

    spec = BracketOrderSpec(
        mode=Mode.PAPER,
        ticker="AAPL",
        side=Action.BUY,
        quantity=Decimal("10"),
        entry_limit=Decimal("100"),
        stop=Decimal("90"),
        target=Decimal("120"),
        setup="breakout",
    )
    asyncio.run(broker.submit_bracket(spec))

    placed = fake_ib.placed_orders
    assert len(placed) == 3
    # parent
    assert placed[0].tif == "DAY"
    assert placed[0].outsideRth is False
    # take-profit child
    assert placed[1].tif == "GTC"
    assert placed[1].outsideRth is True
    # stop-loss child
    assert placed[2].tif == "GTC"
    assert placed[2].outsideRth is True


# ---------------------------------------------------------------------------
# 2) Audit repairs non-GTC child
# ---------------------------------------------------------------------------


def test_audit_repairs_non_gtc_child():
    bad_child = _make_order(
        order_id=42, parent_id=10, tif="DAY", order_type="STP", outside_rth=False
    )
    trade = _make_trade(bad_child, symbol="AAPL")
    fake_ib = _FakeIB(open_trades=[trade])

    broker = IBKRBroker()
    broker._ib = fake_ib
    broker._connected = True

    with patch(
        "src.safety.reconciler.post_system_health", new=AsyncMock()
    ) as mock_alert:
        asyncio.run(audit_open_order_tifs(broker))

    assert bad_child in fake_ib.cancelled_orders
    # Re-issued with GTC + outsideRth
    assert any(
        o.tif == "GTC" and o.outsideRth is True for o in fake_ib.placed_orders
    )
    mock_alert.assert_awaited()


def test_audit_skips_parent_orders():
    """Parents (parentId == 0) may legitimately be DAY — do not touch."""
    parent = _make_order(
        order_id=99, parent_id=0, tif="DAY", order_type="LMT"
    )
    trade = _make_trade(parent, symbol="AAPL")
    fake_ib = _FakeIB(open_trades=[trade])

    broker = IBKRBroker()
    broker._ib = fake_ib
    broker._connected = True

    asyncio.run(audit_open_order_tifs(broker))
    assert fake_ib.cancelled_orders == []
    assert fake_ib.placed_orders == []


# ---------------------------------------------------------------------------
# 3) Reconcile orphan + 4) reconcile stale
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _FakeDB:
    def __init__(self, db_positions):
        self._db_positions = db_positions
        self.added: list = []
        self.committed = False

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._db_positions)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _make_position(ticker: str) -> Position:
    return Position(
        id=uuid.uuid4(),
        mode=Mode.PAPER,
        ticker=ticker,
        side=Action.BUY,
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        stop=Decimal("90"),
        target=Decimal("120"),
        status=PositionStatus.OPEN,
    )


def test_reconcile_orphan_alert():
    """IBKR has AAPL, DB doesn't → AuditLog + post_position_alert."""
    ibkr_pos = SimpleNamespace(
        contract=SimpleNamespace(symbol="AAPL"),
        position=10,
        avgCost=150.5,
    )
    fake_ib = _FakeIB(positions=[ibkr_pos])
    broker = IBKRBroker()
    broker._ib = fake_ib
    broker._connected = True

    db = _FakeDB(db_positions=[])

    with patch(
        "src.safety.reconciler.post_position_alert", new=AsyncMock()
    ) as mock_alert:
        asyncio.run(reconcile_against_broker(broker, db))

    audit_entries = [a for a in db.added if isinstance(a, AuditLog)]
    assert any(
        a.action == "orphan_broker_detected" and a.target_id == "AAPL"
        for a in audit_entries
    )
    mock_alert.assert_awaited()
    assert db.committed is True


def test_reconcile_stale_marks_closed():
    """DB has MSFT open, IBKR doesn't → marked CLOSED with EXTERNAL."""
    msft = _make_position("MSFT")
    fake_ib = _FakeIB(positions=[])  # no IBKR positions
    broker = IBKRBroker()
    broker._ib = fake_ib
    broker._connected = True

    db = _FakeDB(db_positions=[msft])

    with patch(
        "src.safety.reconciler.post_position_alert", new=AsyncMock()
    ):
        asyncio.run(reconcile_against_broker(broker, db))

    assert msft.status == PositionStatus.CLOSED
    assert msft.close_reason == CloseReason.EXTERNAL
    assert msft.exit_at is not None
    audit_entries = [a for a in db.added if isinstance(a, AuditLog)]
    assert any(a.action == "closed_externally" for a in audit_entries)
    assert db.committed is True


# ---------------------------------------------------------------------------
# 5) Connect retry
# ---------------------------------------------------------------------------


def test_connect_retry_succeeds_on_third_attempt():
    broker = IBKRBroker()

    # First two attempts raise, third returns None (success).
    call_count = {"n": 0}

    async def flaky_connect(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise OSError("connection refused")
        return None

    broker._ib = MagicMock()
    broker._ib.isConnected.return_value = False
    broker._ib.connectAsync = flaky_connect

    # Patch sleep to avoid waiting through the backoff schedule.
    with patch("src.broker.ibkr.asyncio.sleep", new=AsyncMock()):
        asyncio.run(broker.connect())

    assert call_count["n"] == 3
    assert broker._connected is True


def test_connect_raises_after_all_attempts():
    broker = IBKRBroker()

    async def always_fail(**_kwargs):
        raise OSError("connection refused")

    broker._ib = MagicMock()
    broker._ib.isConnected.return_value = False
    broker._ib.connectAsync = always_fail

    with patch("src.broker.ibkr.asyncio.sleep", new=AsyncMock()), patch(
        "src.discord_bot.webhooks.post_system_health", new=AsyncMock()
    ) as mock_alert:
        with pytest.raises(BrokerError):
            asyncio.run(broker.connect())

    mock_alert.assert_awaited()
