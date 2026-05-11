# Broker Setup

Bedcrock supports two brokers behind a single `BrokerAdapter` contract:

- **Alpaca** — paper trading only. Easiest path. Two API keys, no Gateway, no ports.
- **IBKR** — paper or live. Requires IB Gateway/TWS running on the same host.

Pick one with the `BROKER` env var (`alpaca` or `ibkr`). Everything else
downstream — scorer, ingestors, monitor, reconciler — is broker-agnostic.

## Choose your broker

| You want… | Pick | Why |
|---|---|---|
| Fastest paper-only setup | `BROKER=alpaca` | No Gateway, no ports, no daily logout. Two keys and you're in. |
| Live trading | `BROKER=ibkr` | Alpaca brokerage is US-only. IBKR is the only live path supported. |
| You're outside the US and want live | `BROKER=ibkr` | Same reason. |
| You already run IB Gateway | `BROKER=ibkr` | No reason to switch. |

`BROKER=alpaca MODE=live` **refuses to boot** with a clear error message
("Alpaca live brokerage is US-only; use BROKER=ibkr for live in Canada").
This is enforced in `Settings._validate_broker_mode()` per the v0.4 truth table
in `docs/V4_ALPACA_PLAN.md` §3.

---

## Path A — Alpaca (paper only, easiest)

### 1. Sign up

1. Sign up at <https://alpaca.markets/>.
2. Activate the paper account (instant — no funding, no ID check for paper).

### 2. Generate paper API keys

1. Go to the paper dashboard:
   <https://app.alpaca.markets/paper/dashboard/overview>
2. In the right-hand sidebar, click **Generate New Keys** under **Your API Keys**.
3. Copy the **Key ID** and **Secret Key**. The secret is shown once — save it.

### 3. Configure `.env`

```ini
BROKER=alpaca
MODE=paper
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# Base URLs default to paper; usually no need to override.
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
ALPACA_STREAM_URL=wss://paper-api.alpaca.markets/stream
```

You can leave any `IBKR_*` vars set or empty — they're ignored when
`BROKER=alpaca`.

### 4. Sanity check

```bash
python -m src.workers.healthcheck
```

It hits `GET /v2/account` against the paper base URL and prints account equity.
Restart `ct-ingest` and `ct-monitor`. The next `/confirm` will open a bracket
on Alpaca paper.

### Why no live for Alpaca?

Alpaca brokerage onboarding is US-only — non-US residents cannot fund a live
account. Rather than ship a foot-gun, the config validator refuses
`BROKER=alpaca MODE=live` at boot.

---

## Path B — IBKR (paper or live)

IBKR gives you a free paper trading account with every account — no funding
required.

### 1. Create an IBKR account

1. Sign up at <https://www.interactivebrokers.com> (or `.ca` for Canada)
2. Account type: **IBKR Pro** individual
3. Complete identity verification (required even for paper)
4. No minimum deposit needed for paper trading

### 2. Install TWS or IB Gateway

You need one of these running on the same machine as Bedcrock:

- **TWS (Trader Workstation)** — full GUI. Best for development/testing on
  your laptop. Download: <https://www.interactivebrokers.com/en/trading/tws.php>
- **IB Gateway** — headless, lighter. Best for VPS/production. Download:
  <https://www.interactivebrokers.com/en/trading/ib-gateway-stable.php>

### 3. Log in with Paper Trading

When TWS/Gateway starts, choose **Paper Trading** login (not Live).

### 4. Enable the API

In TWS: **File > Global Configuration > API > Settings:**

- Check **Enable ActiveX and Socket Clients**
- Check **Allow connections from localhost only** (or add 127.0.0.1 to trusted IPs)
- Uncheck **Read-Only API** (we need to place orders)
- Note the **Socket port**: 7497 (TWS paper) or 4002 (Gateway paper)

### 5. Configure `.env`

```ini
BROKER=ibkr
MODE=paper
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # Gateway paper. Use 7497 for TWS paper.
IBKR_CLIENT_ID=1
IBKR_ACCOUNT=DUxxxxxx   # Your paper account ID (shown in TWS title bar)
```

Restart `ct-ingest` and `ct-monitor`. The first `/confirm` will go to paper.

## Live trading (IBKR only)

When you're ready to go live (after >=30 days paper trading):

1. Fund your IBKR account
2. Log in to TWS/Gateway with **Live Trading** instead of Paper
3. Update `.env`:

```ini
BROKER=ibkr
MODE=live
IBKR_PORT=4001          # Gateway live. Use 7496 for TWS live.
IBKR_ACCOUNT=Uxxxxxxx   # Your live account ID
```

**Don't switch to MODE=live without first running >=30 trading days on paper
and meeting the graduation criteria in the plan.**

## VPS deployment (headless, IBKR)

On a headless VPS, run IB Gateway with a virtual display:

```bash
sudo apt install -y xvfb
xvfb-run --auto-servernum ./ibgateway
```

The keepalive watchdog (`ibc` or homemade) is essential — IB Gateway
logs out daily for maintenance. See <https://github.com/IbcAlpha/IBC> for
automatic login handling.

(Alpaca needs none of this — it's a hosted REST/WebSocket API.)

## Port reference (IBKR)

| Software    | Paper | Live |
|-------------|-------|------|
| TWS         | 7497  | 7496 |
| IB Gateway  | 4002  | 4001 |

---

## How the broker abstraction works

Every broker implements `BrokerAdapter` in `src/broker/base.py` — the same
surface for `submit_bracket`, `get_account`, `cancel_order`, `get_order`,
`get_last_price`, `iter_open_orders`, `iter_positions`,
`repair_child_to_gtc`, and `subscribe_trade_updates`. `make_broker()` in
`src/broker/__init__.py` dispatches on `settings.broker` and returns either an
`IBKRBroker` or an `AlpacaBroker`. The rest of the system — scorer, ingestors,
order builder, live monitor, reconciler, FastAPI surface, Discord bot — never
imports a concrete broker class. Swap brokers by flipping one env var.

## Stop-loss GTC invariant on Alpaca

Alpaca bracket child legs can come back with `time_in_force == "day"` even
when the parent is GTC — their API inherits unpredictably across submit paths.
**Invariant 6** of the project ("stops are GTC by construction") therefore has
two safety nets on the Alpaca side:

1. The adapter verifies every child leg's TIF immediately after `submit_bracket`
   returns. If a child is not GTC, it calls `repair_child_to_gtc()` (cancel +
   resubmit as GTC) and emits an `alpaca_stop_repaired` structlog event.
2. The reconciler's `audit_open_order_tifs()` audits the wire on every startup
   and every monitor reconnect. Any non-GTC child found in flight is repaired
   the same way, with an `AuditLog` row recording the drift.

If you see `alpaca_stop_repaired` events in production logs, the safety net is
doing its job — but file an issue so we can investigate why Alpaca returned
DAY-TIF for that order class.

## Troubleshooting

### Alpaca

- **`401 Unauthorized`**: paper keys pasted into a live URL (or vice versa).
  Check that `ALPACA_BASE_URL` is `paper-api.alpaca.markets`.
- **`422 forbidden.trading`**: the paper account was disabled. Regenerate keys
  from the paper dashboard.
- **WebSocket disconnects every few minutes**: Alpaca free tier rate-limits the
  stream; the adapter auto-reconnects with backoff and the 30s polling fallback
  in the monitor catches anything missed.

### IBKR

- **"Failed to connect to IB Gateway"**: Is TWS/Gateway running? Is the API
  enabled? Is the port correct?
- **"Could not qualify contract"**: The ticker may not be available on IBKR
  or the market may be closed.
- **Daily disconnects**: IB Gateway disconnects daily for server resets
  (~midnight ET). Use IBC for auto-reconnect.
- **PDT rule**: Not applicable in Canada. US users with < $25k equity should
  be aware of the pattern day trader rule.
