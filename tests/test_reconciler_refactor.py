"""Wave C/C1 — reconciler operates against the abstract BrokerAdapter contract.

Exercises ``audit_open_order_tifs`` against a fake adapter that yields a mix
of GTC and DAY children, plus a non-child parent. ``repair_child_to_gtc``
must be called only for non-GTC repairable children.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOrder,
    BrokerOrderState,
    OpenOrder,
)
from src.db.models import Action
from src.safety.reconciler import audit_open_order_tifs


class _FakeBroker(BrokerAdapter):
    """Concrete BrokerAdapter stub that lets us script open-order responses."""

    def __init__(self, orders: list[OpenOrder]) -> None:
        self._orders = orders
        self.repair_calls: list[str] = []
        # Sequence of new ids ``repair_child_to_gtc`` will return.
        self._new_id_counter = 1000

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

    async def iter_open_orders(self) -> AsyncIterator[OpenOrder]:
        for o in self._orders:
            yield o

    async def iter_positions(self):  # pragma: no cover
        if False:
            yield None

    async def repair_child_to_gtc(self, broker_order_id: str) -> str:
        self.repair_calls.append(broker_order_id)
        self._new_id_counter += 1
        return str(self._new_id_counter)


def _open(
    *,
    broker_order_id: str,
    parent_order_id: str | None,
    order_type: str,
    tif: str,
    ticker: str = "AAPL",
) -> OpenOrder:
    return OpenOrder(
        broker_order_id=broker_order_id,
        parent_order_id=parent_order_id,
        ticker=ticker,
        side=Action.BUY,
        order_type=order_type,
        quantity=Decimal("10"),
        limit_price=None,
        stop_price=None,
        tif=tif,
        raw={},
    )


def test_audit_repairs_only_non_gtc_children():
    orders = [
        # Parent — DAY but exempt because parent_order_id is None
        _open(broker_order_id="1", parent_order_id=None, order_type="limit", tif="day"),
        # GTC stop — skip
        _open(broker_order_id="2", parent_order_id="1", order_type="stop", tif="gtc"),
        # DAY stop — REPAIR
        _open(broker_order_id="3", parent_order_id="1", order_type="stop", tif="day"),
        # DAY limit child (take-profit) — REPAIR
        _open(broker_order_id="4", parent_order_id="1", order_type="limit", tif="day"),
        # DAY MARKET child — skip (not in repairable order_type set)
        _open(broker_order_id="5", parent_order_id="1", order_type="market", tif="day"),
        # DAY trailing_stop — REPAIR
        _open(broker_order_id="6", parent_order_id="1", order_type="trailing_stop", tif="day"),
    ]
    broker = _FakeBroker(orders)

    with patch(
        "src.safety.reconciler.post_system_health", new=AsyncMock()
    ):
        new_ids = asyncio.run(audit_open_order_tifs(broker))

    # Exactly 3 repairs (ids 3, 4, 6)
    assert broker.repair_calls == ["3", "4", "6"]
    # The returned ids are the fresh ones produced by the adapter
    assert len(new_ids) == 3
    assert all(int(nid) >= 1001 for nid in new_ids)


def test_audit_returns_empty_when_all_gtc():
    orders = [
        _open(broker_order_id="1", parent_order_id=None, order_type="limit", tif="day"),
        _open(broker_order_id="2", parent_order_id="1", order_type="stop", tif="gtc"),
        _open(broker_order_id="3", parent_order_id="1", order_type="limit", tif="gtc"),
    ]
    broker = _FakeBroker(orders)
    new_ids = asyncio.run(audit_open_order_tifs(broker))
    assert new_ids == []
    assert broker.repair_calls == []


def test_audit_swallows_repair_failure():
    orders = [
        _open(broker_order_id="1", parent_order_id=None, order_type="limit", tif="day"),
        _open(broker_order_id="2", parent_order_id="1", order_type="stop", tif="day"),
    ]

    class _BoomBroker(_FakeBroker):
        async def repair_child_to_gtc(self, broker_order_id: str) -> str:
            raise RuntimeError("broker down")

    broker = _BoomBroker(orders)
    with patch("src.safety.reconciler.post_system_health", new=AsyncMock()):
        new_ids = asyncio.run(audit_open_order_tifs(broker))
    # Failure is logged and skipped; returned list is empty.
    assert new_ids == []


# Mark the abstract-method stubs as not async generators where needed.
# (asyncio is imported at top, BrokerOrderState exported for future use.)
_ = BrokerOrderState
