"""Broker adapter base class.

The broker is the only thing that actually moves money. Everything else just
*decides* what should happen — the broker makes it real.

The contract is deliberately minimal so the same code works for paper and live
IBKR trading.

submit_bracket() accepts either a BracketOrderRequest (legacy dataclass) or a
BracketOrderSpec (Pydantic model from src.schemas) — both have the same fields.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from src.db.models import Action


class BrokerError(Exception):
    """Base broker error — something went wrong talking to the broker."""


class OrderRejectedError(BrokerError):
    """Broker said no — risk check, insufficient buying power, etc."""


class BrokerOrderState(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class BracketOrderRequest:
    ticker: str
    side: Action
    quantity: Decimal
    entry_limit: Decimal
    stop: Decimal
    target: Decimal
    time_in_force: str = "day"
    client_order_id: str | None = None


@dataclass
class BrokerOrder:
    broker_order_id: str
    state: BrokerOrderState
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    submitted_at: datetime
    raw: dict


@dataclass
class OpenOrder:
    broker_order_id: str
    parent_order_id: str | None  # None means it's a parent / standalone
    ticker: str
    side: Action
    order_type: str  # "limit" | "stop" | "stop_limit" | "trailing_stop"
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str  # "day" | "gtc" | "ioc" | "fok" | "opg" | "cls"
    raw: dict


@dataclass
class BrokerPosition:
    ticker: str
    quantity: Decimal  # signed; negative for short
    avg_entry_price: Decimal
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    raw: dict


@dataclass
class TradeUpdate:
    """One push event from the broker about an order state change."""

    event: str  # "new" | "fill" | "partial_fill" | "canceled" | "rejected" | ...
    broker_order_id: str
    client_order_id: str | None
    ticker: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    timestamp: datetime
    raw: dict


@dataclass
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    positions_value: Decimal
    buying_power: Decimal
    pattern_day_trader: bool


class BrokerAdapter(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    @abc.abstractmethod
    def is_paper(self) -> bool: ...

    # Connection lifecycle. IBKR opens a TCP socket so these matter.
    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def healthcheck(self) -> bool:
        """Default: try to fetch account. Override for cheaper checks."""
        try:
            await self.get_account()
            return True
        except Exception:
            return False

    @abc.abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def submit_bracket(self, spec) -> BrokerOrder:
        """Submit a bracket order. `spec` is a BracketOrderRequest or BracketOrderSpec.

        The stop and target attach as OCO server-side. Idempotent if
        client_order_id is set — broker rejects duplicates.
        """

    @abc.abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abc.abstractmethod
    async def get_order(self, broker_order_id: str) -> BrokerOrder: ...

    @abc.abstractmethod
    async def get_last_price(self, ticker: str) -> Decimal | None: ...

    @abc.abstractmethod
    def iter_open_orders(self) -> AsyncIterator[OpenOrder]:
        """Yield every open order known to the broker.

        Implementations are async generators (``async def`` with ``yield``).
        Declared without ``async def`` here so subclasses can be regular
        async generators — Python doesn't allow abstract async generators
        declared with both ``@abc.abstractmethod`` and a real ``yield`` body.
        """
        ...

    @abc.abstractmethod
    def iter_positions(self) -> AsyncIterator[BrokerPosition]:
        """Yield every non-zero broker-side position."""
        ...

    @abc.abstractmethod
    async def repair_child_to_gtc(self, broker_order_id: str) -> str:
        """Re-issue the given child order as GTC. Returns the new broker_order_id."""
        ...

    def subscribe_trade_updates(self) -> AsyncIterator[TradeUpdate]:
        """Yield trade updates forever until the underlying stream closes.

        Default implementation raises ``NotImplementedError`` — Wave C wires this
        per-broker (IBKR event bridge / Alpaca WebSocket).
        """
        raise NotImplementedError("subclass must implement subscribe_trade_updates")

    async def aclose(self) -> None:
        await self.disconnect()


# ---- Compatibility aliases for older modules ----
BaseBroker = BrokerAdapter
AccountState = AccountSnapshot
SubmittedBracket = BrokerOrder
