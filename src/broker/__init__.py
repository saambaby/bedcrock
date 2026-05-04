"""Broker package — adapter factory.

Uses IBKR for both paper and live trading. Paper vs live is controlled by
the IBKR_PORT setting (4002 = paper, 4001 = live) and the MODE env var.
"""

from src.broker.base import (
    AccountSnapshot,
    BracketOrderRequest,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerOrderState,
    OrderRejectedError,
)
from src.broker.ibkr import IBKRBroker

# Compatibility aliases — older code uses these names
BaseBroker = BrokerAdapter
AccountState = AccountSnapshot
SubmittedBracket = BrokerOrder


def make_broker() -> BrokerAdapter:
    """Return the IBKR broker adapter."""
    return IBKRBroker()


# Back-compat name used by workers
get_broker = make_broker

__all__ = [
    "AccountSnapshot",
    "AccountState",
    "BaseBroker",
    "BracketOrderRequest",
    "BrokerAdapter",
    "BrokerError",
    "BrokerOrder",
    "BrokerOrderState",
    "IBKRBroker",
    "OrderRejectedError",
    "SubmittedBracket",
    "get_broker",
    "make_broker",
]
