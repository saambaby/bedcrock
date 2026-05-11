"""Wave C/C1 — LiveMonitor._handle_update is broker-agnostic.

Fakes a BrokerAdapter whose subscribe_trade_updates yields one entry fill,
and asserts the monitor inserts a Position row + flips the draft to FILLED.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderState,
    TradeUpdate,
)
from src.db.models import (
    Action,
    DraftOrder,
    Mode,
    OrderStatus,
    Position,
    PositionStatus,
)
from src.orders.monitor import LiveMonitor


class _FakeAdapter(BrokerAdapter):
    def __init__(self, updates: list[TradeUpdate]) -> None:
        self._updates = updates

    @property
    def name(self) -> str:
        return "fake"

    @property
    def is_paper(self) -> bool:
        return True

    async def get_account(self) -> AccountSnapshot:  # pragma: no cover
        raise NotImplementedError

    async def submit_bracket(self, spec):  # pragma: no cover
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover
        return None

    async def get_order(self, broker_order_id: str) -> BrokerOrder:  # pragma: no cover
        raise NotImplementedError

    async def get_last_price(self, ticker: str):  # pragma: no cover
        return None

    async def iter_open_orders(self):  # pragma: no cover
        if False:
            yield None

    async def iter_positions(self):  # pragma: no cover
        if False:
            yield None

    async def repair_child_to_gtc(self, broker_order_id: str) -> str:  # pragma: no cover
        return broker_order_id

    async def subscribe_trade_updates(self) -> AsyncIterator[TradeUpdate]:
        for u in self._updates:
            yield u


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _DB:
    """Minimal AsyncSession stand-in that returns scripted query results."""

    def __init__(self, draft: DraftOrder | None) -> None:
        self.draft = draft
        self.added: list = []
        self.commit_count = 0
        self._queries = 0

    async def execute(self, stmt):
        self._queries += 1
        # First query: find draft by ref (returns draft)
        # Second query: existing Position by broker_order_id (returns None)
        if self._queries == 1:
            return _Result(self.draft)
        return _Result(None)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1


class _DBFactory:
    def __init__(self, db: _DB) -> None:
        self._db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_handle_update_entry_fill_creates_position() -> None:
    draft = DraftOrder(
        id=uuid.uuid4(),
        mode=Mode.PAPER,
        ticker="AAPL",
        side=Action.BUY,
        quantity=Decimal("10"),
        entry_limit=Decimal("150"),
        stop=Decimal("145"),
        target=Decimal("160"),
        setup="breakout",
        status=OrderStatus.SENT,
        source_signal_ids=[],
    )

    update = TradeUpdate(
        event="fill",
        broker_order_id="parent-1",
        client_order_id=str(draft.id),
        ticker="AAPL",
        filled_qty=Decimal("10"),
        filled_avg_price=Decimal("150.25"),
        timestamp=datetime.now(UTC),
        raw={"parent_id": 0},  # 0 = this IS the parent
    )

    db = _DB(draft=draft)
    factory = _DBFactory(db)

    # Patch make_broker so LiveMonitor doesn't try to construct IBKR/Alpaca.
    fake_adapter = _FakeAdapter([update])
    with patch("src.orders.monitor.make_broker", return_value=fake_adapter), patch(
        "src.orders.monitor.post_position_alert", new=AsyncMock()
    ):
        monitor = LiveMonitor()
        await monitor._handle_update(update, factory)

    # Position was inserted with the right broker_order_id
    positions = [a for a in db.added if isinstance(a, Position)]
    assert len(positions) == 1
    pos = positions[0]
    assert pos.broker_order_id == "parent-1"
    assert pos.ticker == "AAPL"
    assert pos.quantity == Decimal("10")
    assert pos.entry_price == Decimal("150.25")
    assert pos.status == PositionStatus.OPEN

    # Draft flipped to FILLED
    assert draft.status == OrderStatus.FILLED
    assert db.commit_count >= 1


@pytest.mark.asyncio
async def test_handle_update_canceled_marks_draft_cancelled() -> None:
    draft = DraftOrder(
        id=uuid.uuid4(),
        mode=Mode.PAPER,
        ticker="AAPL",
        side=Action.BUY,
        quantity=Decimal("10"),
        entry_limit=Decimal("150"),
        stop=Decimal("145"),
        target=Decimal("160"),
        setup="breakout",
        status=OrderStatus.SENT,
        source_signal_ids=[],
    )

    update = TradeUpdate(
        event="canceled",
        broker_order_id="parent-1",
        client_order_id=str(draft.id),
        ticker="AAPL",
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        timestamp=datetime.now(UTC),
        raw={},
    )

    class _SingleQueryDB(_DB):
        async def execute(self, stmt):
            return _Result(self.draft)

    db = _SingleQueryDB(draft=draft)
    factory = _DBFactory(db)
    fake_adapter = _FakeAdapter([update])
    with patch("src.orders.monitor.make_broker", return_value=fake_adapter):
        monitor = LiveMonitor()
        await monitor._handle_update(update, factory)

    assert draft.status == OrderStatus.CANCELLED
    assert db.commit_count == 1


# Quietly bind the unused enum import so it isn't pruned by Ruff.
_ = BrokerOrderState
_ = asyncio
