# Bedcrock v0.4 — Alpaca Broker Support (Multi-Agent Plan)

Add Alpaca as a second `BrokerAdapter` so paper trading can run without IB Gateway. IBKR remains the only live path (user is in Canada — Alpaca live brokerage is US-only anyway). Reasoning, scoring, ingestors, and the Discord/FastAPI surfaces are unchanged.

**Total estimated wall time:** ~30–40 min with parallel execution (matches v2 / v0.3.0 cadence).
**Total estimated agent-time:** ~3–4 agent-hours.
**Orchestrator:** the main Claude Code session. Agents run in `.claude/worktrees/` worktrees.

---

## 1. Goal & success criteria

**Goal:** `BROKER=alpaca MODE=paper` runs a full ingest → score → confirm → bracket → fill → close loop against Alpaca paper, with the same code paths that `BROKER=ibkr MODE=paper` uses today.

**Done means:**

- [ ] `pytest tests/` green, including new VCR-cassette tests for the Alpaca adapter.
- [ ] `python -m src.workers.healthcheck` succeeds against Alpaca paper credentials.
- [ ] `make_broker()` dispatches on `settings.broker` and returns either `IBKRBroker` or `AlpacaBroker`.
- [ ] `BROKER=alpaca MODE=live` refuses to boot with a clear error.
- [ ] `BROKER=alpaca` skips the IBKR `MODE↔port` validator.
- [ ] One end-to-end paper trade flows on Alpaca: signal → scored → `/confirm` → bracket with **stop legs as GTC** (verifiable via Alpaca dashboard) → WebSocket fill event → Position row + Discord embed.
- [ ] No code path emits a child stop with `tif != "GTC"` on either adapter. The reconciler audits and repairs both.
- [ ] `docs/BROKER_SETUP.md` has a complete Alpaca section. `docs/ENV.md`, `.env.example`, `README.md`, `CHANGELOG.md`, and the vault overview are updated.
- [ ] Discord embeds carry an `[alpaca-paper]` or `[ibkr-paper]`/`[ibkr-live]` prefix so the channel context is obvious without splitting channels.

**Explicit non-goals:**

- Alpaca live brokerage (US-only; user is in Canada).
- Migration tooling between brokers — paper accounts are disposable, no cross-broker state moves.
- Alpaca crypto / options — equities only, matching the current scope.
- Replacing Polygon/yfinance for OHLCV history. Alpaca quote API is only used by `get_last_price()` (cheap, broker-internal). Bar history stays on Polygon→yfinance.

---

## 2. Wave structure

```
Wave A (Foundation, 1 sequential agent)
  ├── Config: BROKER enum, ALPACA_* env vars, MODE↔port validator carve-out
  ├── BrokerAdapter contract extension (iter_open_orders, iter_positions,
  │   repair_child_to_gtc, subscribe_trade_updates)
  └── Factory dispatch in src/broker/__init__.py

Wave B (2 parallel agents, each in own worktree)
  ├── B1: src/broker/alpaca.py — adapter impl over raw httpx + websockets
  └── B2: VCR cassettes + unit tests for AlpacaBroker

Wave C (2 parallel agents, each in own worktree)
  ├── C1: Refactor src/safety/reconciler.py + src/orders/monitor.py to use
  │       the abstract BrokerAdapter contract (drop concrete IBKRBroker imports)
  └── C2: Trade-updates WebSocket plumbing in monitor_worker; matching
          ib_async event bridge so IBKRBroker also feeds subscribe_trade_updates()

Wave D (1 sequential agent)
  └── Docs: BROKER_SETUP.md (Alpaca section), ENV.md, .env.example,
      README.md, CHANGELOG.md, vault overview & context, audit-log row format.
```

**Why this shape:**

- Wave A pre-stages the **adapter contract extension**. Without it, Wave B (Alpaca impl) and Wave C (consumer refactor) would each invent their own view of "open order" / "trade update" and merge would be a mess.
- Wave B's two agents touch disjoint files (`src/broker/alpaca.py` vs `tests/broker/`), so they can't collide.
- Wave C must wait for Wave B *only* to keep the broker contract single-authored; the consumer refactor itself is mechanical.
- Wave D is one author for voice consistency.

**Branches:** `v4/A/foundation` → `v4-staging`; then `v4/B/{alpaca-adapter,alpaca-tests}` and `v4/C/{consumer-refactor,trade-updates-ws}` and `v4/D/docs` off `v4-staging`. Final squash-merge into `main` and tag `v0.4.0`.

---

## 3. Truth table (the new boot-time invariant)

| `BROKER` | `MODE` | Boot behavior |
|---|---|---|
| `ibkr` (default) | `paper` | OK if `IBKR_PORT ∈ {4002, 7497}` else refuse. |
| `ibkr` | `live` | OK if `IBKR_PORT ∈ {4001, 7496}` else refuse. |
| `alpaca` | `paper` | OK if `ALPACA_API_KEY` and `ALPACA_API_SECRET` set; `IBKR_*` ignored. |
| `alpaca` | `live` | **Refuse.** Message: "Alpaca live brokerage is US-only; use BROKER=ibkr for live in Canada." |

`Settings._validate_mode_port()` becomes `_validate_broker_mode()` and dispatches per broker.

---

## 4. The adapter contract (Wave A core deliverable)

`src/broker/base.py` already has `BrokerAdapter`. Wave A extends it with the methods the consumer code currently reaches into `_ib` for. After Wave A, **no module outside `src/broker/` may import `IBKRBroker` or `_ib` directly.**

### New types

```python
@dataclass
class OpenOrder:
    broker_order_id: str
    parent_order_id: str | None      # None means it's a parent / standalone
    ticker: str
    side: Action
    order_type: str                  # "limit" | "stop" | "stop_limit" | "trailing_stop"
    quantity: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str                         # "day" | "gtc" | "ioc" | "fok" | "opg" | "cls"
    raw: dict

@dataclass
class BrokerPosition:
    ticker: str
    quantity: Decimal                # signed; negative for short
    avg_entry_price: Decimal
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    raw: dict

@dataclass
class TradeUpdate:
    """One push event from the broker about an order state change."""
    event: str                       # "new" | "fill" | "partial_fill" | "canceled" | "rejected" | ...
    broker_order_id: str
    client_order_id: str | None
    ticker: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    timestamp: datetime
    raw: dict
```

### New methods on `BrokerAdapter`

```python
async def iter_open_orders(self) -> AsyncIterator[OpenOrder]: ...
async def iter_positions(self) -> AsyncIterator[BrokerPosition]: ...
async def repair_child_to_gtc(self, broker_order_id: str) -> str:
    """Re-issue the given child order as GTC. Returns the new broker_order_id."""
async def subscribe_trade_updates(self) -> AsyncIterator[TradeUpdate]:
    """Yields trade updates forever until the underlying stream closes.
    IBKR: bridge ib_async events into an asyncio.Queue.
    Alpaca: connect to wss://paper-api.alpaca.markets/stream, listen=['trade_updates']."""
```

These are the only new contract surfaces; existing methods (`submit_bracket`, `get_account`, `cancel_order`, `get_order`, `get_last_price`) stay.

---

## 5. Wave A brief (agent: foundation)

**Branch:** `v4/A/foundation` off `main`.
**Files touched:**

- `src/config.py` — add `Broker` enum (`IBKR`, `ALPACA`), `broker: Broker = Broker.IBKR`, `alpaca_api_key: SecretStr | None`, `alpaca_api_secret: SecretStr | None`, `alpaca_base_url: str = "https://paper-api.alpaca.markets"`, `alpaca_data_url: str = "https://data.alpaca.markets"`. Rewrite the mode validator to dispatch per broker per §3.
- `src/broker/base.py` — add `OpenOrder`, `BrokerPosition`, `TradeUpdate` dataclasses and the four new abstract methods. Provide a default `subscribe_trade_updates` that raises `NotImplementedError` so the IBKR shim in Wave C can override.
- `src/broker/ibkr.py` — implement `iter_open_orders` (walk `ib.openTrades()`), `iter_positions` (walk `ib.positions()`), `repair_child_to_gtc` (cancel + re-submit with `tif="GTC"`). Leave `subscribe_trade_updates` as a `NotImplementedError` stub; Wave C/C2 fills it in.
- `src/broker/__init__.py` — factory:

```python
def make_broker() -> BrokerAdapter:
    if settings.broker is Broker.ALPACA:
        from src.broker.alpaca import AlpacaBroker
        return AlpacaBroker()
    from src.broker.ibkr import IBKRBroker
    return IBKRBroker()
```

**Acceptance:**

- Existing tests still pass.
- `pytest tests/test_config.py` (new) covers the 4×2 truth-table, including the Alpaca-live refusal.
- `IBKRBroker.iter_open_orders` returns the same data the reconciler currently inspects, just packaged as `OpenOrder`.

**Out of scope for Wave A:** writing the Alpaca adapter, refactoring the reconciler, the WebSocket stream.

---

## 6. Wave B briefs

### B1 — `src/broker/alpaca.py` adapter (raw httpx + websockets)

**Branch:** `v4/B/alpaca-adapter` off `v4-staging`.
**Library choice:** raw `httpx` for REST, `websockets` (already a dep) for the stream. No `alpaca-py`.

**Endpoints used:**

| Purpose | Method + path | Base URL |
|---|---|---|
| Account snapshot | `GET /v2/account` | `paper-api.alpaca.markets` |
| Submit bracket | `POST /v2/orders` with `order_class=bracket` | `paper-api.alpaca.markets` |
| Cancel order | `DELETE /v2/orders/{id}` | `paper-api.alpaca.markets` |
| Get order | `GET /v2/orders/{id}` | `paper-api.alpaca.markets` |
| Open orders | `GET /v2/orders?status=open&nested=true` | `paper-api.alpaca.markets` |
| Positions | `GET /v2/positions` | `paper-api.alpaca.markets` |
| Last quote | `GET /v2/stocks/{symbol}/quotes/latest` | `data.alpaca.markets` |
| Trade updates | `wss://paper-api.alpaca.markets/stream` (`listen: ["trade_updates"]`) | — |

**Headers:** `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY` on every REST call. WebSocket auth uses an `authenticate` action with the same key/secret in the JSON payload.

**Bracket payload shape:**

```json
{
  "symbol": "AAPL",
  "qty": "10",
  "side": "buy",
  "type": "limit",
  "limit_price": "150.00",
  "time_in_force": "day",
  "order_class": "bracket",
  "client_order_id": "<idempotency-key>",
  "take_profit": {"limit_price": "160.00"},
  "stop_loss":   {"stop_price":  "145.00"}
}
```

**GTC invariant:** Alpaca's bracket child legs default to inheriting the parent's TIF. After the adapter submits, it MUST verify the returned child legs have `time_in_force == "gtc"`. If not, immediately call `repair_child_to_gtc()` and log the repair. This is the equivalent of IBKR's reconciler safety net and is invariant 6 of the project.

**Idempotency:** every `submit_bracket` call passes `client_order_id`. The adapter treats a 422 with `code=40010001` ("client_order_id already exists") as success and fetches the existing order.

**Decimal formatting:** Alpaca expects strings for prices and qty (matches their docs). Use `str(decimal)` not `format()`.

**Error mapping:** `422` → `OrderRejectedError`; other 4xx → `BrokerError` with body; 5xx → retry via `tenacity` (already a dep) with exponential backoff capped at 30s.

**Backoff & rate limits:** Alpaca paper allows 200 req/min per account. Use `tenacity.AsyncRetrying` with `wait_random_exponential(multiplier=0.5, max=10)`, 3 attempts, and treat `429` as retriable.

**Logging:** mirror `IBKRBroker` — `structlog` events `alpaca_submit_bracket`, `alpaca_order_filled`, etc. Never log API key/secret; use `SecretStr.get_secret_value()` only at the call site.

**Out of scope for B1:** wiring `subscribe_trade_updates` into the monitor worker (that's C2's job — B1 only needs the method to yield correctly so B2's tests can assert on it).

### B2 — VCR cassettes + unit tests

**Branch:** `v4/B/alpaca-tests` off `v4-staging`.
**Files:**

- `tests/broker/conftest.py` — fixture that creates an `AlpacaBroker` pointing at a recorded cassette directory; redacts `APCA-API-*` headers.
- `tests/broker/test_alpaca_adapter.py` — covers: account snapshot, bracket submit (happy path), bracket submit with stop returning non-GTC (forces `repair_child_to_gtc`), idempotent re-submit (422 → fetch), cancel, get_order, get_last_price (data API), iter_open_orders, iter_positions, trade_updates (1 fill event from a recorded WS stream).
- `tests/broker/cassettes/alpaca_*.yaml` — recorded against a real Alpaca paper account. The CI mode runs only the cassette-replay tests; recording is a developer-local activity (`vcrpy` record_mode=`once`).

**Recording protocol:** B2 must include a one-paragraph README in `tests/broker/cassettes/` explaining how to re-record (env vars to set, command to run). This keeps tests reproducible across machines.

---

## 7. Wave C briefs

### C1 — Consumer refactor (drop concrete IBKR imports)

**Branch:** `v4/C/consumer-refactor` off `v4-staging`.
**Files touched:**

- `src/safety/reconciler.py` — `audit_open_order_tifs(broker: BrokerAdapter)` walks `broker.iter_open_orders()` and calls `broker.repair_child_to_gtc()`. `reconcile_against_broker` uses `broker.iter_positions()`. No more `broker._ib`.
- `src/orders/monitor.py` — drop the `from src.broker.ibkr import IBKRBroker` line; type the field as `BrokerAdapter`. The IBKR-specific event loop in `start()` moves behind `broker.subscribe_trade_updates()` (C2 fills the IBKR side).
- `src/workers/monitor_worker.py` — same: replace concrete `IBKRBroker` with `BrokerAdapter`.

**Acceptance:**

- `grep -r "from src.broker.ibkr import" src/` returns matches only inside `src/broker/`.
- `grep -r "broker\._ib" src/` returns zero matches outside `src/broker/ibkr.py`.
- All existing reconciler tests still pass against `IBKRBroker`; new ones run the same suite against `AlpacaBroker` cassettes.

### C2 — Trade-updates WebSocket plumbing

**Branch:** `v4/C/trade-updates-ws` off `v4-staging`.
**Files touched:**

- `src/broker/ibkr.py` — implement `subscribe_trade_updates` as a generator that bridges `ib.execDetailsEvent` + `ib.orderStatusEvent` into an `asyncio.Queue` and yields normalized `TradeUpdate` events.
- `src/broker/alpaca.py` — implement `subscribe_trade_updates`: open the WS, authenticate, subscribe to `trade_updates`, decode each message into `TradeUpdate`, yield. Auto-reconnect with exponential backoff on disconnect.
- `src/orders/monitor.py` — the main loop becomes:

```python
async for update in self._broker.subscribe_trade_updates():
    await self._handle_update(update, db)
```

`_handle_update` is the broker-agnostic version of today's `_on_order_status` callback.

- Polling fallback (every 30s) stays — uses `broker.iter_open_orders()`, also broker-agnostic.

**Acceptance:**

- IBKR paper run: one bracket submit → entry fill event arrives via `subscribe_trade_updates`, not via the legacy callback. Position row created.
- Alpaca paper run: one bracket submit → same. The recorded WS cassette in B2 exercises this path in tests.
- Reconnect: kill the WS mid-test; the generator reconnects within 5s and resumes emitting events. (Covered by a unit test that closes the underlying transport mock.)

---

## 8. Wave D brief (docs)

**Branch:** `v4/D/docs` off `v4-staging`.
**Files touched:**

- `docs/BROKER_SETUP.md` — add a top-level "Choose your broker" section. Then an **Alpaca** section: how to sign up, generate paper keys, set `BROKER=alpaca` + `ALPACA_API_KEY` + `ALPACA_API_SECRET`, why no port is needed, why live is unavailable. Keep the existing IBKR section.
- `docs/ENV.md` — document the new vars; mark IBKR vars as "ignored when BROKER=alpaca". Update the validator-error reference.
- `.env.example` — add the Alpaca block. Default `BROKER=ibkr` to preserve current behavior on `cp .env.example .env`.
- `README.md` — short paragraph under "Quick start" that `BROKER=alpaca` is the easiest path for paper testing.
- `CHANGELOG.md` — v0.4.0 entry: "Add Alpaca paper broker via generic `BrokerAdapter`. IBKR remains the only live path."
- `docs/AUDIT.md` — append the per-component review notes for the Alpaca adapter and consumer refactor.
- Vault overview (`<vault-repo>/<vaultDir>/projects/bedcrock/bedcrock.md`) — add Alpaca to the stack table; update Key Patterns to call out broker-agnostic monitor loop; update Gotchas with the Alpaca live-refusal rule.
- Vault `context.md` — update Status to v0.4 in progress / shipped; record the decision.

**Acceptance:**

- Cold-read by an outsider can pick `BROKER=alpaca`, sign up, paste two keys, and reach a green `python -m src.workers.healthcheck` in under 10 minutes.
- All cross-references between docs resolve (no broken links).

---

## 9. Merge protocol

1. **Wave A** lands first on `main` via squash-merge (only foundation; small enough to inspect line-by-line).
2. Orchestrator creates `v4-staging` from new `main`.
3. **Wave B agents** open PRs into `v4-staging`. Orchestrator merges as they complete, rebasing `v4-staging` forward.
4. **Wave C agents** open PRs into `v4-staging` after both Wave B branches are in. Same merge cadence.
5. **Wave D** opens its PR last.
6. Once `v4-staging` is green (`pytest`, `ruff`, `mypy`, manual end-to-end on Alpaca paper), squash-merge into `main` and tag `v0.4.0`.

**Rollback plan:** if `v4-staging` goes sideways, `v4-staging` is the only branch to discard; `main` stays at v0.3.x. Worktrees remain on disk for forensic inspection until the orchestrator deletes them.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Alpaca bracket child stops are not GTC by default. | Adapter verifies on every submit and calls `repair_child_to_gtc` if needed. Reconciler audit-loop catches drift in production. Invariant 6 is preserved. |
| WebSocket stream drops silently. | `subscribe_trade_updates` is a self-reconnecting generator with backoff. Polling fallback in the monitor still runs every 30s. |
| User accidentally points Alpaca paper keys at a fresh live key. | The base URL is pinned to `paper-api.alpaca.markets`; `BROKER=alpaca` + `MODE=live` refuses to boot. No code path uses `api.alpaca.markets`. |
| Cassette tests rot when Alpaca evolves the response shape. | `tests/broker/cassettes/README.md` documents the re-record command; CI keeps replay-only mode so production keys never leak. |
| Two-broker world doubles the audit-log surface. | `audit_log.event` already includes a `broker` field via `settings.broker.value`. C1 ensures every reconciler audit row carries this label. |
| Existing v0.3 IBKR users see a behavior change after upgrading. | `BROKER` defaults to `ibkr`; the `MODE↔port` validator is unchanged for that branch. No env-var diff is required on upgrade. Documented in `CHANGELOG.md`. |

---

## 11. Out of scope (explicit non-goals reminder)

- Alpaca live brokerage.
- Alpaca crypto/options.
- Replacing Polygon/yfinance for OHLCV history.
- Cross-broker position migration tooling.
- A `BROKER=both` mode (the system trades on one broker at a time; running two requires two deployments with different `MODE`-tag columns).

---

## 12. Kickoff checklist for the orchestrator

- [ ] Confirm `.env` has Alpaca paper keys (or the orchestrator records cassettes once and commits them).
- [ ] Create branch `v4/A/foundation` and dispatch the Wave A agent.
- [ ] After Wave A merges, create `v4-staging`.
- [ ] Dispatch Wave B agents in parallel (B1 + B2) on separate worktrees.
- [ ] Dispatch Wave C agents in parallel (C1 + C2) after Wave B merges.
- [ ] Dispatch Wave D agent last.
- [ ] Tag `v0.4.0` after `v4-staging` merges to `main`.
