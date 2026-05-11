---
title: Bedcrock — Build & Migration Plan
status: active
version: v2
phase: paper-1
created: 2026-05-03
updated: 2026-05-11
implemented: 2026-05-10
shipped_as: v0.2.0
tags: [trading/system, plan, obsidian]
---

# Bedcrock

A signal-aggregation and analysis system that watches politicians, hedge fund titans, insiders, and options whales; scores trade ideas; runs deep Claude analysis on a schedule; paper-trades them through a broker; and graduates to live capital only after empirical validation.

> [!warning] Not financial advice
> This is a personal research/decision-support system. It is not a registered advisory service. Do not let other people's money ride on it without consulting a securities lawyer in your jurisdiction.

> [!info] Document version
> This is the canonical, self-contained spec. It folds in everything that landed in v0.2.0 (the six audit fixes F1–F6 and the four Proxy Bot ports N1–N4). For the evolution from the v0.1 spec, see Appendix C — Version history.

---

## 1. Goals & Non-Goals

**Goals**
- Surface high-signal trade ideas from delayed but reliable disclosures (STOCK Act, 13F, Form 4) and faster sources (options flow, public statements).
- Use Claude (via Cowork) for the heavy reasoning layer four-plus times per day, with always-on infrastructure handling the parts Claude can't.
- Build paper-trading data with the **exact same schema** as live, so the live switch is a config flip, not a rewrite.
- Generate a feedback loop (closure post-mortems, weekly synthesis) that improves the scoring rules over time.

**Non-Goals**
- High-frequency or intraday scalping. The system's edge is days-to-weeks, not seconds.
- Fully autonomous execution. **Human one-click review on every entry is a permanent design feature, not a phase-limited safety wheel.** The bot prepares the order; you confirm it.
- Selling signals to other people. Out of scope; regulatory rabbit hole.

---

## 2. Architectural Principles

These are the invariants. Every component has to respect them or migration breaks.

1. **Paper and live share one data path.** The only difference between paper and live is the broker endpoint and a `mode: paper|live` flag. Everything else — vault layout, Discord channels, Cowork prompts, scoring, schemas — is identical.
2. **Cowork is the reasoning layer, not the infrastructure layer.** Anything that needs to run while your laptop is asleep lives on the always-on backend.
3. **The vault is the source of truth.** Discord and the broker are views on top of vault state, not parallel systems.
4. **Inbox-then-process.** The backend only writes to `00 Inbox/`. Cowork only writes to everywhere else. No write conflicts ever.
5. **Every decision leaves a trace.** Every entry, exit, and skipped signal gets a note. Without traces, the weekly synthesis has nothing to learn from.
6. **Humans confirm entries; machines manage exits.** Bot prepares the bracket order with stop and target attached; you click once to send it. Once filled, server-side OCO at the broker manages the exit even if your VPS dies. This split is intentional: humans are good at sanity-checking but bad at obeying stops, machines are the opposite.
7. **Broker truth wins on conflict.** On any startup or post-disconnect reconnect, IBKR's view of positions and open orders is the source of truth; the DB is repaired to match (with an audit-log entry per repair). Orphan positions in IBKR with no DB record raise an alert; DB rows the broker has no record of are marked closed-externally.
8. **Stops are GTC by construction.** No code path may submit a child order with `tif != "GTC"`. A reconciler audit re-issues any non-conforming order found on the wire. Bracket parents are DAY (entry-zone-as-daily-decision); bracket children (stop and take-profit) are GTC + `outsideRth=True` so overnight gaps and pre/post-market action are protected.
9. **Mode and port are coupled.** `MODE=paper` requires `IBKR_PORT ∈ {4002, 7497}` (4002 = IB Gateway paper, 7497 = TWS paper). `MODE=live` requires `{4001, 7496}`. Mismatched config refuses to boot. Prevents the "shipped paper code to the live port" disaster.

---

## 3. System Map

```mermaid
flowchart LR
    subgraph Sources
        A1[SEC EDGAR<br/>Form 4, 13F, 13D/G]
        A2[Capitol Trades / Quiver<br/>politician trades]
        A3[Unusual Whales<br/>options flow]
        A4[X / RSS<br/>letters, interviews]
        A5[Earnings & Event<br/>Calendar]
        A6[Market Data<br/>OHLCV + indicators]
        A7[Heavy Movement<br/>volume / 52w / gap]
    end

    subgraph Backend [Always-on Backend]
        B1[Ingestors]
        B2[Scorer + Gates<br/>liquidity, earnings,<br/>correlation, regime]
        B3[Live Monitor<br/>price + position]
        B4[IBKR Adapter<br/>Paper / Live]
        B5[Indicator<br/>Computer]
        B6[Reconciler<br/>broker truth wins]
    end

    subgraph Vault [Obsidian Vault]
        V1[00 Inbox]
        V2[01 Watchlist]
        V3[02 Open Positions]
        V4[03 Closed]
        V5[04 Traders]
        V6[05 Daily]
        V7[99 Meta]
    end

    subgraph Cowork [Cowork Scheduled Tasks]
        C1[Morning Heavy Run<br/>08:00 ET]
        C2[Intraday Light Runs<br/>11:00, 14:00, 16:30]
        C3[Hourly Closure Run]
        C4[Weekly Synthesis<br/>Sun 18:00]
    end

    Discord[Discord<br/>#signals-firehose<br/>#high-score<br/>#position-alerts]
    Human((You<br/>one-click confirm))

    Sources --> B1 --> B2 --> V1
    A6 --> B5 --> V2
    A7 -- "corroboration only" --> B2
    B2 --> Discord
    B3 --> V1
    B3 --> Discord
    B4 <--> B3
    B6 <--> B4
    V1 --> Cowork
    Cowork --> V2 & V3 & V4 & V6 & V7
    Cowork --> Discord
    Cowork -- "ACT TODAY list" --> Human
    Human -- "click confirm" --> B4
    B4 -- "fill confirmation" --> V3
```

---

## 4. Phase Gates

Four phases, each with explicit graduation criteria. Don't skip phases. Most of the value of this system comes from the data you accumulate across them.

| Phase | Capital | Duration | Goal | Graduates when… |
|---|---|---|---|---|
| **Paper Phase 1** | $0 (sim) | 4–6 weeks | Plumbing works end-to-end | All components log a full week with no manual fixes; ≥10 closed paper trades |
| **Paper Phase 2** | $0 (sim) | 90 days min | Empirical validation | ≥50 closed trades, Sharpe > 1.0, win rate stable across 4 consecutive 2-week windows, beats SPY on risk-adjusted return |
| **Live Phase 1** | 10–25% of intended size | 60 days min | Real fills, real slippage | Live Sharpe within 30% of paper Sharpe, max drawdown < pre-set limit, no operational incidents |
| **Live Phase 2** | Full size | ongoing | Steady state | Continuous monitoring; revert to Live Phase 1 sizing on any 2-week breach of guardrails |

> [!note] The Sharpe-within-30% rule
> Paper-to-live Sharpe degradation is normal — usually 20–40% — because slippage, partial fills, and overnight gaps that look fine in sim are real losses live. If your live Sharpe is more than 30% worse than paper's, the paper sim is unrealistic, not the strategy. Tighten the slippage model and re-run.

### 4.1 Paper Phase 1 → Paper Phase 2 acceptance checklist

- [ ] All v0.1 critical bugs fixed (F1–F6 landed in code)
- [ ] `tests/test_orders.py::test_bracket_children_are_gtc` passes
- [ ] `tests/test_orders.py::test_double_fill_idempotency` passes
- [ ] `tests/test_orders.py::test_startup_reconciles_orphan_ibkr_position` passes
- [ ] `tests/test_gates.py::test_daily_kill_switch_blocks_at_negative_threshold` passes
- [ ] `_reconcile_against_broker` has run on at least 5 startups with no false-positive alerts
- [ ] At least 10 closed paper trades
- [ ] No orphaned IBKR positions in any reconciliation run
- [ ] No duplicate Position rows in DB (verify with `SELECT broker_order_id, COUNT(*) FROM positions GROUP BY broker_order_id HAVING COUNT(*) > 1` returns empty)
- [ ] All bracket stops on the wire are GTC (verify by `audit_open_order_tifs()` returns empty across 7 daily runs)
- [ ] IBC nightly logout survived ≥ 7 nights with auto-reconnect
- [ ] Heavy-movement ingestor (§5.1 / §5.7) running and emitting signals during market hours
- [ ] Sector-correlation gate has blocked at least one over-concentrated trade attempt

### 4.2 Paper Phase 2 → Live Phase 1 acceptance checklist

- [ ] At least 90 calendar days in Paper Phase 2
- [ ] At least 50 closed paper trades
- [ ] Sharpe ratio > 1.0 over the full Phase 2 period
- [ ] Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014) > 0.95 — adjusts for the multiple weight tweaks
- [ ] Profit factor > 1.5
- [ ] Win rate stable: stdev across 4 consecutive 2-week buckets < 10pp
- [ ] Average excess return vs. SPY > 0 (computed against the SPY baseline portfolio, §6.5)
- [ ] Max drawdown < 15% of starting paper equity
- [ ] No more than 1 operational incident in last 30 days
- [ ] At least 3 signal types each with ≥ 10 closed trades
- [ ] Weekly synthesis has adopted at least 2 rule refinements via the mini-backtester replay-validated flow (§5.5)
- [ ] At least one rule change has been REJECTED by the replay's out-of-sample test (proves the gate works)
- [ ] All four safety changes have been triggered at least once in paper:
  - GTC stop fired overnight
  - Idempotency check skipped a duplicate fill
  - Startup reconciler caught an orphan
  - Daily kill switch tripped (or dry-run forced it to)

### 4.3 Live Phase 1 → Live Phase 2 acceptance checklist

- [ ] At least 60 calendar days in Live Phase 1
- [ ] At least 25 closed live trades
- [ ] Live Sharpe within 30% of Phase 2 paper Sharpe (otherwise the slippage model is wrong)
- [ ] Max drawdown stayed within `99 Meta/risk-limits.md` Live Phase 1 limits
- [ ] No operational incidents requiring manual broker intervention
- [ ] Paper kept running in parallel for the full 60 days (head-to-head data exists)

---

## 5. Components

### 5.1 Data Sources

| Source | Latency | Cost | Use |
|---|---|---|---|
| SEC EDGAR (Form 4) | 2 business days | Free | Insider buys/sells — fastest disclosure channel |
| SEC EDGAR (13D/G) | 10 days | Free | Activist stakes |
| SEC EDGAR (13F) | up to 45 days | Free | Hedge fund quarterly snapshots |
| Capitol Trades | 1–45 days | Free (web scrape) | Politician trades |
| Quiver Quantitative | 1–45 days | Paid API | Politician trades + return calc |
| Unusual Whales | real-time | Paid API | Options flow, dark pool, congress |
| X / Twitter list | real-time | Free–paid | Tracked traders' public posts |
| RSS / Substack | real-time | Free | Hedge fund letters, fund manager interviews |
| TradingView alerts | real-time | $15/mo | Per-ticker price triggers |
| **Earnings calendar** | real-time | Free (Finnhub) | Block/flag entries near earnings |
| **Event calendar** | real-time | Free (FDA, FOMC) | Block/flag entries near scheduled catalysts |
| **OHLCV + indicators** | end-of-day + intraday | Free (Polygon free tier, yfinance fallback) | Pattern/regime layer (§5.8) |
| **Heavy-movement detector** | every 5 min, market hours | Free (computed from OHLCV) | Volume spike / 52w breakout / gap — corroboration source only, never a primary signal |
| **News headlines** | real-time | Free (Finnhub/Benzinga RSS) | Sentiment modifier — Phase 2 add |

Pick the cheapest set that covers two faster channels and one slower channel for Paper Phase 1, plus earnings calendar and OHLCV (those two are not optional). Add the rest in Phase 2.

**Heavy-movement ingestor (a.k.a. N1).** Runs every 5 minutes during US market hours. For each ticker in the active watchlist (open positions + recent `#high-score` candidates), it computes today's volume vs the 20-day average, the gap from prior close, and whether today's high prints a new 52-week high. Triggers on volume ≥ 3× average, |gap| ≥ 5%, or a 52w breakout. Emits a `Signal` row with `source = MARKET_MOVEMENT`. Hard rule: market-movement signals score 0 in isolation — they only count as flow corroboration on a ticker that already has a non-MARKET_MOVEMENT signal in the last 14 days. This preserves the disclosure-driven thesis invariant. Empirical basis: George & Hwang (*Journal of Finance*, 2004), 52-week-high breakouts return +0.65%/month with no long-run reversal.

### 5.2 Always-On Backend

**Where it runs:** $5/mo Hetzner or DigitalOcean VPS, Ubuntu 22.04, Python 3.11+. Or a Cloudflare Worker if you want serverless.

**Stack:**
- `python` + `httpx` for ingestors
- `apscheduler` or systemd timers for polling cadence
- `postgres` + `asyncpg` + `alembic` for raw signal storage (multi-worker safe; SQLite would block on writer contention)
- `pydantic` for the signal schema and settings
- `discord.py` + `discord-webhook` for posts and slash commands
- **`ib_async==2.1.0`** for the IBKR adapter (paper and live, same code path) — pure-asyncio, actively maintained successor to the abandoned `ib_insync`
- `pandas` + `pandas-ta` for indicator computation (Wilder ATR, RSI)
- `syncthing` for vault file sync between VPS and your laptop
- `git` cron job for daily vault backup to a private repo
- **IBC** (https://github.com/IbcAlpha/IBC) + Xvfb in a `gnzsnz/ib-gateway-docker`-style container for IB Gateway lifecycle. IBKR forces a logout window 23:45–00:45 ET nightly and again Sunday for re-auth; IBC handles auto-relogin. Configure `AutoRestartTime=23:45` (time-based, not token-based — token AutoRestart is broken on unfunded paper accounts per IBC issue #345). The `LiveMonitor` tolerates a ~5-min disconnect without paging. `IBKRBroker.connect()` retries with exponential backoff (5 attempts: 1s, 2s, 4s, 8s, 16s) and posts a system-health alert on terminal failure.

**Responsibilities:**
- Poll each data source at its appropriate cadence (Form 4: every 15 min during market hours; 13F: daily; politician trades: every 30 min; options flow: websocket; earnings calendar: daily at 06:00 ET; OHLCV: end-of-day + on-demand; heavy-movement: every 5 min during market hours).
- Apply the scoring pipeline (see §8).
- Apply hard gates (see §5.7) before a signal escalates to `#high-score` or `ACT TODAY`.
- Compute and cache the indicator/pattern layer (see §5.8) for every ticker in the watchlist.
- Write signal `.md` files into `00 Inbox/`.
- Post to Discord webhooks.
- Run the live monitor (price subscriptions for tickers in `02 Open Positions/`).
- Prepare bracket orders for one-click confirmation (see §5.9).
- Talk to IBKR for paper or live fills and position state, depending on `MODE` and `IBKR_PORT`.

### 5.3 Vault Structure

```
Trading/
├── 00 Inbox/                  # backend writes only
├── 01 Watchlist/              # one note per active candidate
├── 02 Open Positions/         # one note per held position (paper or live)
├── 03 Closed/                 # post-mortems
├── 04 Traders/                # one note per tracked person
├── 05 Daily/                  # Cowork's run outputs
├── 06 Weekly/                 # synthesis notes
├── 99 Meta/
│   ├── scoring-rules.md       # current weights (versioned)
│   ├── watchlist-config.md    # tracked traders, sectors, thresholds
│   ├── risk-limits.md         # position size, drawdown, kill switch
│   └── changelog.md           # every rule change with rationale
└── Templates/                 # frontmatter templates
```

**Frontmatter schemas** — keep these stable across paper and live:

`00 Inbox/` signal file:
```yaml
---
type: signal
status: new          # new | processed | ignored
mode: paper          # paper | live
ticker: NVDA
trader: "[[Pelosi]]"
source: capitol-trades
action: buy
disclosed_at: 2026-05-02
trade_date_range: [2026-04-15, 2026-04-15]
size_range_usd: [50000, 100000]
score: 7.2
score_breakdown:
  cluster: 2.0
  committee_match: 1.5
  size: 1.7
  sector_momentum: 2.0
links:
  source_url: https://capitoltrades.com/...
urgent: false
---
```

`02 Open Positions/` position file:
```yaml
---
type: position
status: open         # open | closed
mode: paper          # paper | live
ticker: NVDA
broker_order_id: ibkr_xxx
entry_date: 2026-05-03
entry_price: 942.15
quantity: 5
size_usd: 4710.75
stop: 866.78
target: 1130.00
thesis_link: "[[01 Watchlist/NVDA]]"
source_signals:
  - "[[00 Inbox/2026-05-02-NVDA-pelosi]]"
  - "[[00 Inbox/2026-04-28-NVDA-form4]]"
---
```

`03 Closed/` post-mortem file: same as position, plus `exit_date`, `exit_price`, `pnl_usd`, `pnl_pct`, `holding_days`, `vs_spy_pct`, `vs_sector_pct`, `close_reason: stop_hit | target_hit | signal_exit | discretionary | external`.

> [!tip] Why `mode: paper|live` matters
> Every Dataview query, every Cowork prompt, every report filters on this field. When you migrate, you don't change anything about how data is stored — paper and live just live side-by-side and queries pick which to look at. You can even run them in parallel: paper continues validating new rule changes while live runs the validated rules.

### 5.4 Discord

Three primary channels plus an ops channel, one webhook each:
- `#signals-firehose` — every raw signal, low signal-to-noise, browse-only.
- `#high-score` — score ≥ threshold, the channel you actually watch.
- `#position-alerts` — entries, exits, stops hit, urgent events.
- `#system-health` — ingestor heartbeats, reconciler repairs, broker reconnect alerts.

A small bot (~80 lines, `discord.py`) handles slash commands:
- `/thesis TICKER` — reads `01 Watchlist/TICKER.md`, posts thesis.
- `/positions` — lists open positions from `02 Open Positions/`.
- `/snooze TICKER 7d` — adds a snooze entry to `99 Meta/snoozed.md` so the scorer ignores that ticker for 7 days.
- `/pnl` — paper-mode equity curve summary.
- `/confirm <id>` and `/skip <id> reason: …` — order confirmation flow (§5.9).
- `/heartbeat` — surfaces per-ingestor heartbeat ages (drives external monitoring).

### 5.5 Cowork Scheduled Tasks

Four scheduled tasks. Keep prompts in `Templates/` so you version them with the rest of the vault.

#### Morning Heavy Run — daily, 08:00 ET (90 min before US open)

```
You are running the morning heavy analysis. Today is {{date}}.

Step 1 — Sweep:
Read every file in ~/Obsidian/Trading/00 Inbox/ where status: new
AND no blocking gate is set. Skip files where gate_blocked: true
unless the gate is overrideable and the override flag is set.
Read all files in 01 Watchlist/ whose frontmatter score changed in the
last 24h. Read 02 Open Positions/ in full.

Step 2 — Regime context:
Fetch overnight ES/NQ futures, Asia and Europe session moves, VIX,
DXY, US 10Y yield, and today's macro calendar (Fed speakers, CPI,
NFP, etc.). Note any events within 48h that could move the book.
Tag the day's market_regime (bull_low_vix | bull_high_vix |
bear_low_vix | bear_high_vix). Write findings to
05 Daily/{{date}}-regime.md.

Step 3 — Per-candidate thesis build:
For each high-score candidate (score >= threshold from
99 Meta/scoring-rules.md), read its indicators block from the
watchlist note. Reject if indicators stale (computed_at > 24h ago).
Write or update 01 Watchlist/<TICKER>.md with: bull case, bear case,
technical levels (use the swing_high_90d / swing_low_90d / SMAs from
the indicators), catalyst calendar 30d out, position sizing relative
to current book (check correlation gate), entry zone (must respect
ATR — stop never tighter than 1.5x ATR_20), invalidation level,
profit targets. Follow [[trader]] wikilinks for context on the
originator's history with this name.

Step 4 — Game plan:
Write 05 Daily/{{date}}-gameplan.md with three sections:
  - ACT TODAY: specific orders for the backend to draft, with size,
    entry zone, stop, target, and the setup tag (breakout | pullback
    | base | mean_reversion | none)
  - WATCH TODAY: price levels that would activate something
  - PASSIVE: no action unless specific event hits

Step 5 — Hand off to backend:
For each ACT TODAY entry, write a corresponding row to
00 Inbox/orders/<id>.md with status: ready_for_draft. The backend
will pick these up, run final gates, construct bracket orders, and
post one-click confirm embeds to #high-score for you to confirm
or skip via Discord.

Step 6 — Mark inbox files processed.
```

#### Intraday Light Runs — daily, 11:00 / 14:00 / 16:30 ET

```
Light intraday check. Today is {{date}}, run at {{time}}.

1. Read 05 Daily/{{date}}-gameplan.md.
2. Check current prices for everything in WATCH TODAY. If any
   level activated, write a brief alert note to 00 Inbox/ flagged
   urgent: true and post to #high-score.
3. Read 00 Inbox/ for new signals since the last run. If any
   has score >= URGENT_THRESHOLD from scoring-rules.md, do a
   compressed thesis build (5-7 sentences) and post to #high-score.
4. Re-check open positions: any approaching stop/target, any news
   in the last 3h. If yes, alert in #position-alerts.
5. Append a short "intraday note" to 05 Daily/{{date}}-intraday.md.

Do NOT rebuild the morning thesis. Do NOT touch closed positions.
Stay light — this run should finish in <5 minutes.
```

#### Hourly Closure Run — hourly, market hours + 1h after

```
Process closure events.

1. Read 00 Inbox/ for files with type: closure and status: new.
   These are written by the live monitor when a paper or live
   position closes.
2. For each, write a post-mortem to 03 Closed/<DATE>-<TICKER>.md
   with the schema in 99 Meta/templates. Include:
   - Entry/exit/pnl
   - Why it worked or didn't (mechanism, not just direction)
   - Original signal predicted vs. actual driver
   - Compare return to SPY and sector ETF over same window
   - One specific lesson (concrete rule change, not vague)
3. If the lesson suggests a scoring weight change, append a
   proposal to 99 Meta/scoring-rules-proposed.md. Do NOT modify
   scoring-rules.md directly — the weekly synthesis adopts proposals.
4. Mark closure inbox files processed.
5. Post a compact summary to #position-alerts.
```

#### Weekly Synthesis — Sunday 18:00 local

```
System-level learning pass.

1. Read every file in 03 Closed/ with exit_date in the last 30 days.
2. Read 99 Meta/scoring-rules.md (current) and scoring-rules-proposed.md
   (proposals from the closure runs). For each proposal, read the
   matching mini-backtester replay report from
   06 Weekly/{{date}}-replay-{rule}.md (written by the EOD worker —
   see "Mini-backtester" below).
3. Compute per-trader, per-source, per-sector stats:
   - Win rate
   - Average return
   - Average return vs. SPY
   - Average holding period
   - Sharpe-like ratio
4. Write 06 Weekly/{{date}}-synthesis.md with:
   - Top 3 working signal patterns
   - Top 3 failing signal patterns (candidates for removal/dampening)
   - Whether each proposed rule change in scoring-rules-proposed.md
     is supported by data; for each, recommend ADOPT or REJECT, citing
     both the closed-trade stats AND the replay's
     ADOPT/REJECT/INCONCLUSIVE recommendation
5. If recommending ADOPT, update 99 Meta/scoring-rules.md, append
   to 99 Meta/changelog.md with rationale, and clear the relevant
   line from scoring-rules-proposed.md.
6. Phase-gate check: read 99 Meta/risk-limits.md and the synthesis
   stats. If we're in Paper Phase 2 and all graduation criteria
   are met (see §4 of system plan), post a "READY FOR LIVE PHASE 1"
   note to #high-score with the supporting numbers.
7. Post a 5-bullet summary of the synthesis to #high-score.
```

#### Mini-backtester (N4)

The weekly synthesis depends on a mini-backtester at `src/backtest/replay.py` that re-scores past signals (already in the DB) under a proposed weight set, simulates entries/exits at OHLCV boundaries, and reports the Sharpe delta. This exists because v1's "paper trading IS the backtest" philosophy is defensible for the signal universe (politician trades, insider buys are hard to backtest faithfully) but breaks down for *evaluating weight changes*: with 50 trades, a 1-point change on `cluster_per_extra_source` is statistically indistinguishable from noise.

The replay tool guards against overfitting by reserving the last 30 days of signals as out-of-sample, reporting both in-sample and out-of-sample Sharpe, and refusing to recommend ADOPT unless out-of-sample Sharpe beats the baseline. Entry rule is T+1 OPEN, exit is OCO at a configurable stop loss (default 10%) or target (default 1.5R) or timeout (default 30 days). Slippage is a flat-bps assumption. The output is a `ReplayReport` written to `06 Weekly/{date}-replay-{rule}.md` with an ADOPT / REJECT / INCONCLUSIVE recommendation that the human-confirmed weekly synthesis uses as one input — never the sole input.

Caveats baked into the module: historical OHLCV only (no bid/ask depth), constant slippage, no survivorship-bias correction (yfinance acknowledged). It's a sanity check, not a Monte Carlo. Treat the recommendation as advisory.

### 5.6 Live Monitor

A long-running Python process on the VPS:

- Subscribes to IBKR's `orderStatusEvent` and `execDetailsEvent` for instant fill notifications. Polling fallback every 30s catches missed events.
- **Stops and targets are server-side at the broker as OCO bracket orders, not enforced by this monitor.** The monitor *observes*; the broker *enforces*. If your VPS dies overnight, your stops still fire (because they're GTC by construction — see invariant 8).
- **Idempotency by construction.** `_on_entry_fill` first checks `SELECT Position WHERE broker_order_id = ?`; if a row already exists (because the WS handler beat the polling reconciler, or vice versa), it logs and returns instead of inserting a duplicate. Defense in depth: `Position.broker_order_id` has a `UNIQUE` constraint at the DB layer, so a second insert would error rather than corrupt.
- **Startup reconciliation against IBKR (invariant 7).** On `LiveMonitor.start()`, after broker connect and before the event loop begins, runs `_reconcile_against_broker`: any IBKR position not in the DB raises an `orphan_ibkr_position` alert and an `AuditLog` row; any open DB position the broker has no record of is marked `status=CLOSED, close_reason=EXTERNAL`. This catches the "worker crashed between `placeOrder` and writing the Position row" failure mode and the "user closed via mobile app" case.
- **Reconciler audit (invariant 8).** Every 30s, walks `ib.openTrades()` and re-issues any child order with `tif != "GTC"` as GTC + `outsideRth=True`. Posts a `#system-health` alert per repair.
- On every tick, updates the position file with the current price and unrealized P&L (so the dashboard is always live).
- On stop or target fill received from broker, writes a closure event to `00 Inbox/` flagged `type: closure`, updates `02 Open Positions/` → moves the file to `03 Closed/`, and posts to `#position-alerts`.
- Re-reads `02 Open Positions/` every 30 seconds so newly-opened positions get monitored automatically.
- Heartbeat: posts a status message to `#system-health` every 15 min during market hours so you can see it's alive.
- **Daily P&L computation (F5).** A `daily_pnl` worker computes `daily_pnl_pct = (current_equity - start_of_day_equity) / start_of_day_equity × 100` once a minute during market hours and stashes it in a small `DailyState` table keyed on `(date, mode)`. The `daily_kill_switch` gate reads from this table — when `daily_pnl_pct ≤ -RISK_DAILY_LOSS_PCT` (default -2%), the gate blocks all new entries until next session. Override is **not** allowed.

### 5.7 Hard Gates

Every signal must pass these gates before it can escalate to `#high-score` or appear in `ACT TODAY`. Gates are *binary* — they don't dampen the score, they block the trade outright. The gate result is logged to the signal frontmatter so the weekly synthesis can ask "did blocked trades have outperformed?"

| Gate | Rule | Override? |
|---|---|---|
| **Liquidity** | 30-day average dollar volume ≥ $5M (paper) / $20M (live) | Never. Fail-closes if ADV data is missing. |
| **Earnings proximity** | No entry within 3 trading days of scheduled earnings, before or after | Manual override allowed if thesis explicitly involves the print |
| **Scheduled event proximity** | No entry within 2 trading days of FOMC, CPI, NFP, or known FDA/contract dates for the ticker | Manual override allowed |
| **Sector correlation** | New position would push portfolio's largest sector concentration over 25% of equity (`RISK_SECTOR_CONCENTRATION_LIMIT`) | Manual override allowed with thesis justification |
| **Stale signal** | Signal more than 14 days old at first surfacing (handles backlogged disclosures hitting late) | Manual override |
| **Snoozed ticker** | Ticker is in `99 Meta/snoozed.md` | Lift snooze in Discord (`/snooze TICKER 0`) |
| **Daily loss kill switch** | Account at or below the day's loss limit (default -2%) | Resets next session |
| **Open positions cap** | Already at max open positions for the phase | Close something or wait |

The earnings-proximity gate is the one that quietly saves you the most pain. Pelosi buying NVDA the week before earnings is a different trade than buying it in a quiet window — the disclosure didn't tell you which.

**Sector-correlation gate (N2).** Bedcrock's universe is structurally cluster-prone — politicians on Armed Services committees buy defense; biotech insiders correlate with FDA cycles. Without this gate, "5 positions" can really be one bet. A `SECTOR_ETF_MAP` ships with mappings for the largest names (`LMT/RTX/NOC/GD/BA → ITA`; `MRNA/BNTX/CRSP/VRTX → XBI`; `NVDA/AAPL/MSFT/GOOGL/META → XLK`; `AMZN/TSLA → XLY`; `XOM/CVX/COP → XLE`; `JPM/BAC/GS → XLF`; etc.); unmapped tickers fall through to `OTHER`. The gate sums existing exposure by sector, projects the proposed position at its worst-case (max 5% of equity per the half-Kelly cap), and blocks if the sum exceeds 25% of equity. Fail-open if account equity or indicator data is missing — the gate doesn't have to make the trade, but it shouldn't block on its own confusion.

**Heavy-movement signals are not gated separately** — they are pre-filtered in the scorer (§8) so they cannot trigger a draft order on their own.

### 5.8 Pattern & Indicator Layer

This is **a filter and timing layer, not a signal source.** Don't let it generate trade ideas. Let it sharpen entries, exits, and stops on ideas that already passed the fundamental signal cluster.

**What the backend computes** (cached per ticker, refreshed end-of-day and on-demand during the morning run):

| Indicator | Use |
|---|---|
| 50-day SMA, 200-day SMA | Trend regime: above both = uptrend; below both = downtrend; mixed = chop |
| 20-day ATR (Wilder, via `pandas-ta`) | Stop distance and position sizing — never put a stop tighter than 1.5×ATR |
| 14-day RSI | Overbought/oversold context, not a signal |
| 30-day IV percentile | Volatility regime; affects size and option strategy if used |
| 30-day average dollar volume | Liquidity gate input |
| Relative strength vs. SPY (60-day) | Is the name leading or lagging the market |
| Relative strength vs. sector ETF (60-day) | Is the name leading or lagging its peers |
| 20-day high / 20-day low | Breakout / breakdown reference levels |
| Recent swing levels (last 90 days) | Support and resistance for entry zones and stops |

These are written into the watchlist note's frontmatter, so Cowork sees them as structured data, not raw OHLCV:

```yaml
indicators:
  trend: uptrend
  price: 942.15
  sma_50: 901.20
  sma_200: 820.40
  atr_20: 28.50
  rsi_14: 62
  iv_percentile_30d: 38
  adv_30d_usd: 412_000_000
  rs_vs_spy_60d: 1.18      # outperforming SPY by 18% over 60d
  rs_vs_sector_60d: 1.05
  swing_high_90d: 974.00
  swing_low_90d: 768.00
  setup: pullback_to_50sma  # one of: breakout, pullback, base, none
```

**What Cowork does with them.** The morning prompt asks Claude to interpret these in context: is the entry zone a sensible level given the swing structure, does the 1.5×ATR stop give the trade room without risking too much, is the relative strength supportive of the thesis. Claude *interprets*; it doesn't compute. The numbers come from the backend.

**What's deliberately excluded.** Candlestick pattern catalogs, Fibonacci retracements, Elliott Wave, multi-indicator crossover systems. These have weak empirical support as standalone alpha and would dilute the system's actual edge (the signal cluster). Resist adding them.

**Setup tagging.** The closure post-mortem records which setup type was active at entry (`breakout`, `pullback`, `base_breakout`, `mean_reversion`, `none`). After ~30 closed trades the weekly synthesis can answer "do my breakout entries outperform my pullback entries on the same fundamental signals?" — which is the actually useful pattern question.

### 5.9 Order Execution — One-Click Confirm Flow

The execution layer is deliberately split so the human is the trigger, but never has to babysit risk management once the order is live.

**Step-by-step:**

1. **Cowork morning run** writes `ACT TODAY` to `05 Daily/<date>-gameplan.md` and posts the list to `#high-score` with one Discord embed per candidate.
2. **Backend prepares draft bracket orders.** For every `ACT TODAY` row, the backend constructs a complete bracket order in memory (limit entry, OCO stop and target attached, calculated quantity, all gate checks passed) and writes a draft order file to `00 Inbox/orders/<id>.md` with status `draft`. It does *not* send it to the broker yet.
3. **Discord prompt to you.** The `#high-score` embed for each candidate has the full order preview (entry, stop, target, size, %risk, gate results) and a clear path to confirm. Two ways to confirm:
   - **Slash command in Discord:** `/confirm <id>` — bot reads the draft, sends the bracket to the broker, updates the order file to `status: sent`.
   - **Tap-friendly mobile:** the embed includes a deep link `claude-trade://confirm/<id>` (an `itsdangerous`-signed token, expires with the draft) that opens a tiny local web UI showing the order, with a single "Send to broker" button. Best on phone — tap it from bed at 8:15 ET, done.
4. **Broker fills**, `execDetailsEvent` fires, live monitor catches it (idempotent — see §5.6), position file written, `#position-alerts` ping. From this moment on, the OCO at the broker (GTC by construction) manages the exit.
5. **Skipping a trade is also one click.** `/skip <id> reason: earnings-too-close` writes `status: skipped` and the reason to the order file. Skipped orders feed the weekly synthesis the same as taken ones — "would I have made money on the trades I skipped?" is one of the most useful questions the system can answer.

**Position sizing.** Risk-based: `qty = (equity × RISK_PER_TRADE_PCT / 100) / |entry - stop|`. Stop is ATR-floored at 1.5× ATR_20 — auto-widened if the supplied stop is tighter. Then a **half-Kelly per-position concentration cap (N3)** is applied: `qty = min(qty_by_risk, (equity × RISK_MAX_POSITION_SIZE_PCT) / entry)`. This defends against pathological tight-stop sizing where a 1%-risk trade with a tiny stop becomes a 20%-of-equity position — risk *per trade* stays at 1%, but a halt or earnings shock crosses the stop and you eat the *full* position size as the loss. Default cap is 5% (`RISK_MAX_POSITION_SIZE_PCT=0.05`); Live Phase 1 overrides to 3% per §10.

**Default order type:** marketable limit at entry zone midpoint + OCO stop + take-profit. Not market orders — they slip on news. Not pure limits at the bottom of the entry zone — they don't fill on momentum.

**Time-in-force:** Parent (entry) is `tif="DAY"` — entry zones decay overnight, the morning run re-evaluates tomorrow. Children (stop, take-profit) are `tif="GTC"` + `outsideRth=True` so overnight gaps and pre/post-market action are protected. This is invariant 8 and is enforced both at submission (`submit_bracket` sets it) and by the reconciler audit (§5.6) which re-issues any non-conforming child it finds on the wire.

**What the bot will *never* do without a click:**
- Open a new position
- Add to an existing position
- Reverse a position
- Lift the daily kill switch
- Override a hard gate

**What the bot *will* do without a click:**
- Trail or move stops *only if* the position file's `auto_trail` flag is true and the rule is in `99 Meta/risk-limits.md` (e.g., "move stop to break-even after +1R"). Default off; you turn it on per-position.
- Close at stop or target via the pre-set OCO at the broker. (The broker enforces; the bot just records.)
- Cancel stale draft orders at end of day.
- Hit the daily kill switch on -2% drawdown.
- Re-issue a non-GTC child order found on the wire (reconciler).
- Mark stale DB positions as closed-externally when the broker has no record (reconciler).

---

## 6. Paper Trading & Broker Choice

### 6.1 Broker

**Same broker for paper and live: Interactive Brokers.** Paper and live are differentiated by `IBKR_PORT` only (`4002` IB Gateway paper / `7497` TWS paper / `4001` IB Gateway live / `7496` TWS live), validated by the mode↔port coupling (invariant 9). This collapses the entire migration story to two env-var changes (`MODE` and `IBKR_PORT`).

Why IBKR over Alpaca/Tradier (the v1 candidates):
- Best execution at any meaningful book size; cheapest at scale; international markets if ever needed.
- Mature paper environment that mirrors live behavior closely.
- Native bracket orders with server-side OCO.
- The cost is the API: `ib_async` (the maintained successor to `ib_insync`) is good but IB Gateway has a desktop-app heritage that needs IBC + Xvfb to run headless. See §5.2 for the deployment recipe.

### 6.2 Order Mechanics

Every entry is a **bracket order**: limit entry + OCO (one-cancels-other) stop loss + take profit. Sent in a single API call (three orders with shared `parentId`, atomic at IBKR). Children are GTC + `outsideRth=True` (invariant 8); parent is DAY.

The execution flow (from §5.9, restated for completeness):

1. Cowork's morning gameplan lists the candidate.
2. Backend constructs the full bracket draft, runs all gates, writes it to `00 Inbox/orders/<id>.md` as `status: draft`. Discord posts the preview embed to `#high-score`.
3. **You click confirm** — `/confirm <id>` in Discord, or tap the deep link to the local web UI. One action, no fields to fill.
4. Backend sends the bracket to IBKR. `client_order_id` is set to the `DraftOrder.id` UUID, so a duplicate confirm is rejected by the broker.
5. IBKR fills the entry; OCO sits server-side and will fire on stop or target *even if your VPS is offline* (because GTC).
6. Live monitor observes the fill via `execDetailsEvent` (idempotent), writes the position file, posts to `#position-alerts`.

### 6.3 Slippage Model

Don't take mid-price as your fill in paper. IBKR's paper sim is mildly optimistic; layer on top of it:

- Limit orders: fill only if price *crosses through* your limit by at least 1bp during the bar; assume 50% fill probability if it just touches.
- Market orders (used only for closes if OCO doesn't fire): fill at ask + 5bps (buy) or bid - 5bps (sell), plus 2bps for assumed market impact on size > $5k.
- For tickers with 30-day ADV below 500k shares: halve simulated fill size and add 10bps slippage.

Adjust these constants in `99 Meta/risk-limits.md` after Live Phase 1 based on observed slippage. The point is to make paper *pessimistic*, not optimistic — you want live results to surprise you positively, not the other way around.

### 6.4 Equity Tracking

Starting paper equity: **$100k**. Don't match it to your intended live size — you're testing the strategy, not the size. Sizing scales with equity in both modes via the `risk-limits.md` percentages.

Backend writes daily equity snapshots to `99 Meta/equity-curve.csv` and to the `EquitySnapshot` DB table:

```csv
date,mode,equity,cash,positions_value,daily_pnl,daily_pnl_pct
2026-05-03,paper,100000.00,100000.00,0.00,0.00,0.00
2026-05-03,live,25000.00,25000.00,0.00,0.00,0.00
```

Sharpe, drawdown, and phase-gate calculations read from this CSV. Once live is running, paper continues in parallel for at least 30 days so you have a head-to-head comparison.

### 6.5 The "Do Nothing" Baseline

Run a parallel paper portfolio that just buys SPY equal-weight every time the system would have entered. Same sizing, same hold periods, same exits driven by SPY's behavior on those dates. Track its equity curve in `99 Meta/equity-curve.csv` with `mode: baseline`. If your system's risk-adjusted return doesn't beat this baseline net of stress and effort, that's important to know early — and you'll only know if you tracked it from day one.

---

## 7. Data Captured Per Trade (the Migration Schema)

Every closed trade — paper or live — has these fields. Identical schema is what makes migration painless.

```yaml
ticker: NVDA
mode: paper                  # only field that differs at migration
entry_date: 2026-05-03
entry_price: 942.15
exit_date: 2026-05-21
exit_price: 1108.40
quantity: 5
holding_days: 18
pnl_usd: 831.25
pnl_pct: 17.65
fees_usd: 0.00               # paper = 0; live = real commissions

# Benchmarking
spy_return_pct: 1.8
sector_etf: SMH
sector_return_pct: 4.2
excess_vs_spy_pct: 15.85
excess_vs_sector_pct: 13.45

# Attribution
source_signals: [...]
score_at_entry: 7.2
score_breakdown_at_entry: {...}
trader_primary: "[[Pelosi]]"

# Pattern/regime context at entry
setup: pullback_to_50sma     # breakout | pullback | base | mean_reversion | none
trend_at_entry: uptrend      # uptrend | downtrend | chop
atr_at_entry: 28.50
iv_percentile_at_entry: 38
rs_vs_spy_at_entry: 1.18
rs_vs_sector_at_entry: 1.05
market_regime: bull_low_vix  # bull_low_vix | bull_high_vix | bear_low_vix | bear_high_vix
days_to_earnings_at_entry: 21

# Execution quality
slippage_entry_bps: 4.2
slippage_exit_bps: 6.1
fill_quality: good           # good | acceptable | poor
broker: ibkr

# Outcome
close_reason: target_hit     # stop_hit | target_hit | signal_exit | discretionary | external
thesis_held: true            # did the original mechanism actually drive the move?
notes: "..."
```

The weekly synthesis aggregates these across `mode: paper` for paper performance and `mode: live` for live performance. When you graduate to live, the same Dataview queries just start returning live data alongside paper.

---

## 8. Scoring & Filtering

**Initial scoring (Paper Phase 1 starting weights):**

| Component | Range | Notes |
|---|---|---|
| Cluster (multiple traders, same ticker, 30d) | 0–3 | +1 per additional independent source after the first. Heavy-movement corroboration adds +0.5 (additive, capped). |
| Committee/sector match | 0–2 | Lawmaker on Armed Services buys defense → +2 |
| Position size relative to trader's typical | 0–2 | Within 90th percentile of their size → +2 |
| Insider buy corroboration (Form 4) | 0–2 | Cluster insider buy in same 30d window |
| Options flow corroboration | 0–2 | Unusual call sweeps in same direction in 14d |
| Trader's own track record | -1 to +2 | Long-term win rate, applied as multiplier or bonus |
| Public statement alignment | 0–1 | Trader on TV/letters confirming thesis |
| **Trend regime alignment** | -1 to +1 | Long signal in uptrend +1; long signal in downtrend -1 |
| **Relative strength** | 0–1 | RS vs. sector > 1.0 → +1 |
| **News/sentiment (Phase 2 only)** | -2 to +1 | Negative news 48h: -2; corroborating positive news: +1 |
| **Regime overlay** | -1 to +1 | Set per-source per-regime; e.g., insider buys do better in drawdowns: +1 when SPY -10% off highs |

Threshold for `#high-score`: 5.0 (Phase 1) — tune from data in Phase 2.
Threshold for "ACT TODAY": 7.0 (Phase 1).
Urgent threshold for intraday: 8.0.

**Heavy-movement signals (`source = MARKET_MOVEMENT`) are special-cased.** In isolation they score `0.0`. If — and only if — there is a non-MARKET_MOVEMENT signal on the same ticker in the trailing 14 days, the market-movement signal contributes through the `flow_corroboration_market` slot using the same weight as Unusual Whales options flow. The cluster scorer also adds a flat +0.5 to a fundamental signal when any matching market-movement signal exists in the trailing window (additive, not multiplicative). This is the "George & Hwang corroboration" path. The hard rule: market movement never originates a draft on its own.

**Hard gates from §5.7 are applied after scoring.** A signal can have a score of 9 and still be blocked by the earnings-proximity gate. Blocked signals are logged with their score and the gate that blocked them, so the weekly synthesis can validate the gates.

All thresholds and weights live in `99 Meta/scoring-rules.md`. Every change goes through the proposed → adopted flow in §5.5 — and proposals are validated by the mini-backtester replay (§5.5) before adoption.

---

## 9. Migration Criteria — Paper to Live

The weekly synthesis checks these every Sunday. **All** must be true. (Restated from §4.2 for the casual reader.)

- [ ] At least 90 calendar days in Paper Phase 2.
- [ ] At least 50 closed trades (across paper).
- [ ] Sharpe ratio > 1.0 over the full Phase 2 period.
- [ ] Deflated Sharpe > 0.95.
- [ ] Profit factor > 1.5.
- [ ] Win rate stable: standard deviation of win-rate across 4 consecutive 2-week buckets < 10 percentage points.
- [ ] Average excess return vs. SPY > 0 over Phase 2.
- [ ] Max drawdown < 15% of starting paper equity.
- [ ] No more than 1 operational incident in the last 30 days.
- [ ] At least 3 of the tracked signal types each have ≥ 10 closed trades.
- [ ] Weekly synthesis has adopted at least 2 rule refinements via the replay-validated flow.
- [ ] All four v0.2 safety changes have triggered at least once in paper (GTC stop fired overnight, idempotency skip, orphan reconciled, daily kill switch tripped).

> [!warning] Don't game the criteria
> If you find yourself tweaking a stop level to nudge the Sharpe over 1.0 in week 12, you've already failed the test. The criteria are guardrails for *you*, not for the system.

---

## 10. Risk Management (Live)

These bind from Live Phase 1 onward. Paper has no real risk but you should still simulate them so the muscle memory exists.

| Limit | Live Phase 1 | Live Phase 2 |
|---|---|---|
| Per-trade risk | 1% of equity | 2% |
| Per-position size cap (half-Kelly) | 3% of equity | 5% |
| Per-sector concentration | 15% | 25% |
| Open positions max | 8 | 15 |
| Daily loss kill-switch | -2% of equity | -3% |
| Weekly loss → revert phase | -5% | -7% |
| Max correlated exposure | 0.7 portfolio beta | 1.0 |

The position-size cap is enforced by the half-Kelly logic in §5.9 — it binds whenever `qty_by_concentration < qty_by_risk`, which happens on tight-stop trades. The sector concentration limit is enforced by the gate in §5.7. The daily loss kill switch is enforced by the gate (which reads the live-monitor-populated `DailyState`, see §5.6); on trip, new entries are refused until next session and Cowork still does its analysis runs but flags everything as `acted: false` in the position frontmatter.

**Backstop:** also set IBKR account-level risk limits via TWS → Configure → Account → Risk Limits → Daily Loss = 2% of NLV. This is broker-side and fires even if the bot is misconfigured.

---

## 11. Build Order

Don't try to build everything in week one. The order matters because each step de-risks the next.

**Week 1 — Skeleton.**
Vault structure, frontmatter templates, Discord webhooks, one ingestor (Capitol Trades or SEC Form 4), backend writes to `00 Inbox/`. **Add the earnings calendar ingestor and OHLCV fetcher in this same week** — they're cheap and needed by the gates. Daily git backup of vault. End-to-end signal flowing into the vault and into Discord. No Cowork yet.

**Week 2 — Cowork morning run + indicator layer.**
Backend computes indicators (§5.8) and writes them to watchlist frontmatter. Morning heavy run consumes them. Generate hypothetical game plans without placing orders. Build feel for prompt quality.

**Week 3 — IBKR paper integration + safety scaffold + one-click confirm + live monitor + hard gates.**
Bracket order construction with GTC children, draft order writing, Discord `/confirm` slash command, signed deep-link mobile UI. Live monitor with idempotent `_on_entry_fill`, startup reconciliation, and the GTC-audit reconciler. Hard gates from §5.7 wired in including the sector-correlation gate. Daily P&L worker populating `DailyState`. SPY baseline portfolio starts tracking. **End of Paper Phase 1 starts here.**

**Weeks 4–6 — Add ingestors, heavy-movement, and tune.**
13F (WhaleWisdom or 13F.info), more Form 4 coverage, options flow if you have UW, X list. Heavy-movement ingestor running. Add the intraday light runs. Tune scoring weights manually as you see signal quality. Hit the Phase 1 graduation criteria (§4.1).

**Weeks 7–18 — Paper Phase 2.**
Weekly synthesis runs. Mini-backtester replay validates each proposed weight change. Don't add new sources unless one is clearly missing. Add news/sentiment modifier mid-Phase 2 if a clear gap shows up in the data. Focus on rule refinements via the proposed/adopted loop. Hit migration criteria (§4.2).

**Weeks 19+ — Live Phase 1, then 2.**
Flip `MODE=live` and `IBKR_PORT=4001`. Keep paper running in parallel for 30 days minimum. Compare paper vs. live Sharpe weekly. Hit §4.3.

> [!tip] Resist mid-build scope creep
> The temptation to add "just one more source" or "just one more rule" before Phase 2 is enormous and almost always wrong. The system needs *closed trade data* to learn, and you only get that by running it as-is for 90+ days.

---

## 12. Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Cowork run fails silently (laptop asleep) | Backend pings `#high-score` if no run output appears in `05 Daily/` within 30 min of scheduled time |
| Backend crashes overnight | systemd auto-restart + healthcheck endpoint + Discord ping on restart |
| Vault sync conflict | Inbox-then-process pattern (invariant 4) prevents this by design; if it ever happens, Cowork's prompt rejects the run and pings you |
| Vault corruption / lost | Daily `git commit` of the vault to a private repo via cron; full recovery by clone |
| IB Gateway nightly logout (23:45–00:45 ET) | IBC handles auto-relogin; `LiveMonitor` tolerates ~5min disconnect; connection retry with exponential backoff on reconnect |
| IBKR API outage during live monitor | Live monitor caches last known prices, refuses to open new positions, alerts to `#system-health`; existing stops handled by GTC OCO at the broker |
| Worker crashed between `placeOrder` and Position write | Startup reconciliation (§5.6, invariant 7) catches the orphan and posts an alert |
| Position closed externally (mobile app) | Startup reconciliation marks DB row `status=CLOSED, close_reason=EXTERNAL` |
| WS handler + polling reconciler race on same fill | Idempotency check at `_on_entry_fill` + `UNIQUE(broker_order_id)` constraint (defense in depth) |
| Bracket child order missing GTC | Reconciler audit (every 30s) re-issues as GTC + `outsideRth=True` and posts a `#system-health` alert per repair |
| Mode/port misconfigured (paper code → live port) | Pydantic `model_validator` refuses to boot; system never connects to the wrong endpoint |
| Signal source goes quiet (scraper breaks) | Per-source heartbeat; if no signals from source X in N hours during market hours, Discord alert |
| Scoring drift (rules change too fast) | Hard-cap: max one weight change per weekly synthesis. Mini-backtester replay must show out-of-sample Sharpe ≥ baseline before adopt. Changelog reviewed before any live phase advance |
| Overfitting to paper | The 30%-Sharpe-degradation guardrail in §4 catches this; if breached, drop back to Live Phase 1 sizing and re-run paper for 4 weeks |
| Stale indicator cache | Backend stamps every indicator block with `computed_at`; Cowork prompt rejects watchlist entries whose indicators are >24h old and re-fetches |
| Stale draft order never confirmed | Drafts auto-expire at end of regular session; Discord summary lists what was skipped vs. expired |
| Travel / timezone confusion | Backend and all scheduled tasks anchor to ET regardless of laptop TZ; phone reminders sent in your local TZ separately |
| Earnings/event calendar miss | Gates fail-closed — if the earnings API call fails, the gate blocks the trade rather than passing it. Manual override available |
| Tight-stop pathological sizing | Half-Kelly per-position cap (§5.9) bounds position to 5% of equity regardless of how tight the stop is |
| Single-sector concentration | Sector-correlation gate (§5.7) blocks at 25% of equity per sector |

---

## 13. Out of Scope (for now)

- Crypto. Different infra, different exchanges, different signal sources.
- Options trading by you (you can still *use* options flow as a signal). Options sizing/Greeks add a whole risk layer.
- International equities. ADRs are fine; native foreign listings aren't.
- Selling signals or letting others trade off your system.
- Tax optimization (wash sales, lot accounting). Real concern for live but a separate workstream.
- Tier-2 (software) hard-stop monitor. Bedcrock's broker-side-OCO-only design is intentional — no double-sell race possible. Don't add complexity that has to be defended against itself.
- Inline `anthropic` SDK call per signal. Breaks the Cowork-via-vault decoupling. The four-times-daily Cowork cadence already covers the latency need.
- ARK as a primary signal source. Research found -34% MWR 2020–2025; not worth weighting.

---

## 14. Caveats

- Past returns of any tracked trader do not predict their future returns. Survivorship bias is rampant in published "copy Buffett" results.
- The 45-day disclosure delay for STOCK Act and 13F is a structural problem. The system mitigates it via faster corroborating signals and theme-trading, not by pretending the delay isn't there.
- Paper-trading P&L is *always* better than live. Plan for it.
- IBKR is the broker for both paper and live. Operationally that means living with IB Gateway's desktop-app heritage (IBC, Xvfb, nightly logout). Documented in `docs/DEPLOYMENT.md`; tolerable but not invisible.
- `yfinance` (the OHLCV fallback when Polygon is unavailable) overstates returns for speculative-universe backtests by 1–4%/yr because delisted symbols disappear. Acceptable for *live* indicator computation; if a future v0.3 builds a primary backtester it should use Norgate or CRSP instead.
- The mini-backtester (§5.5) uses historical OHLCV only — no bid/ask depth, constant slippage, no survivorship correction. Treat its ADOPT/REJECT/INCONCLUSIVE recommendation as advisory input to the human-confirmed weekly synthesis, not as a decision oracle.
- This document is a starting point. Treat `99 Meta/changelog.md` as the canonical record once the system is running — this plan is just the bootstrap.

---

## Appendix A — Dataview Queries

Drop these into a dashboard note (`Dashboard.md`) for live system state.

**Active high-score watchlist:**
````dataview
TABLE ticker, score, trader, disclosed_at
FROM "00 Inbox"
WHERE type = "signal" AND status = "new" AND score >= 5
SORT score DESC
````

**Open paper positions:**
````dataview
TABLE ticker, entry_date, entry_price, stop, target
FROM "02 Open Positions"
WHERE status = "open" AND mode = "paper"
SORT entry_date DESC
````

**Last 30 days closed paper trades:**
````dataview
TABLE ticker, pnl_pct, excess_vs_spy_pct, close_reason, holding_days
FROM "03 Closed"
WHERE mode = "paper" AND exit_date >= date(today) - dur(30 days)
SORT exit_date DESC
````

**Per-trader paper performance:**
````dataview
TABLE
  length(rows) as trades,
  round(sum(rows.pnl_pct) / length(rows), 2) as avg_pnl,
  round(sum(rows.excess_vs_spy_pct) / length(rows), 2) as avg_excess
FROM "03 Closed"
WHERE mode = "paper"
GROUP BY trader_primary
SORT avg_excess DESC
````

---

## Appendix B — File Naming Conventions

- Inbox signals: `00 Inbox/YYYY-MM-DD-TICKER-source.md` (e.g. `2026-05-02-NVDA-pelosi.md`)
- Watchlist: `01 Watchlist/TICKER.md` (one per ticker, gets updated)
- Open positions: `02 Open Positions/TICKER-YYYY-MM-DD.md`
- Closed: `03 Closed/YYYY-MM-DD-TICKER.md` (date is exit date)
- Daily: `05 Daily/YYYY-MM-DD-{regime|gameplan|intraday}.md`
- Weekly: `06 Weekly/YYYY-MM-DD-synthesis.md` and `06 Weekly/YYYY-MM-DD-replay-{rule}.md`

Stable names matter because the Cowork prompts reference paths directly. Don't rename folders without updating prompts and Dataview queries.

---

## Appendix C — Version history

**v0.1 (2026-05-03) — pre-IBKR-migration baseline.** Initial spec for a vault-driven swing-trading system. Targeted Alpaca paper → Alpaca live as the broker path with IBKR reserved for $250k+ books. Six hard gates, nine-component scorer, four Cowork prompts, inbox-then-process write discipline, server-side OCO. The plan shipped as code in v0.1 and ran cleanly enough to surface the audit findings below. Recoverable from git history at the original `bedcrock-plan.md` commit.

**v0.2.0 (2026-05-10) — audit response + selective Proxy Bot port-overs.** Driven by the [2026-05-10 code audit](docs/AUDIT_2026-05-10.md) (which surfaced six blockers in v0.1 code) and a parallel research pass on a competing "Proxy Bot" design (which surfaced four transferable ideas worth folding in). Landed on `v2-staging` over four parallel waves and merged to `main`. See [`docs/AUDIT.md`](docs/AUDIT.md) for the per-item status table with commit hashes.

Six audit fixes:

- **F1 — `ib_insync` → `ib_async==2.1.0`** migration (commit `90ec1ad`). The old library was abandoned in 2024; the maintained successor is pure-asyncio and lets us delete the `_keep_alive` event-loop bridging in `monitor.py`.
- **F2 — GTC + `outsideRth=True` on bracket children** (commit `de5eaa0`). The default DAY TIF on stop/take-profit children meant a position entered at 15:59 ET had its broker-side stop expire one minute later. Plus a reconciler audit that re-issues any non-conforming child found on the wire.
- **F3 — Idempotency on `_on_entry_fill` + `UNIQUE(Position.broker_order_id)`** (commit `f324062`). The websocket fill handler and the polling reconciler could both create a Position row for the same broker order. Idempotency check at the boundary, unique constraint as defense in depth.
- **F4 — Startup reconciliation against IBKR** (in commit `de5eaa0`). On `LiveMonitor.start()`, reconcile the DB against IBKR's view of positions; alert on orphans, mark stale-DB rows as closed-externally. Closes the "worker crashed between `placeOrder` and DB write" failure.
- **F5 — `daily_pnl_pct` wired end-to-end → daily kill switch actually trips** (commit `071a82d`). v0.1 had the gate but never populated its input; v0.2 adds a `daily_pnl` worker and a `DailyState` table.
- **F6 — Connection retry with exponential backoff + IBC + nightly-logout docs** (in commit `de5eaa0` and Wave D docs). IB Gateway forces a logout window 23:45–00:45 ET; IBC handles auto-relogin. Retry is 5 attempts with 1/2/4/8/16-second backoff and a terminal alert.

Four Proxy Bot ports:

- **N1 — Heavy-movement ingestor** (commit `3a2b658`). Volume-spike + 52w-breakout + gap detector running every 5 min during market hours. Strict corroboration-only: never originates a draft, only adds points to existing fundamental signals on the same ticker. George & Hwang (2004) is the empirical basis.
- **N2 — Concrete sector-correlation gate** (commit `2fd9354`). v1 §10 mandated it; v1 shipped a stub that returned unconditional `blocked=False`. The real implementation maps tickers to sector ETFs and blocks at 25% sector concentration.
- **N3 — Half-Kelly per-position cap** (in commit `2fd9354`). Defends against pathological tight-stop sizing where 1%-risk becomes 20%-of-equity exposure. `qty = min(qty_by_risk, equity × 0.05 / entry)`.
- **N4 — Mini-backtester for scoring-rule evaluation** (commit `7c2955c`). Re-scores past Signals under a proposed weight set; reserves the last 30 days as out-of-sample; refuses to recommend ADOPT unless out-of-sample Sharpe beats baseline. Hooked into the weekly synthesis via an EOD worker.

Three new invariants (now folded into §2 as 7, 8, 9):

- **Broker truth wins on conflict** — startup/reconnect reconciliation uses IBKR as source of truth.
- **Stops are GTC by construction** — no code path may submit a non-GTC child; the reconciler audit re-issues any found on the wire.
- **Mode and port are coupled** — Pydantic `model_validator` refuses to boot a paper config on a live port or vice versa.

**Explicitly NOT ported from Proxy Bot v3:**

- Inline `anthropic` SDK call per signal — breaks the Cowork-via-vault decoupling and the vault-as-source-of-truth invariant.
- Tier-2 software hard-stop monitor — bedcrock's broker-side-OCO-only design is *safer* (no double-sell race possible).
- Convergence-multiplier scoring — bedcrock's additive 9-component scorer is more flexible.
- SQLite, Telegram, ARK-as-primary-signal.

The previous delta document `bedcrock-plan-v2.md` was deleted during the v0.2.0 → spec consolidation; it is recoverable from git history at commit `1938275` (where v2 was added) if the diff-style record is ever needed.
