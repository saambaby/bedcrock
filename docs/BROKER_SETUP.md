# Broker Setup

Bedcrock uses **Interactive Brokers (IBKR)** for both paper and live trading.
Paper vs live is just a different port — same code path.

## Paper trading — IBKR Paper

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
MODE=paper
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # Gateway paper. Use 7497 for TWS paper.
IBKR_CLIENT_ID=1
IBKR_ACCOUNT=DUxxxxxx   # Your paper account ID (shown in TWS title bar)
```

Restart `ct-ingest` and `ct-monitor`. The first `/confirm` will go to paper.

## Live trading

When you're ready to go live (after >=30 days paper trading):

1. Fund your IBKR account
2. Log in to TWS/Gateway with **Live Trading** instead of Paper
3. Update `.env`:

```ini
MODE=live
IBKR_PORT=4001          # Gateway live. Use 7496 for TWS live.
IBKR_ACCOUNT=Uxxxxxxx   # Your live account ID
```

**Don't switch to MODE=live without first running >=30 trading days on paper
and meeting the graduation criteria in the plan.**

## VPS deployment (headless)

On a headless VPS, run IB Gateway with a virtual display:

```bash
sudo apt install -y xvfb
xvfb-run --auto-servernum ./ibgateway
```

The keepalive watchdog (`ibc` or homemade) is essential — IB Gateway
logs out daily for maintenance. See <https://github.com/IbcAlpha/IBC> for
automatic login handling.

## Port reference

| Software    | Paper | Live |
|-------------|-------|------|
| TWS         | 7497  | 7496 |
| IB Gateway  | 4002  | 4001 |

## Troubleshooting

- **"Failed to connect to IB Gateway"**: Is TWS/Gateway running? Is the API
  enabled? Is the port correct?
- **"Could not qualify contract"**: The ticker may not be available on IBKR
  or the market may be closed.
- **Daily disconnects**: IB Gateway disconnects daily for server resets
  (~midnight ET). Use IBC for auto-reconnect.
- **PDT rule**: Not applicable in Canada. US users with < $25k equity should
  be aware of the pattern day trader rule.
