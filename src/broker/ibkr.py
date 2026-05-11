"""Interactive Brokers adapter — paper and live trading.

Uses `ib_async` (the maintained successor to `ib_insync`) to connect to IB
Gateway or TWS. Paper vs live is just a different port:
  - TWS paper: 7497  |  TWS live: 7496
  - Gateway paper: 4002  |  Gateway live: 4001

Bracket orders go in as three linked orders (parent + take-profit + stop-loss)
via ib.bracketOrder(). Stops and targets are enforced server-side — if our
VPS dies, IB still enforces the exits.

Setup:
  1. Download IB Gateway (headless) or TWS (GUI):
     https://www.interactivebrokers.com/en/trading/ib-gateway-stable.php
  2. Configure: API > Settings > Enable ActiveX and Socket Clients
  3. Set IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID, IBKR_ACCOUNT in .env
  4. For paper trading, log in with "Paper Trading" mode

References:
  - https://ib-api.readthedocs.io  (ib_async docs)
  - https://interactivebrokers.github.io/tws-api/
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from ib_async import IB, Stock, Trade

from src.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerOrderState,
    OrderRejectedError,
)
from src.config import settings
from src.db.models import Action
from src.logging_config import get_logger

logger = get_logger(__name__)


def _to_state(status: str) -> BrokerOrderState:
    """Map IB order status strings to our unified enum."""
    s = (status or "").lower()
    if s in ("presubmitted", "submitted", "pendingsubmit", "pendingcancel"):
        return BrokerOrderState.PENDING
    if s == "filled":
        return BrokerOrderState.FILLED
    if s in ("cancelled", "canceled"):
        return BrokerOrderState.CANCELLED
    if s == "inactive":
        return BrokerOrderState.REJECTED
    return BrokerOrderState.PENDING


class IBKRBroker(BrokerAdapter):
    """Interactive Brokers adapter via ib_insync."""

    def __init__(self) -> None:
        self._ib = IB()
        self._connected = False

    @property
    def name(self) -> str:
        return "ibkr_paper" if settings.is_paper else "ibkr_live"

    @property
    def is_paper(self) -> bool:
        return settings.is_paper

    async def connect(self, readonly: bool = False) -> None:
        if self._connected and self._ib.isConnected():
            return
        host = settings.ibkr_host
        port = settings.ibkr_port
        client_id = settings.ibkr_client_id

        # Exponential-backoff retry: 5 attempts at 1s, 2s, 4s, 8s, 16s
        delays = [1, 2, 4, 8, 16]
        last_exc: Exception | None = None
        for attempt, delay in enumerate(delays, start=1):
            try:
                await self._ib.connectAsync(
                    host=host,
                    port=port,
                    clientId=client_id,
                    readonly=readonly,
                    account=settings.ibkr_account or "",
                )
                self._connected = True
                logger.info(
                    "ibkr_connected",
                    host=host,
                    port=port,
                    client_id=client_id,
                    paper=self.is_paper,
                    readonly=readonly,
                    attempt=attempt,
                )
                return
            except Exception as e:
                last_exc = e
                logger.warning(
                    "ibkr_connect_attempt_failed",
                    host=host,
                    port=port,
                    attempt=attempt,
                    delay=delay,
                    error=str(e),
                )
                if attempt < len(delays):
                    await asyncio.sleep(delay)

        # Terminal alert via Discord system-health webhook
        try:
            from src.discord_bot.webhooks import post_system_health

            await post_system_health(
                title="IBKR connection failed",
                body=(
                    f"Could not connect to IB Gateway at {host}:{port} after "
                    f"{len(delays)} attempts. Last error: {last_exc}"
                ),
                ok=False,
            )
        except Exception as alert_exc:
            logger.error("ibkr_connect_alert_failed", error=str(alert_exc))

        raise BrokerError(
            f"Failed to connect to IB Gateway at {host}:{port} after "
            f"{len(delays)} attempts — is IB Gateway/TWS running? "
            f"Last error: {last_exc}"
        ) from last_exc

    async def disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()
        self._connected = False

    async def _ensure(self) -> IB:
        if not self._connected or not self._ib.isConnected():
            await self.connect()
        return self._ib

    async def healthcheck(self) -> bool:
        try:
            if not self._ib.isConnected():
                return False
            # Quick check — request managed accounts (sync call, returns cached list)
            self._ib.managedAccounts()
            return True
        except Exception:
            return False

    async def get_account(self) -> AccountSnapshot:
        ib = await self._ensure()
        account = settings.ibkr_account or ""

        # ib_async provides a native async variant
        summary = await ib.accountSummaryAsync(account)

        values: dict[str, str] = {}
        for item in summary:
            values[item.tag] = item.value

        equity = Decimal(values.get("NetLiquidation", "0"))
        cash = Decimal(values.get("AvailableFunds", "0"))
        positions_val = Decimal(values.get("GrossPositionValue", "0"))
        buying_power = Decimal(values.get("BuyingPower", "0"))

        return AccountSnapshot(
            equity=equity,
            cash=cash,
            positions_value=positions_val,
            buying_power=buying_power,
            pattern_day_trader=False,  # PDT is a US-only concept; not applicable in Canada
        )

    async def submit_bracket(self, spec) -> BrokerOrder:
        """Submit a bracket order (limit entry + stop-loss + take-profit).

        `spec` accepts BracketOrderRequest (dataclass) or BracketOrderSpec (Pydantic).
        """
        ib = await self._ensure()

        ticker: str = spec.ticker.upper()
        side: Action = spec.side
        quantity = float(spec.quantity)
        entry_limit = float(spec.entry_limit)
        stop_price = float(spec.stop)
        target_price = float(spec.target)

        contract = Stock(ticker, "SMART", "USD")
        try:
            qualified = await ib.qualifyContractsAsync(contract)
            if not qualified:
                raise OrderRejectedError(f"Could not qualify contract for {ticker}")
        except OrderRejectedError:
            raise
        except Exception as e:
            raise BrokerError(f"Contract qualification failed for {ticker}: {e}") from e

        ib_action = "BUY" if side == Action.BUY else "SELL"

        bracket = ib.bracketOrder(
            action=ib_action,
            quantity=quantity,
            limitPrice=entry_limit,
            takeProfitPrice=target_price,
            stopLossPrice=stop_price,
        )

        # bracket = [parent, take_profit, stop_loss]
        parent, take_profit, stop_loss = bracket

        # Parent: DAY (entry zone is a same-day decision per plan v1 §6.2)
        parent.tif = "DAY"
        parent.outsideRth = False

        # Children MUST be GTC + outsideRth — otherwise stop expires at session
        # close and overnight gap risk has no protection (audit §3.2).
        for child in (take_profit, stop_loss):
            child.tif = "GTC"
            child.outsideRth = True

        # Set client_order_id on parent for idempotency
        client_order_id = getattr(spec, "client_order_id", None)
        if client_order_id:
            parent.orderRef = client_order_id

        trades: list[Trade] = []
        try:
            for order in bracket:
                # ib_async placeOrder is non-blocking — returns Trade immediately,
                # async work happens via the event loop.
                trade = ib.placeOrder(contract, order)
                trades.append(trade)
        except Exception as e:
            logger.error("ibkr_submit_failed", error=str(e), ticker=ticker)
            raise OrderRejectedError(str(e)) from e

        # The parent order is the first one
        parent_trade = trades[0]
        parent_order = parent_trade.order

        return BrokerOrder(
            broker_order_id=str(parent_order.orderId),
            state=_to_state(parent_trade.orderStatus.status if parent_trade.orderStatus else ""),
            filled_qty=Decimal(str(parent_trade.orderStatus.filled))
            if parent_trade.orderStatus
            else Decimal("0"),
            filled_avg_price=Decimal(str(parent_trade.orderStatus.avgFillPrice))
            if parent_trade.orderStatus and parent_trade.orderStatus.avgFillPrice
            else None,
            submitted_at=datetime.now(UTC),
            raw={"order_id": parent_order.orderId, "bracket_size": len(trades)},
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        ib = await self._ensure()
        # Find the order in open orders
        for trade in ib.openTrades():
            if str(trade.order.orderId) == broker_order_id:
                ib.cancelOrder(trade.order)
                return
        logger.warning("ibkr_cancel_order_not_found", order_id=broker_order_id)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        ib = await self._ensure()
        # Check open trades first
        for trade in ib.openTrades():
            if str(trade.order.orderId) == broker_order_id:
                return self._trade_to_broker_order(trade)
        # Check completed trades
        fills = await ib.reqExecutionsAsync()
        for fill in fills:
            if str(fill.execution.orderId) == broker_order_id:
                return BrokerOrder(
                    broker_order_id=broker_order_id,
                    state=BrokerOrderState.FILLED,
                    filled_qty=Decimal(str(fill.execution.shares)),
                    filled_avg_price=Decimal(str(fill.execution.avgPrice)),
                    submitted_at=datetime.now(UTC),
                    raw={"exec_id": fill.execution.execId},
                )
        raise BrokerError(f"Order {broker_order_id} not found")

    async def get_last_price(self, ticker: str) -> Decimal | None:
        try:
            ib = await self._ensure()
            contract = Stock(ticker.upper(), "SMART", "USD")
            await ib.qualifyContractsAsync(contract)
            [ticker_data] = await ib.reqTickersAsync(contract)
            # Use midpoint if available, otherwise last
            if ticker_data.midpoint() and ticker_data.midpoint() == ticker_data.midpoint():
                return Decimal(str(ticker_data.midpoint()))
            if ticker_data.last and ticker_data.last == ticker_data.last:  # NaN check
                return Decimal(str(ticker_data.last))
            if ticker_data.close and ticker_data.close == ticker_data.close:
                return Decimal(str(ticker_data.close))
            return None
        except Exception as e:
            logger.warning("ibkr_get_price_failed", ticker=ticker, error=str(e))
            return None

    @staticmethod
    def _trade_to_broker_order(trade: Trade) -> BrokerOrder:
        status = trade.orderStatus
        return BrokerOrder(
            broker_order_id=str(trade.order.orderId),
            state=_to_state(status.status if status else ""),
            filled_qty=Decimal(str(status.filled)) if status else Decimal("0"),
            filled_avg_price=Decimal(str(status.avgFillPrice))
            if status and status.avgFillPrice
            else None,
            submitted_at=datetime.now(UTC),
            raw={"perm_id": trade.order.permId},
        )

    async def aclose(self) -> None:
        await self.disconnect()
