"""Unit tests for AlpacaBroker using httpx.MockTransport.

Cassette-replay (vcrpy) tests are developer-local; see
``tests/broker/cassettes/README.md`` for the recording protocol. These tests
hand-build realistic Alpaca response payloads instead.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from src.broker.alpaca import AlpacaBroker, _to_state
from src.broker.base import (
    BracketOrderRequest,
    BrokerError,
    BrokerOrderState,
    OrderRejectedError,
)
from src.db.models import Action

# --------------------- Fixtures ---------------------


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        alpaca_api_key=SecretStr("test-key"),
        alpaca_api_secret=SecretStr("test-secret"),
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_data_url="https://data.alpaca.markets",
        alpaca_stream_url="wss://paper-api.alpaca.markets/stream",
    )


class _Recorder:
    """Captures request bodies and routes responses by (method, path-prefix)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, Any]] = []  # (method, path-suffix-match, handler)

    def add(self, method: str, match: str, response: Any) -> None:
        self._routes.append((method.upper(), match, response))

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            for method, match, resp in self._routes:
                if request.method != method:
                    continue
                # Match by full URL substring (path + query)
                full = str(request.url)
                if match in full:
                    if callable(resp):
                        return resp(request)
                    return resp
            return httpx.Response(404, json={"message": "no route", "url": str(request.url)})

        return httpx.MockTransport(handler)


def _make_broker(rec: _Recorder) -> AlpacaBroker:
    return AlpacaBroker(settings=_settings(), transport=rec.transport())


# --------------------- Tests ---------------------


@pytest.mark.parametrize(
    "alpaca_status,expected",
    [
        ("new", BrokerOrderState.PENDING),
        ("accepted", BrokerOrderState.PENDING),
        ("pending_new", BrokerOrderState.PENDING),
        ("partially_filled", BrokerOrderState.PARTIAL),
        ("filled", BrokerOrderState.FILLED),
        ("canceled", BrokerOrderState.CANCELLED),
        ("rejected", BrokerOrderState.REJECTED),
        ("expired", BrokerOrderState.EXPIRED),
    ],
)
def test_get_order_state_mapping(alpaca_status: str, expected: BrokerOrderState) -> None:
    assert _to_state(alpaca_status) == expected


async def test_get_account() -> None:
    rec = _Recorder()
    rec.add(
        "GET",
        "/v2/account",
        httpx.Response(
            200,
            json={
                "equity": "10500.00",
                "cash": "5000.00",
                "long_market_value": "4000.00",
                "short_market_value": "1500.00",
                "buying_power": "15000.00",
                "pattern_day_trader": True,
            },
        ),
    )
    broker = _make_broker(rec)
    acct = await broker.get_account()
    assert acct.equity == Decimal("10500.00")
    assert acct.cash == Decimal("5000.00")
    assert acct.positions_value == Decimal("5500.00")
    assert acct.buying_power == Decimal("15000.00")
    assert acct.pattern_day_trader is True
    # Headers
    assert rec.requests[0].headers["APCA-API-KEY-ID"] == "test-key"
    assert rec.requests[0].headers["APCA-API-SECRET-KEY"] == "test-secret"
    await broker.disconnect()


def _gtc_bracket_response(parent_id: str = "p1") -> dict[str, Any]:
    return {
        "id": parent_id,
        "client_order_id": "cid-1",
        "status": "accepted",
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2024-01-02T03:04:05.123456Z",
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "limit_price": "150.00",
        "time_in_force": "day",
        "legs": [
            {
                "id": "tp1",
                "symbol": "AAPL",
                "qty": "10",
                "side": "sell",
                "type": "limit",
                "limit_price": "160.00",
                "time_in_force": "gtc",
                "status": "accepted",
            },
            {
                "id": "sl1",
                "symbol": "AAPL",
                "qty": "10",
                "side": "sell",
                "type": "stop",
                "stop_price": "145.00",
                "time_in_force": "gtc",
                "status": "accepted",
            },
        ],
    }


async def test_submit_bracket_happy_path() -> None:
    rec = _Recorder()
    resp = _gtc_bracket_response()
    rec.add("POST", "/v2/orders", httpx.Response(200, json=resp))
    rec.add("GET", "/v2/orders/p1?nested=true", httpx.Response(200, json=resp))
    broker = _make_broker(rec)

    order = await broker.submit_bracket(
        BracketOrderRequest(
            ticker="AAPL",
            side=Action.BUY,
            quantity=Decimal("10"),
            entry_limit=Decimal("150.00"),
            stop=Decimal("145.00"),
            target=Decimal("160.00"),
            client_order_id="cid-1",
        )
    )
    assert order.broker_order_id == "p1"
    assert order.state == BrokerOrderState.PENDING
    # Payload assertion
    post_req = next(r for r in rec.requests if r.method == "POST")
    body = json.loads(post_req.content)
    assert body["symbol"] == "AAPL"
    assert body["qty"] == "10"
    assert body["side"] == "buy"
    assert body["type"] == "limit"
    assert body["limit_price"] == "150.00"
    assert body["order_class"] == "bracket"
    assert body["client_order_id"] == "cid-1"
    assert body["take_profit"] == {"limit_price": "160.00"}
    assert body["stop_loss"] == {"stop_price": "145.00"}
    # No DELETE -> no repair invoked
    assert not any(r.method == "DELETE" for r in rec.requests)
    await broker.disconnect()


async def test_submit_bracket_repairs_non_gtc_stop() -> None:
    rec = _Recorder()
    parent = _gtc_bracket_response()
    parent["legs"][1]["time_in_force"] = "day"  # stop_loss is DAY -> repair
    rec.add("POST", "/v2/orders", httpx.Response(200, json=parent))
    rec.add("GET", "/v2/orders/p1?nested=true", httpx.Response(200, json=parent))

    # repair_child_to_gtc fetches the child
    sl_payload = {
        "id": "sl1",
        "symbol": "AAPL",
        "qty": "10",
        "side": "sell",
        "type": "stop",
        "stop_price": "145.00",
        "time_in_force": "day",
        "status": "accepted",
    }
    rec.add("GET", "/v2/orders/sl1?nested=true", httpx.Response(200, json=sl_payload))
    rec.add("DELETE", "/v2/orders/sl1", httpx.Response(204))

    new_sl = {**sl_payload, "id": "sl2", "time_in_force": "gtc"}
    # Sequential POSTs: first was bracket submit (already routed). Use a closure
    # so a second POST to /v2/orders returns the new SL.
    post_count = {"n": 0}

    def post_handler(request: httpx.Request) -> httpx.Response:
        post_count["n"] += 1
        if post_count["n"] == 1:
            return httpx.Response(200, json=parent)
        return httpx.Response(200, json=new_sl)

    rec._routes = [r for r in rec._routes if not (r[0] == "POST" and r[1] == "/v2/orders")]
    rec.add("POST", "/v2/orders", post_handler)

    broker = _make_broker(rec)
    await broker.submit_bracket(
        BracketOrderRequest(
            ticker="AAPL",
            side=Action.BUY,
            quantity=Decimal("10"),
            entry_limit=Decimal("150.00"),
            stop=Decimal("145.00"),
            target=Decimal("160.00"),
            client_order_id="cid-1",
        )
    )
    # repair path hit: DELETE sl1 + POST a new gtc order
    assert any(r.method == "DELETE" and r.url.path.endswith("/sl1") for r in rec.requests)
    new_posts = [r for r in rec.requests if r.method == "POST"]
    assert len(new_posts) == 2
    new_body = json.loads(new_posts[1].content)
    assert new_body["time_in_force"] == "gtc"
    assert new_body["type"] == "stop"
    await broker.disconnect()


async def test_submit_bracket_idempotent_on_dup_client_order_id() -> None:
    rec = _Recorder()
    rec.add(
        "POST",
        "/v2/orders",
        httpx.Response(
            422,
            json={"code": 40010001, "message": "client_order_id must be unique. Already exists."},
        ),
    )
    existing = _gtc_bracket_response("existing-1")
    rec.add(
        "GET",
        "/v2/orders:by_client_order_id",
        httpx.Response(200, json=existing),
    )
    broker = _make_broker(rec)
    order = await broker.submit_bracket(
        BracketOrderRequest(
            ticker="AAPL",
            side=Action.BUY,
            quantity=Decimal("10"),
            entry_limit=Decimal("150.00"),
            stop=Decimal("145.00"),
            target=Decimal("160.00"),
            client_order_id="cid-existing",
        )
    )
    assert order.broker_order_id == "existing-1"
    await broker.disconnect()


async def test_submit_bracket_422_other_raises_rejected() -> None:
    rec = _Recorder()
    rec.add("POST", "/v2/orders", httpx.Response(422, json={"message": "insufficient buying power"}))
    broker = _make_broker(rec)
    with pytest.raises(OrderRejectedError):
        await broker.submit_bracket(
            BracketOrderRequest(
                ticker="AAPL",
                side=Action.BUY,
                quantity=Decimal("10"),
                entry_limit=Decimal("150.00"),
                stop=Decimal("145.00"),
                target=Decimal("160.00"),
                client_order_id="cid-2",
            )
        )
    await broker.disconnect()


async def test_cancel_order_swallows_404_and_422() -> None:
    rec = _Recorder()
    rec.add("DELETE", "/v2/orders/abc", httpx.Response(404, json={"message": "not found"}))
    rec.add(
        "DELETE",
        "/v2/orders/def",
        httpx.Response(422, json={"message": "order cannot be canceled — already filled"}),
    )
    rec.add("DELETE", "/v2/orders/ghi", httpx.Response(204))
    broker = _make_broker(rec)
    await broker.cancel_order("abc")
    await broker.cancel_order("def")
    await broker.cancel_order("ghi")
    await broker.disconnect()


async def test_get_last_price_midpoint() -> None:
    rec = _Recorder()
    rec.add(
        "GET",
        "/v2/stocks/AAPL/quotes/latest",
        httpx.Response(200, json={"symbol": "AAPL", "quote": {"bp": 150.00, "ap": 150.10}}),
    )
    broker = _make_broker(rec)
    px = await broker.get_last_price("AAPL")
    assert px == Decimal("150.05")
    await broker.disconnect()


async def test_get_last_price_zero_returns_none() -> None:
    rec = _Recorder()
    rec.add(
        "GET",
        "/v2/stocks/FOO/quotes/latest",
        httpx.Response(200, json={"quote": {"bp": 0, "ap": 0}}),
    )
    broker = _make_broker(rec)
    assert await broker.get_last_price("FOO") is None
    await broker.disconnect()


async def test_iter_open_orders_yields_parent_and_children() -> None:
    rec = _Recorder()
    parent = _gtc_bracket_response()
    rec.add(
        "GET",
        "/v2/orders?status=open",
        httpx.Response(200, json=[parent]),
    )
    broker = _make_broker(rec)
    seen = [o async for o in broker.iter_open_orders()]
    assert len(seen) == 3
    assert seen[0].parent_order_id is None
    assert seen[0].order_type == "limit"
    assert seen[1].parent_order_id == "p1"
    assert seen[2].parent_order_id == "p1"
    assert seen[2].tif == "gtc"
    await broker.disconnect()


async def test_iter_positions_short_side_signed_negative() -> None:
    rec = _Recorder()
    rec.add(
        "GET",
        "/v2/positions",
        httpx.Response(
            200,
            json=[
                {
                    "symbol": "TSLA",
                    "qty": "5",
                    "side": "short",
                    "avg_entry_price": "200.00",
                    "market_value": "-1000.00",
                    "unrealized_pl": "20.00",
                },
                {
                    "symbol": "AAPL",
                    "qty": "10",
                    "side": "long",
                    "avg_entry_price": "150.00",
                    "market_value": "1500.00",
                    "unrealized_pl": "-5.00",
                },
            ],
        ),
    )
    broker = _make_broker(rec)
    positions = [p async for p in broker.iter_positions()]
    assert positions[0].ticker == "TSLA"
    assert positions[0].quantity == Decimal("-5")
    assert positions[1].quantity == Decimal("10")
    await broker.disconnect()


async def test_repair_child_to_gtc_noops_if_already_gtc() -> None:
    rec = _Recorder()
    rec.add(
        "GET",
        "/v2/orders/sl1?nested=true",
        httpx.Response(
            200,
            json={
                "id": "sl1",
                "symbol": "AAPL",
                "qty": "10",
                "side": "sell",
                "type": "stop",
                "stop_price": "145.00",
                "time_in_force": "gtc",
            },
        ),
    )
    broker = _make_broker(rec)
    new_id = await broker.repair_child_to_gtc("sl1")
    assert new_id == "sl1"
    # No DELETE/POST occurred
    assert not any(r.method == "DELETE" for r in rec.requests)
    assert not any(r.method == "POST" for r in rec.requests)
    await broker.disconnect()


# --------------------- WebSocket trade-updates ---------------------


class _FakeWS:
    """Minimal async context manager + iterator stand-in for websockets."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self._sent: list[str] = []

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def send(self, msg: str) -> None:
        self._sent.append(msg)

    async def recv(self) -> str:
        # Yield the auth-ack response first
        return json.dumps({"stream": "authorization", "data": {"status": "authorized"}})

    def __aiter__(self) -> _FakeWS:
        self._iter = iter(self._messages)
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._iter)
        except StopIteration as e:
            raise StopAsyncIteration from e


async def test_subscribe_trade_updates_yields_fill() -> None:
    fill_msg = json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": "fill",
                "timestamp": "2024-01-02T03:04:05.123456Z",
                "position_qty": "10",
                "order": {
                    "id": "p1",
                    "client_order_id": "cid-1",
                    "symbol": "AAPL",
                    "filled_qty": "10",
                    "filled_avg_price": "150.05",
                },
            },
        }
    )

    fake = _FakeWS([fill_msg])

    def factory(url: str) -> _FakeWS:
        return fake

    rec = _Recorder()
    broker = AlpacaBroker(
        settings=_settings(), transport=rec.transport(), ws_factory=factory
    )

    gen = broker.subscribe_trade_updates()
    update = await gen.__anext__()
    assert update.event == "fill"
    assert update.broker_order_id == "p1"
    assert update.ticker == "AAPL"
    assert update.filled_qty == Decimal("10")
    assert update.filled_avg_price == Decimal("150.05")
    assert update.client_order_id == "cid-1"
    await gen.aclose()
    await broker.disconnect()


async def test_subscribe_trade_updates_auth_failure_raises() -> None:
    class _AuthFailWS(_FakeWS):
        async def recv(self) -> str:
            return json.dumps({"stream": "authorization", "data": {"status": "unauthorized"}})

    fake = _AuthFailWS([])

    def factory(url: str) -> _AuthFailWS:
        return fake

    rec = _Recorder()
    broker = AlpacaBroker(
        settings=_settings(), transport=rec.transport(), ws_factory=factory
    )
    gen = broker.subscribe_trade_updates()
    with pytest.raises(BrokerError):
        await gen.__anext__()
    await broker.disconnect()
