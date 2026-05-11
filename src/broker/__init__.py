"""Broker package — adapter factory.

Dispatches on ``settings.broker``:
  - ``BROKER=ibkr`` (default) — IBKR paper or live via IB Gateway / TWS.
  - ``BROKER=alpaca`` — Alpaca paper only (live is US-only, refused at boot).
"""

from src.broker.base import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerOrderState,
    BrokerPosition,
    OpenOrder,
    OrderRejectedError,
    TradeUpdate,
)
from src.broker.ibkr import IBKRBroker
from src.config import Broker, settings

# Compatibility aliases — older code uses these names
BaseBroker = BrokerAdapter
AccountState = AccountSnapshot
SubmittedBracket = BrokerOrder


def make_broker() -> BrokerAdapter:
    """Return the broker adapter selected by ``settings.broker``."""
    if settings.broker is Broker.ALPACA:
        # Local import so the package still loads when alpaca deps / module
        # are absent (Wave B adds ``src.broker.alpaca``).
        from src.broker.alpaca import AlpacaBroker  # type: ignore[import-not-found]

        return AlpacaBroker()
    return IBKRBroker()


# Back-compat name used by workers
get_broker = make_broker

__all__ = [
    "AccountSnapshot",
    "AccountState",
    "BaseBroker",
    "BracketOrderRequest",
    "Broker",
    "BrokerAdapter",
    "BrokerError",
    "BrokerOrder",
    "BrokerOrderState",
    "BrokerPosition",
    "IBKRBroker",
    "OpenOrder",
    "OrderRejectedError",
    "SubmittedBracket",
    "TradeUpdate",
    "get_broker",
    "make_broker",
]
