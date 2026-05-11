"""Alpaca paper broker adapter — raw httpx + websockets.

Implements the v0.4 ``BrokerAdapter`` contract for Alpaca's paper trading API.
Live Alpaca is refused at boot in ``src.config`` — this adapter only ever talks
to ``paper-api.alpaca.markets`` and ``data.alpaca.markets``.

References:
  - https://docs.alpaca.markets/reference/  (REST API)
  - https://docs.alpaca.markets/docs/streaming-real-time-data  (WS)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import websockets
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from src.broker.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerOrder,
    BrokerOrderState,
    BrokerPosition,
    OpenOrder,
    OrderRejectedError,
    TradeUpdate,
)
from src.config import settings as default_settings
from src.db.models import Action
from src.logging_config import get_logger

logger = get_logger(__name__)


_PRICE_Q = Decimal("0.01")
_QTY_Q = Decimal("1")


def _to_state(status: str) -> BrokerOrderState:
    s = (status or "").lower()
    if s in ("new", "accepted", "pending_new", "pending_cancel", "accepted_for_bidding"):
        return BrokerOrderState.PENDING
    if s == "partially_filled":
        return BrokerOrderState.PARTIAL
    if s == "filled":
        return BrokerOrderState.FILLED
    if s in ("canceled", "cancelled", "pending_cancel"):
        return BrokerOrderState.CANCELLED
    if s == "rejected":
        return BrokerOrderState.REJECTED
    if s == "expired":
        return BrokerOrderState.EXPIRED
    return BrokerOrderState.PENDING


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        # Alpaca returns RFC3339 like '2024-01-02T03:04:05.123456Z'
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _q_price(d: Decimal) -> str:
    return str(Decimal(d).quantize(_PRICE_Q))


def _q_qty(d: Decimal) -> str:
    return str(Decimal(d).quantize(_QTY_Q))


def _retryable_status(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        sc = exc.response.status_code
        return sc == 429 or 500 <= sc < 600
    return isinstance(exc, httpx.TransportError)


class AlpacaBroker(BrokerAdapter):
    """Alpaca paper-trading adapter."""

    def __init__(
        self,
        settings: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_factory: Any = None,
    ) -> None:
        # Use module-level settings by default; tests inject a SimpleNamespace.
        self._settings = settings if settings is not None else default_settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._ws_factory = ws_factory  # for test injection

    # ---- BrokerAdapter abstract ----

    @property
    def name(self) -> str:
        return "alpaca_paper"

    @property
    def is_paper(self) -> bool:
        return True

    async def connect(self) -> None:
        if self._client is not None:
            return
        key = self._settings.alpaca_api_key
        secret = self._settings.alpaca_api_secret
        if key is None or secret is None:
            raise BrokerError("ALPACA_API_KEY / ALPACA_API_SECRET are not configured.")
        headers = {
            "APCA-API-KEY-ID": key.get_secret_value(),
            "APCA-API-SECRET-KEY": secret.get_secret_value(),
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)
        kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        return self._client

    # ---- low-level request helper ----

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        client = await self._ensure()

        async def _do() -> httpx.Response:
            resp = await client.request(method, url, json=json_body, params=params)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                # raise to trigger retry
                resp.raise_for_status()
            return resp

        try:
            async for attempt in AsyncRetrying(
                wait=wait_random_exponential(multiplier=0.5, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
                reraise=True,
            ):
                with attempt:
                    resp = await _do()
            return resp
        except httpx.HTTPStatusError as e:
            return e.response
        except RetryError as e:  # pragma: no cover - reraise=True so unlikely
            raise BrokerError(f"Alpaca request failed: {e}") from e

    def _map_error(self, resp: httpx.Response) -> None:
        """Raise an appropriate BrokerError-family exception for a non-2xx response."""
        body = resp.text
        if resp.status_code == 422:
            raise OrderRejectedError(body)
        if 400 <= resp.status_code < 500:
            raise BrokerError(f"{resp.status_code} {body}")
        # 5xx after retries
        raise BrokerError(f"{resp.status_code} {body}")

    # ---- Endpoints ----

    @property
    def _base(self) -> str:
        return self._settings.alpaca_base_url.rstrip("/")

    @property
    def _data(self) -> str:
        return self._settings.alpaca_data_url.rstrip("/")

    async def healthcheck(self) -> bool:
        try:
            resp = await self._request("GET", f"{self._base}/v2/account")
            return resp.status_code == 200
        except Exception:
            return False

    async def get_account(self) -> AccountSnapshot:
        resp = await self._request("GET", f"{self._base}/v2/account")
        if resp.status_code != 200:
            self._map_error(resp)
        d = resp.json()
        long_mv = Decimal(d.get("long_market_value") or "0")
        short_mv = Decimal(d.get("short_market_value") or "0")
        return AccountSnapshot(
            equity=Decimal(d.get("equity") or "0"),
            cash=Decimal(d.get("cash") or "0"),
            positions_value=long_mv + short_mv,
            buying_power=Decimal(d.get("buying_power") or "0"),
            pattern_day_trader=bool(d.get("pattern_day_trader", False)),
        )

    async def submit_bracket(self, spec: Any) -> BrokerOrder:
        ticker = spec.ticker.upper()
        side = "buy" if spec.side == Action.BUY else "sell"
        client_order_id = getattr(spec, "client_order_id", None) or uuid.uuid4().hex
        payload = {
            "symbol": ticker,
            "qty": _q_qty(spec.quantity),
            "side": side,
            "type": "limit",
            "limit_price": _q_price(spec.entry_limit),
            "time_in_force": getattr(spec, "time_in_force", "day"),
            "order_class": "bracket",
            "client_order_id": client_order_id,
            "take_profit": {"limit_price": _q_price(spec.target)},
            "stop_loss": {"stop_price": _q_price(spec.stop)},
        }
        logger.info(
            "alpaca_submit_bracket",
            ticker=ticker,
            side=side,
            qty=payload["qty"],
            client_order_id=client_order_id,
        )
        resp = await self._request("POST", f"{self._base}/v2/orders", json_body=payload)

        if resp.status_code == 422:
            body = resp.text
            if "already exists" in body.lower() or "40010001" in body:
                logger.info(
                    "alpaca_submit_bracket_dup_client_order_id",
                    client_order_id=client_order_id,
                )
                # fetch the existing order
                existing = await self._request(
                    "GET",
                    f"{self._base}/v2/orders:by_client_order_id",
                    params={"client_order_id": client_order_id},
                )
                if existing.status_code != 200:
                    self._map_error(existing)
                return self._order_from_json(existing.json())
            raise OrderRejectedError(body)
        if resp.status_code >= 400:
            self._map_error(resp)

        parent_id = resp.json().get("id")
        # Re-fetch nested to inspect children TIF.
        nested = await self._request(
            "GET", f"{self._base}/v2/orders/{parent_id}", params={"nested": "true"}
        )
        if nested.status_code != 200:
            self._map_error(nested)
        parent = nested.json()

        for child in parent.get("legs") or []:
            child_tif = (child.get("time_in_force") or "").lower()
            if child_tif != "gtc":
                logger.warning(
                    "alpaca_child_repair_at_submit",
                    parent_order_id=parent_id,
                    child_order_id=child.get("id"),
                    original_tif=child_tif,
                )
                await self.repair_child_to_gtc(child["id"])

        return self._order_from_json(parent)

    async def cancel_order(self, broker_order_id: str) -> None:
        resp = await self._request("DELETE", f"{self._base}/v2/orders/{broker_order_id}")
        if resp.status_code in (204, 207, 200, 404):
            return
        if resp.status_code == 422:
            body = resp.text.lower()
            if "cannot be canceled" in body or "already" in body:
                return
            raise OrderRejectedError(resp.text)
        self._map_error(resp)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        resp = await self._request(
            "GET",
            f"{self._base}/v2/orders/{broker_order_id}",
            params={"nested": "true"},
        )
        if resp.status_code != 200:
            self._map_error(resp)
        return self._order_from_json(resp.json())

    async def get_last_price(self, ticker: str) -> Decimal | None:
        resp = await self._request(
            "GET", f"{self._data}/v2/stocks/{ticker.upper()}/quotes/latest"
        )
        if resp.status_code != 200:
            return None
        d = resp.json()
        quote = d.get("quote") or {}
        bp = quote.get("bp")
        ap = quote.get("ap")
        try:
            bid = Decimal(str(bp)) if bp is not None else Decimal("0")
            ask = Decimal(str(ap)) if ap is not None else Decimal("0")
        except (ValueError, ArithmeticError):
            return None
        if bid == 0 and ask == 0:
            return None
        if bid == 0:
            return ask
        if ask == 0:
            return bid
        return (bid + ask) / Decimal("2")

    async def iter_open_orders(self) -> AsyncIterator[OpenOrder]:
        resp = await self._request(
            "GET",
            f"{self._base}/v2/orders",
            params={"status": "open", "nested": "true", "limit": 500},
        )
        if resp.status_code != 200:
            self._map_error(resp)
        for parent in resp.json():
            yield self._to_open_order(parent, parent_id=None)
            for child in parent.get("legs") or []:
                yield self._to_open_order(child, parent_id=parent.get("id"))

    async def iter_positions(self) -> AsyncIterator[BrokerPosition]:
        resp = await self._request("GET", f"{self._base}/v2/positions")
        if resp.status_code != 200:
            self._map_error(resp)
        for p in resp.json():
            qty = Decimal(p.get("qty") or "0")
            if (p.get("side") or "").lower() == "short":
                qty = -abs(qty)
            yield BrokerPosition(
                ticker=p.get("symbol") or "",
                quantity=qty,
                avg_entry_price=Decimal(p.get("avg_entry_price") or "0"),
                market_value=Decimal(p["market_value"]) if p.get("market_value") else None,
                unrealized_pnl=(
                    Decimal(p["unrealized_pl"]) if p.get("unrealized_pl") is not None else None
                ),
                raw=p,
            )

    async def repair_child_to_gtc(self, broker_order_id: str) -> str:
        resp = await self._request(
            "GET",
            f"{self._base}/v2/orders/{broker_order_id}",
            params={"nested": "true"},
        )
        if resp.status_code != 200:
            self._map_error(resp)
        order = resp.json()
        original_tif = (order.get("time_in_force") or "").lower()
        if original_tif == "gtc":
            return broker_order_id

        # Cancel then resubmit with GTC.
        cancel_resp = await self._request(
            "DELETE", f"{self._base}/v2/orders/{broker_order_id}"
        )
        if cancel_resp.status_code not in (204, 207, 200, 404, 422):
            self._map_error(cancel_resp)

        side = (order.get("side") or "buy").lower()
        order_type = (order.get("type") or "stop").lower()
        symbol = order.get("symbol") or ""
        qty = order.get("qty") or "0"
        payload: dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": "gtc",
        }
        if order.get("stop_price") is not None:
            payload["stop_price"] = str(order["stop_price"])
        if order.get("limit_price") is not None:
            payload["limit_price"] = str(order["limit_price"])

        new_resp = await self._request("POST", f"{self._base}/v2/orders", json_body=payload)
        if new_resp.status_code >= 400:
            self._map_error(new_resp)
        new_id = new_resp.json().get("id")
        logger.info(
            "alpaca_repaired_to_gtc",
            old_order_id=broker_order_id,
            new_order_id=new_id,
            original_tif=original_tif,
        )
        return str(new_id)

    # ---- Trade-updates WebSocket ----

    async def subscribe_trade_updates(self) -> AsyncIterator[TradeUpdate]:  # type: ignore[override]
        backoff = 1.0
        key_obj = self._settings.alpaca_api_key
        secret_obj = self._settings.alpaca_api_secret
        if key_obj is None or secret_obj is None:
            raise BrokerError("ALPACA_API_KEY / ALPACA_API_SECRET are not configured.")
        key = key_obj.get_secret_value()
        secret = secret_obj.get_secret_value()
        url = self._settings.alpaca_stream_url

        while True:
            try:
                connect = (
                    self._ws_factory(url) if self._ws_factory else websockets.connect(url)
                )
                async with connect as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "action": "authenticate",
                                "data": {"key_id": key, "secret_key": secret},
                            }
                        )
                    )
                    auth_raw = await ws.recv()
                    auth = json.loads(auth_raw)
                    auth_data = auth.get("data") or {}
                    if auth_data.get("status") not in ("authorized", "ok", None):
                        raise BrokerError(f"Alpaca WS auth failed: {auth_raw!r}")
                    await ws.send(
                        json.dumps(
                            {"action": "listen", "data": {"streams": ["trade_updates"]}}
                        )
                    )
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("stream") != "trade_updates":
                            continue
                        data = msg.get("data") or {}
                        order = data.get("order") or {}
                        filled_qty_s = data.get("position_qty") or order.get("filled_qty") or "0"
                        avg_s = order.get("filled_avg_price")
                        yield TradeUpdate(
                            event=data.get("event") or "",
                            broker_order_id=str(order.get("id") or ""),
                            client_order_id=order.get("client_order_id"),
                            ticker=order.get("symbol") or "",
                            filled_qty=Decimal(str(filled_qty_s or "0")),
                            filled_avg_price=Decimal(str(avg_s)) if avg_s else None,
                            timestamp=_parse_dt(data.get("timestamp")),
                            raw=data,
                        )
            except asyncio.CancelledError:
                # Cooperative shutdown — caller cancelled the generator.
                raise
            except BrokerError:
                raise
            except Exception as e:  # pragma: no cover - reconnect path
                logger.warning("alpaca_ws_disconnect", error=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

    # ---- Helpers ----

    def _order_from_json(self, d: dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(d.get("id") or ""),
            state=_to_state(d.get("status") or ""),
            filled_qty=Decimal(str(d.get("filled_qty") or "0")),
            filled_avg_price=(
                Decimal(str(d["filled_avg_price"])) if d.get("filled_avg_price") else None
            ),
            submitted_at=_parse_dt(d.get("submitted_at") or d.get("created_at")),
            raw=d,
        )

    def _to_open_order(self, d: dict[str, Any], parent_id: str | None) -> OpenOrder:
        side = Action.BUY if (d.get("side") or "").lower() == "buy" else Action.SELL
        limit_price = (
            Decimal(str(d["limit_price"])) if d.get("limit_price") is not None else None
        )
        stop_price = (
            Decimal(str(d["stop_price"])) if d.get("stop_price") is not None else None
        )
        return OpenOrder(
            broker_order_id=str(d.get("id") or ""),
            parent_order_id=parent_id,
            ticker=d.get("symbol") or "",
            side=side,
            order_type=(d.get("type") or "").lower(),
            quantity=Decimal(str(d.get("qty") or "0")),
            limit_price=limit_price,
            stop_price=stop_price,
            tif=(d.get("time_in_force") or "").lower(),
            raw=d,
        )
