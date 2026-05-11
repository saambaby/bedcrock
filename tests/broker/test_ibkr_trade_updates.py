"""Wave C/C2 — IBKRBroker.subscribe_trade_updates bridges ib_async events
into a normalized ``TradeUpdate`` stream.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.broker.base import TradeUpdate
from src.broker.ibkr import IBKRBroker


class _FakeEvent:
    """Tiny stand-in for ib_async's signal-like Event objects.

    Supports ``+=`` to subscribe a handler and ``-=`` to detach.
    Tests fire events by calling ``emit(*args)``.
    """

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass
        return self

    def emit(self, *args) -> None:
        for h in list(self._handlers):
            h(*args)


class _FakeIB:
    def __init__(self) -> None:
        self.orderStatusEvent = _FakeEvent()
        self.execDetailsEvent = _FakeEvent()
        self._connected = True

    def isConnected(self) -> bool:  # noqa: N802 — mimics ib_async API
        return self._connected


@pytest.mark.asyncio
async def test_subscribe_trade_updates_yields_status_and_exec() -> None:
    """Firing one exec_details and one order_status event must surface
    two TradeUpdates from the generator, with mapped event vocabulary.
    """
    broker = IBKRBroker()
    fake_ib = _FakeIB()
    broker._ib = fake_ib
    broker._connected = True

    gen = broker.subscribe_trade_updates()

    # Drive the generator past subscription setup. ``__anext__`` will await
    # on the queue — schedule it as a task and emit events from the test.
    async def _collect_two() -> list[TradeUpdate]:
        out = []
        async for upd in gen:
            out.append(upd)
            if len(out) == 2:
                break
        return out

    task = asyncio.create_task(_collect_two())
    # Yield control so the generator can subscribe its handlers.
    await asyncio.sleep(0)
    assert len(fake_ib.orderStatusEvent._handlers) == 1
    assert len(fake_ib.execDetailsEvent._handlers) == 1

    # Fire a status event: parent order, status=Submitted (event=new)
    parent_order = SimpleNamespace(orderId=101, orderRef="draft-abc", parentId=0)
    status_obj = SimpleNamespace(
        status="Submitted", filled=0, remaining=10, avgFillPrice=0
    )
    trade1 = SimpleNamespace(
        order=parent_order,
        orderStatus=status_obj,
        contract=SimpleNamespace(symbol="AAPL"),
    )
    fake_ib.orderStatusEvent.emit(trade1)

    # Fire an exec_details event: child fill, remaining 0 → "fill"
    child_order = SimpleNamespace(orderId=102, orderRef=None, parentId=101)
    execution = SimpleNamespace(
        shares=10, avgPrice=150.25, execId="ex-1"
    )
    fill = SimpleNamespace(
        execution=execution,
        contract=SimpleNamespace(symbol="AAPL"),
    )
    trade2 = SimpleNamespace(
        order=child_order,
        orderStatus=SimpleNamespace(status="Filled", filled=10, remaining=0, avgFillPrice=150.25),
        contract=SimpleNamespace(symbol="AAPL"),
    )
    fake_ib.execDetailsEvent.emit(trade2, fill)

    updates = await asyncio.wait_for(task, timeout=2.0)
    assert len(updates) == 2

    # The order is queue-FIFO. status first, exec second.
    u1, u2 = updates
    assert u1.event == "new"
    assert u1.broker_order_id == "101"
    assert u1.client_order_id == "draft-abc"
    assert u1.ticker == "AAPL"

    assert u2.event == "fill"
    assert u2.broker_order_id == "102"
    assert u2.filled_qty == Decimal("10")
    assert u2.filled_avg_price == Decimal("150.25")
    assert u2.raw["parent_id"] == 101


@pytest.mark.asyncio
async def test_subscribe_trade_updates_cleans_up_on_cancel() -> None:
    """Cancelling the consumer must detach the handlers from ib_async."""
    broker = IBKRBroker()
    fake_ib = _FakeIB()
    broker._ib = fake_ib
    broker._connected = True

    gen = broker.subscribe_trade_updates()

    async def _consume() -> None:
        async for _ in gen:
            pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    assert len(fake_ib.orderStatusEvent._handlers) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # After cancellation propagates, handlers must be detached.
    assert fake_ib.orderStatusEvent._handlers == []
    assert fake_ib.execDetailsEvent._handlers == []
