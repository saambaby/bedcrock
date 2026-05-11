---
title: Bedcrock — Build & Migration Plan
status: active
version: v3
phase: paper-1
created: 2026-05-03
updated: 2026-05-10
implemented: 2026-05-10
shipped_as: v0.3.0
tags: [trading/system, plan]
---

# Bedcrock

A signal-aggregation and analysis system that watches politicians, hedge fund titans, insiders, and options whales; scores trade ideas; runs deep Claude analysis on a schedule; paper-trades them through a broker; and graduates to live capital only after empirical validation.

> [!warning] Not financial advice
> This is a personal research/decision-support system. It is not a registered advisory service. Do not let other people's money ride on it without consulting a securities lawyer in your jurisdiction.

> [!info] Document version
> This is the canonical, self-contained spec. It folds in everything that landed in v0.3.0 (drop the Obsidian vault and Cowork prompts; reasoning moves to Claude Code skills + cloud-hosted Routines). For the full evolution from v0.1 → v0.2 → v0.3, see Appendix C — Version history.

---

## 1. Goals & Non-Goals

**Goals**
- Surface high-signal trade ideas from delayed but reliable disclosures (STOCK Act, 13F, Form 4) and faster sources (options flow, public statements).
- Use Claude (via cloud-hosted Claude Code Routines) for the heavy reasoning layer four-plus times per day, with always-on infrastructure handling the parts Claude can't.
- Build paper-trading data with the **exact same schema** as live, so the live switch is a config flip, not a rewrite.
- Generate a feedback loop (closure post-mortems, weekly synthesis) that improves the scoring rules over time.

**Non-Goals**
- High-frequency or intraday scalping. The system's edge is days-to-weeks, not seconds.
- Fully autonomous execution. **Human one-click review on every entry is a permanent design feature, not a phase-limited safety wheel.** The bot prepares the order; you confirm it.
- Selling signals to other people. Out of scope; regulatory rabbit hole.

---

## 2. Architectural Principles

These are the invariants. Every component has to respect them or migration breaks.

1. **Paper and live share one data path.** The only difference between paper and live is the broker endpoint and a `mode: paper|live` flag. Everything else — DB schemas, Discord channels, Claude Code skills, scoring — is identical.
2. **Three-layer authority.** The broker (IBKR) is the source of truth for live positions and open orders — it survives bot/VPS/network death. The DB (Postgres) is operational truth for everything else: signals, drafts, indicators, audit log, daily state, scoring proposals, replay reports. The Claude Code reasoning layer consumes the DB via a FastAPI read layer; it does not have its own persistence. On conflict between layers, broker beats DB beats reasoning.
3. **DB is the only durable store.** Claude Code Routines (cloud-hosted, scheduled) read from FastAPI read endpoints, reason, and write back through dedicated POST endpoints (e.g. `/scoring-proposals`). There is no file-based IPC bus.
4. **Reasoning is stateless and replayable.** Routines hold no state between runs; every run starts from a fresh dashboard fetch. Re-running a skill on the same DB snapshot produces the same conclusions modulo Claude's sampling.
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

    subgraph DB [Postgres]
        D1[signals]
        D2[positions]
        D3[indicators]
        D4[daily_state]
        D5[audit_log]
        D6[scoring_proposals]
        D7[scoring_replay_reports]
    end

    subgraph API [FastAPI dashboard endpoints]
        E1[/dashboard/morning]
        E2[/dashboard/intraday]
        E3[/dashboard/closures]
        E4[/dashboard/weekly]
        E5[/dashboard/status]
        E6[POST /scoring-proposals]
    end

    subgraph Routines [Claude Code Routines — Anthropic infrastructure, cloud]
        R1[morning-analyze<br/>06:30 ET weekdays]
        R2[intraday-check<br/>12:00 + 14:00 ET]
        R3[hourly-closure<br/>10:00–16:00 ET]
        R4[weekly-synthesis<br/>Sun 19:00 ET]
        R5[status<br/>on-demand]
    end

    Discord[Discord<br/>#signals-firehose<br/>#high-score<br/>#position-alerts<br/>#system-health]
    Human((You<br/>one-click confirm))

    Sources --> B1 --> B2 --> D1
    A6 --> B5 --> D3
    A7 -- "corroboration only" --> B2
    B2 --> Discord
    B3 --> D2
    B3 --> Discord
    B4 <--> B3
    B6 <--> B4
    DB --> API --> Routines
    Routines --> Discord
    Routines -- "POST proposals" --> E6 --> D6
    Routines -- "ACT TODAY list" --> Human
    Human -- "click confirm" --> B4
    B4 -- "fill confirmation" --> D2
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
- [ ] Max drawdown stayed within the configured Live Phase 1 limits (see §10)
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
- `pg_dump` cron job for nightly Postgres backup to a private object store
- **IBC** (https://github.com/IbcAlpha/IBC) + Xvfb in a `gnzsnz/ib-gateway-docker`-style container for IB Gateway lifecycle. IBKR forces a logout window 23:45–00:45 ET nightly and again Sunday for re-auth; IBC handles auto-relogin. Configure `AutoRestartTime=23:45` (time-based, not token-based — token AutoRestart is broken on unfunded paper accounts per IBC issue #345). The `LiveMonitor` tolerates a ~5-min disconnect without paging. `IBKRBroker.connect()` retries with exponential backoff (5 attempts: 1s, 2s, 4s, 8s, 16s) and posts a system-health alert on terminal failure.

**Responsibilities:**
- Poll each data source at its appropriate cadence (Form 4: every 15 min during market hours; 13F: daily; politician trades: every 30 min; options flow: websocket; earnings calendar: daily at 06:00 ET; OHLCV: end-of-day + on-demand; heavy-movement: every 5 min during market hours).
- Apply the scoring pipeline (see §8).
- Apply hard gates (see §5.7) before a signal escalates to `#high-score` or `ACT TODAY`.
- Compute and cache the indicator/pattern layer (see §5.8) for every ticker in the watchlist.
- Persist signals, indicators, drafts, positions, and audit rows into Postgres.
- Post to Discord webhooks.
- Run the live monitor (price subscriptions for currently-open positions in the DB).
- Prepare bracket orders for one-click confirmation (see §5.9).
- Talk to IBKR for paper or live fills and position state, depending on `MODE` and `IBKR_PORT`.

### 5.3 Reasoning Surface

The reasoning layer is a small set of Claude Code project skills under `.claude/skills/`. Each skill is a SKILL.md file (YAML frontmatter + prompt body) that fetches data from the FastAPI dashboard endpoints, reasons over it, and posts to Discord. There is no local file IPC; all state flows through Postgres.

| Skill | Path | Purpose |
|---|---|---|
| `morning-analyze` | `.claude/skills/morning-analyze/SKILL.md` | Build the day's gameplan from `/dashboard/morning`; post triage embed to `#high-score`. |
| `intraday-check` | `.claude/skills/intraday-check/SKILL.md` | Light midday review of open positions and watchlist via `/dashboard/intraday`; alert on triggers. |
| `hourly-closure` | `.claude/skills/hourly-closure/SKILL.md` | Process recent fills/closes from `/dashboard/closures`; post post-mortems to `#position-alerts`. |
| `weekly-synthesis` | `.claude/skills/weekly-synthesis/SKILL.md` | Aggregate 7-day stats from `/dashboard/weekly`; if a weight change is supported, POST to `/scoring-proposals`. |
| `status` | `.claude/skills/status/SKILL.md` | On-demand `/status`-style snapshot from `/dashboard/status`. |

**Skill frontmatter** (Claude Code 2026 spec):

```yaml
---
name: morning-analyze
description: <when this skill applies; how it's invoked>
disable-model-invocation: true   # only user- or Routine-invokable
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---
```

**Registration as Routines.** Each skill is registered as a cloud-hosted Routine via `/schedule` from a `claude` session at the bedcrock repo root (e.g. `/schedule "every weekday 06:30 ET, run /morning-analyze"`). Routines run on Anthropic infrastructure — no VPS daemon, no laptop required. Manage them at `claude.ai/code/routines`. The `status` skill is on-demand only; the other four have cron-style schedules listed in §5.5.

**FastAPI read endpoints consumed by the skills:**

- `GET /dashboard/morning` — overnight signals not yet acted on, current open positions, today's earnings calendar, regime snapshot, gates that blocked entries yesterday.
- `GET /dashboard/intraday` — open positions with current P&L vs. trailing-stop distance, active watchlist alerts.
- `GET /dashboard/closures?hours=24` — recent fill events (entries and exits) for post-mortem generation.
- `GET /dashboard/weekly` — 7-day stats: trades by source/trader/sector, win rate, current scoring rules, recent proposals, replay reports.
- `GET /dashboard/status` — P&L summary, open positions count, system health (heartbeats, broker connection state).

**Write path: `POST /scoring-proposals`** — used only by `weekly-synthesis`. Body is a structured proposal `{rule, current_value, proposed_value, rationale, supporting_stats, replay_report_id}`. The endpoint inserts a row into the `scoring_proposals` table (status `pending`); a human reviewer adopts or rejects via a separate workflow. Skills never mutate `scoring_rules` directly — proposed → adopted is gated by replay results and human review (§5.5).

**Auth.** All `/dashboard/*` and `/scoring-proposals` endpoints require `Authorization: Bearer <token>`. The token is signed via `itsdangerous`; each Routine has it set as the `API_BEARER_TOKEN` environment variable (set in the Routine config in `claude.ai/code/routines`, never committed to the repo).

**Per-trade data.** The DB schema for `signals`, `positions`, and the closed-trade view holds the same fields formerly carried in vault frontmatter — see §7 for the full per-trade schema.

> [!tip] Why `mode: paper|live` matters
> Every dashboard query, every skill, every report filters on this field. When you migrate, you don't change anything about how data is stored — paper and live live side-by-side in the same tables and queries pick which to look at. You can run them in parallel: paper continues validating new rule changes while live runs the validated rules.

### 5.4 Discord

Three primary channels plus an ops channel, one webhook each:
- `#signals-firehose` — every raw signal, low signal-to-noise, browse-only.
- `#high-score` — score ≥ threshold, the channel you actually watch.
- `#position-alerts` — entries, exits, stops hit, urgent events.
- `#system-health` — ingestor heartbeats, reconciler repairs, broker reconnect alerts.

A small bot (~80 lines, `discord.py`) handles slash commands:
- `/thesis TICKER` — reads the latest thesis row for the ticker from the DB and posts it.
- `/positions` — lists open positions from the `positions` table.
- `/snooze TICKER 7d` — inserts a snooze row in the `snoozes` table so the scorer ignores that ticker for 7 days.
- `/pnl` — paper-mode equity curve summary.
- `/confirm <id>` and `/skip <id> reason: …` — order confirmation flow (§5.9).
- `/heartbeat` — surfaces per-ingestor heartbeat ages (drives external monitoring).

### 5.5 Claude Code Routines

Four scheduled Routines plus one on-demand skill. Each Routine is registered once via `/schedule` from a `claude` session at the bedcrock repo root; from then on it runs on Anthropic's cloud regardless of laptop state. Routine env vars (`API_BASE_URL`, `API_BEARER_TOKEN`, `DISCORD_WEBHOOK_*`) are configured per-Routine at `claude.ai/code/routines`.

#### Morning Run — weekdays, 06:30 ET (3 hours before US open)

Routine fires the `morning-analyze` skill. The skill:
1. Pulls `/dashboard/morning` for overnight signals, open positions, today's earnings calendar, regime snapshot, and gates that blocked entries yesterday.
2. Tags the day's `market_regime` (`bull_low_vix | bull_high_vix | bear_low_vix | bear_high_vix`) and notes any 48h macro events.
3. For each high-score candidate (score ≥ threshold), confirms its indicators are fresh (`computed_at` ≤ 24h), then drafts a thesis: bull case, bear case, technical levels from the cached indicators, entry zone (stop never tighter than 1.5×ATR_20), invalidation level, profit targets.
4. Posts a single "ACT TODAY / WATCH TODAY / PASSIVE" gameplan embed to `#high-score`. Each ACT TODAY item is a hint to the human; the backend already has the corresponding scored signal in the DB and will draft a bracket order on confirm (§5.9).

#### Intraday Check — weekdays, 12:00 ET and 14:00 ET

Routine fires the `intraday-check` skill. The skill pulls `/dashboard/intraday`:
1. For each open position, checks distance to stop/target and any news in the last 3h. Alerts to `#position-alerts` if anything is approaching its level.
2. Pulls signals scored above `URGENT_THRESHOLD` since the last run; posts a compressed thesis (5–7 sentences) to `#high-score` if any.
3. Stays light — should finish in well under 5 minutes. Does not rebuild the morning thesis or touch closed positions.

#### Hourly Closure — weekdays, top of the hour 10:00–16:00 ET

Routine fires the `hourly-closure` skill. The skill pulls `/dashboard/closures?hours=1`:
1. For each fresh closure event, drafts a post-mortem (entry/exit/pnl, mechanism vs. direction, original signal vs. actual driver, return vs. SPY and sector ETF, one specific lesson).
2. Posts a compact summary to `#position-alerts`. Closure rows in the DB are updated with the post-mortem text and a `processed_at` timestamp so the next run skips them.
3. If a lesson suggests a scoring change, the skill defers the proposal to the weekly run rather than POSTing immediately — keeps the proposal volume manageable and lets weekly evaluate against replay results.

#### Weekly Synthesis — Sunday 19:00 ET

Routine fires the `weekly-synthesis` skill. The skill pulls `/dashboard/weekly` (last 30 days of closes, current scoring rules, pending proposals, recent replay reports):
1. Computes per-trader, per-source, per-sector stats: win rate, avg return, avg excess vs. SPY, avg holding period, Sharpe-like ratio.
2. Identifies top 3 working and top 3 failing patterns.
3. For each weight change supported by both stats AND a passing replay (out-of-sample Sharpe ≥ baseline), POSTs a structured proposal to `/scoring-proposals` (writes a row to the `scoring_proposals` table for human review).
4. Phase-gate check: if all Paper Phase 2 graduation criteria are met, posts a "READY FOR LIVE PHASE 1" note to `#high-score`.
5. Posts a 5-bullet summary to `#high-score`.

#### `status` skill (on-demand)

Not on a schedule. Invoked manually via `/status` in a `claude` session. Pulls `/dashboard/status` and posts a one-screen summary: P&L, open positions count, ingestor heartbeats, broker connection state. Useful for checking the system from a phone via `claude remote-control`.

#### Mini-backtester (N4)

Weekly synthesis depends on a mini-backtester at `src/backtest/replay.py` that re-scores past signals (already in the DB) under a proposed weight set, simulates entries/exits at OHLCV boundaries, and reports the Sharpe delta. This exists because v1's "paper trading IS the backtest" philosophy is defensible for the signal universe (politician trades, insider buys are hard to backtest faithfully) but breaks down for *evaluating weight changes*: with 50 trades, a 1-point change on `cluster_per_extra_source` is statistically indistinguishable from noise.

The replay tool guards against overfitting by reserving the last 30 days of signals as out-of-sample, reporting both in-sample and out-of-sample Sharpe, and refusing to recommend ADOPT unless out-of-sample Sharpe beats the baseline. Entry rule is T+1 OPEN, exit is OCO at a configurable stop loss (default 10%) or target (default 1.5R) or timeout (default 30 days). Slippage is a flat-bps assumption. The output is a `ReplayReport` row written to the `scoring_replay_reports` table by the EOD worker, with an ADOPT / REJECT / INCONCLUSIVE recommendation that the weekly-synthesis skill uses as one input — never the sole input.

Caveats baked into the module: historical OHLCV only (no bid/ask depth), constant slippage, no survivorship-bias correction (yfinance acknowledged). It's a sanity check, not a Monte Carlo. Treat the recommendation as advisory.

### 5.6 Live Monitor

A long-running Python process on the VPS:

- Subscribes to IBKR's `orderStatusEvent` and `execDetailsEvent` for instant fill notifications. Polling fallback every 30s catches missed events.
- **Stops and targets are server-side at the broker as OCO bracket orders, not enforced by this monitor.** The monitor *observes*; the broker *enforces*. If your VPS dies overnight, your stops still fire (because they're GTC by construction — see invariant 8).
- **Idempotency by construction.** `_on_entry_fill` first checks `SELECT Position WHERE broker_order_id = ?`; if a row already exists (because the WS handler beat the polling reconciler, or vice versa), it logs and returns instead of inserting a duplicate. Defense in depth: `Position.broker_order_id` has a `UNIQUE` constraint at the DB layer, so a second insert would error rather than corrupt.
- **Startup reconciliation against IBKR (invariant 7).** On `LiveMonitor.start()`, after broker connect and before the event loop begins, runs `_reconcile_against_broker`: any IBKR position not in the DB raises an `orphan_ibkr_position` alert and an `AuditLog` row; any open DB position the broker has no record of is marked `status=CLOSED, close_reason=EXTERNAL`. This catches the "worker crashed between `placeOrder` and writing the Position row" failure mode and the "user closed via mobile app" case.
- **Reconciler audit (invariant 8).** Every 30s, walks `ib.openTrades()` and re-issues any child order with `tif != "GTC"` as GTC + `outsideRth=True`. Posts a `#system-health` alert per repair.
- On every tick, updates the position row with the current price and unrealized P&L (so the dashboard endpoints are always live).
- On stop or target fill received from broker, writes a closure event row, transitions the position to `status=CLOSED`, and posts to `#position-alerts`. The next `hourly-closure` Routine picks up the closure row and writes the post-mortem.
- Re-queries the open positions list every 30 seconds so newly-opened positions get monitored automatically.
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
| **Snoozed ticker** | Ticker has an active row in the `snoozes` table | Lift snooze in Discord (`/snooze TICKER 0`) |
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

These are written into the `indicators` table per ticker, so the morning skill sees them as structured data via `/dashboard/morning`, not raw OHLCV:

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

**What the morning skill does with them.** The skill asks Claude to interpret these in context: is the entry zone a sensible level given the swing structure, does the 1.5×ATR stop give the trade room without risking too much, is the relative strength supportive of the thesis. Claude *interprets*; it doesn't compute. The numbers come from the backend.

**What's deliberately excluded.** Candlestick pattern catalogs, Fibonacci retracements, Elliott Wave, multi-indicator crossover systems. These have weak empirical support as standalone alpha and would dilute the system's actual edge (the signal cluster). Resist adding them.

**Setup tagging.** The closure post-mortem records which setup type was active at entry (`breakout`, `pullback`, `base_breakout`, `mean_reversion`, `none`). After ~30 closed trades the weekly synthesis can answer "do my breakout entries outperform my pullback entries on the same fundamental signals?" — which is the actually useful pattern question.

### 5.9 Order Execution — One-Click Confirm Flow

The execution layer is deliberately split so the human is the trigger, but never has to babysit risk management once the order is live.

**Step-by-step:**

1. **Morning Routine** (`morning-analyze`) posts the gameplan to `#high-score` with one Discord embed per ACT TODAY candidate.
2. **Backend prepares draft bracket orders.** For every scored signal that the gameplan tags ACT TODAY, the backend constructs a complete bracket order in memory (limit entry, OCO stop and target attached, calculated quantity, all gate checks passed) and writes a `DraftOrder` row in the DB with status `draft`. It does *not* send it to the broker yet.
3. **Discord prompt to you.** The `#high-score` embed for each candidate has the full order preview (entry, stop, target, size, %risk, gate results) and a clear path to confirm. Two ways to confirm:
   - **Slash command in Discord:** `/confirm <id>` — bot reads the draft from the DB, sends the bracket to the broker, updates the row to `status: sent`.
   - **Tap-friendly mobile:** the embed includes a deep link `claude-trade://confirm/<id>` (an `itsdangerous`-signed token, expires with the draft) that opens a tiny local web UI showing the order, with a single "Send to broker" button. Best on phone — tap it from bed at 8:15 ET, done.
4. **Broker fills**, `execDetailsEvent` fires, live monitor catches it (idempotent — see §5.6), position file written, `#position-alerts` ping. From this moment on, the OCO at the broker (GTC by construction) manages the exit.
5. **Skipping a trade is also one click.** `/skip <id> reason: earnings-too-close` updates the draft row to `status: skipped` with the reason. Skipped orders feed the weekly synthesis the same as taken ones — "would I have made money on the trades I skipped?" is one of the most useful questions the system can answer.

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
- Trail or move stops *only if* the position row's `auto_trail` flag is true and the rule is configured (e.g., "move stop to break-even after +1R"). Default off; you turn it on per-position.
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

1. The morning Routine's gameplan lists the candidate.
2. Backend constructs the full bracket draft, runs all gates, writes it to the `draft_orders` table as `status: draft`. Discord posts the preview embed to `#high-score`.
3. **You click confirm** — `/confirm <id>` in Discord, or tap the deep link to the local web UI. One action, no fields to fill.
4. Backend sends the bracket to IBKR. `client_order_id` is set to the `DraftOrder.id` UUID, so a duplicate confirm is rejected by the broker.
5. IBKR fills the entry; OCO sits server-side and will fire on stop or target *even if your VPS is offline* (because GTC).
6. Live monitor observes the fill via `execDetailsEvent` (idempotent), writes the position file, posts to `#position-alerts`.

### 6.3 Slippage Model

Don't take mid-price as your fill in paper. IBKR's paper sim is mildly optimistic; layer on top of it:

- Limit orders: fill only if price *crosses through* your limit by at least 1bp during the bar; assume 50% fill probability if it just touches.
- Market orders (used only for closes if OCO doesn't fire): fill at ask + 5bps (buy) or bid - 5bps (sell), plus 2bps for assumed market impact on size > $5k.
- For tickers with 30-day ADV below 500k shares: halve simulated fill size and add 10bps slippage.

Adjust these constants in the risk-limits config after Live Phase 1 based on observed slippage. The point is to make paper *pessimistic*, not optimistic — you want live results to surprise you positively, not the other way around.

### 6.4 Equity Tracking

Starting paper equity: **$100k**. Don't match it to your intended live size — you're testing the strategy, not the size. Sizing scales with equity in both modes via the `risk-limits.md` percentages.

Backend writes daily equity snapshots to the `EquitySnapshot` DB table:

```csv
date,mode,equity,cash,positions_value,daily_pnl,daily_pnl_pct
2026-05-03,paper,100000.00,100000.00,0.00,0.00,0.00
2026-05-03,live,25000.00,25000.00,0.00,0.00,0.00
```

Sharpe, drawdown, and phase-gate calculations read from this table. Once live is running, paper continues in parallel for at least 30 days so you have a head-to-head comparison.

### 6.5 The "Do Nothing" Baseline

Run a parallel paper portfolio that just buys SPY equal-weight every time the system would have entered. Same sizing, same hold periods, same exits driven by SPY's behavior on those dates. Track its equity curve in `EquitySnapshot` with `mode: baseline`. If your system's risk-adjusted return doesn't beat this baseline net of stress and effort, that's important to know early — and you'll only know if you tracked it from day one.

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

The weekly synthesis aggregates these across `mode: paper` for paper performance and `mode: live` for live performance. When you graduate to live, the same `/dashboard/weekly` queries just start returning live data alongside paper.

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

All thresholds and weights live in the `scoring_rules` table (seeded from `config/scoring-rules.yaml`). Every change goes through the proposed → adopted flow in §5.5 — proposals are written to `scoring_proposals` by the weekly skill, validated by the mini-backtester replay (§5.5), and human-reviewed before being applied to `scoring_rules`.

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

The position-size cap is enforced by the half-Kelly logic in §5.9 — it binds whenever `qty_by_concentration < qty_by_risk`, which happens on tight-stop trades. The sector concentration limit is enforced by the gate in §5.7. The daily loss kill switch is enforced by the gate (which reads the live-monitor-populated `DailyState`, see §5.6); on trip, new entries are refused until next session and the Routines still run but the dashboard endpoints flag the day as `acted: false`.

**Backstop:** also set IBKR account-level risk limits via TWS → Configure → Account → Risk Limits → Daily Loss = 2% of NLV. This is broker-side and fires even if the bot is misconfigured.

---

## 11. Build Order

Don't try to build everything in week one. The order matters because each step de-risks the next.

**Week 1 — Skeleton.**
Postgres schema + alembic, Discord webhooks, one ingestor (Capitol Trades or SEC Form 4), backend persists scored signals to the DB and posts to `#signals-firehose`. **Add the earnings calendar ingestor and OHLCV fetcher in this same week** — they're cheap and needed by the gates. Nightly `pg_dump` to private object store. End-to-end signal flowing into the DB and into Discord. No reasoning layer yet.

**Week 2 — FastAPI dashboard + Claude Code skills + first Routine.**
Backend computes indicators (§5.8) and writes them to the `indicators` table. Stand up the `/dashboard/*` read endpoints (§5.3) behind bearer auth. Author the `morning-analyze` and `status` skills under `.claude/skills/`. Register the morning Routine via `/schedule` from a `claude` session at the repo root. Generate hypothetical gameplans without placing orders. Build feel for skill output quality.

**Week 3 — IBKR paper integration + safety scaffold + one-click confirm + live monitor + hard gates.**
Bracket order construction with GTC children, draft order writing, Discord `/confirm` slash command, signed deep-link mobile UI. Live monitor with idempotent `_on_entry_fill`, startup reconciliation, and the GTC-audit reconciler. Hard gates from §5.7 wired in including the sector-correlation gate. Daily P&L worker populating `DailyState`. SPY baseline portfolio starts tracking. **End of Paper Phase 1 starts here.**

**Weeks 4–6 — Add ingestors, heavy-movement, remaining Routines, and tune.**
13F (WhaleWisdom or 13F.info), more Form 4 coverage, options flow if you have UW, X list. Heavy-movement ingestor running. Author and register the `intraday-check` and `hourly-closure` skills as Routines. Tune scoring weights manually as you see signal quality. Hit the Phase 1 graduation criteria (§4.1).

**Weeks 7–18 — Paper Phase 2.**
Register the `weekly-synthesis` Routine; it POSTs scoring proposals to `/scoring-proposals`. Mini-backtester replay validates each proposed weight change. Don't add new sources unless one is clearly missing. Add news/sentiment modifier mid-Phase 2 if a clear gap shows up in the data. Focus on rule refinements via the proposed/adopted loop. Hit migration criteria (§4.2).

**Weeks 19+ — Live Phase 1, then 2.**
Flip `MODE=live` and `IBKR_PORT=4001`. Keep paper running in parallel for 30 days minimum. Compare paper vs. live Sharpe weekly. Hit §4.3.

> [!tip] Resist mid-build scope creep
> The temptation to add "just one more source" or "just one more rule" before Phase 2 is enormous and almost always wrong. The system needs *closed trade data* to learn, and you only get that by running it as-is for 90+ days.

---

## 12. Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Routine fails silently | Each skill's first action is to mark a `routine_runs` row at start; the backend pings `#system-health` if no end-marker appears within the expected window of a scheduled Routine. Routine logs are also viewable at `claude.ai/code/routines`. |
| Backend crashes overnight | systemd auto-restart + healthcheck endpoint + Discord ping on restart |
| DB corruption / lost | Nightly `pg_dump` to a private object store; restore is a single `pg_restore`. |
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
| Stale indicator cache | Backend stamps every indicator row with `computed_at`; the morning skill rejects watchlist entries whose indicators are >24h old and triggers a re-fetch via the backend. |
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
- Inline `anthropic` SDK call per signal. Breaks the Routines-via-FastAPI decoupling. The four-times-daily Routine cadence already covers the latency need.
- ARK as a primary signal source. Research found -34% MWR 2020–2025; not worth weighting.

---

## 14. Caveats

- Past returns of any tracked trader do not predict their future returns. Survivorship bias is rampant in published "copy Buffett" results.
- The 45-day disclosure delay for STOCK Act and 13F is a structural problem. The system mitigates it via faster corroborating signals and theme-trading, not by pretending the delay isn't there.
- Paper-trading P&L is *always* better than live. Plan for it.
- IBKR is the broker for both paper and live. Operationally that means living with IB Gateway's desktop-app heritage (IBC, Xvfb, nightly logout). Documented in `docs/DEPLOYMENT.md`; tolerable but not invisible.
- `yfinance` (the OHLCV fallback when Polygon is unavailable) overstates returns for speculative-universe backtests by 1–4%/yr because delisted symbols disappear. Acceptable for *live* indicator computation; if a future v0.3 builds a primary backtester it should use Norgate or CRSP instead.
- The mini-backtester (§5.5) uses historical OHLCV only — no bid/ask depth, constant slippage, no survivorship correction. Treat its ADOPT/REJECT/INCONCLUSIVE recommendation as advisory input to the human-confirmed weekly synthesis, not as a decision oracle.
- This document is a starting point. Treat the `scoring_rules` history rows + git log as the canonical record once the system is running — this plan is just the bootstrap.

---

## Appendix A — Dashboards

v0.3.0 dropped the Obsidian vault. Equivalent dashboards are exposed via the FastAPI `/dashboard/*` endpoints (§5.3); build a custom dashboard against those endpoints if a visual surface is needed beyond Discord and the Claude Code skills' summaries. The endpoints return JSON suitable for any frontend (Streamlit, Grafana via JSON API, a static SPA, or just `curl | jq` from a terminal).

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

- Inline `anthropic` SDK call per signal — breaks the reasoning-via-Routines decoupling. (At v0.2 the framing was "breaks the Cowork-via-vault decoupling"; v0.3 dropped the vault but the architectural objection is the same: ingestors stay deterministic, reasoning stays scheduled.)
- Tier-2 software hard-stop monitor — bedcrock's broker-side-OCO-only design is *safer* (no double-sell race possible).
- Convergence-multiplier scoring — bedcrock's additive 9-component scorer is more flexible.
- SQLite, Telegram, ARK-as-primary-signal.

The previous delta document `bedcrock-plan-v2.md` was deleted during the v0.2.0 → spec consolidation; it is recoverable from git history at commit `1938275` (where v2 was added) if the diff-style record is ever needed.

**v0.3.0 (2026-05-10) — drop the vault and Cowork; reasoning moves to Claude Code Routines.** Triggered by two facts: (1) the v0.2 vault writer at `src/vault/writer.py` was no-op stubs and had been silently broken since v0.1 — production bedcrock had never written anything to the vault; (2) the operator has no Obsidian Sync subscription and no Syncthing, so the vault was not reachable from phone or laptop, collapsing the "human-readable dashboard" rationale. Claude Code 2026's `/schedule` (cloud-hosted Routines) covers the same cadence Cowork provided, on the same Pro/Max subscription, without a desktop product dependency. So instead of restoring the broken layer, the v0.3 refactor deleted it.

Landed across four parallel waves on `v3-staging`. Highlights:

- **Removed `src/vault/`** (writer + frontmatter helpers) and all call sites in the ingest worker and orders monitor — commit `16f4de8`.
- **Dropped `vault_path` columns** from `Signal` and `Position`; alembic migration `0003_drop_vault.py` also adds the new `scoring_proposals` and `scoring_replay_reports` tables — commits `53daf1b`, `db0d226`.
- **Refactored `src/workers/eod_worker.py`** to drop vault writes; replay reports now persist as structured rows in `scoring_replay_reports`, and EOD posts a Discord summary instead of writing a daily note — commit `7281b1e`.
- **Deleted `cowork-prompts/` and `vault-templates/` directories** and pruned `python-frontmatter` from `pyproject.toml` — commit `2bc01d1`.
- **Added five Claude Code skills** under `.claude/skills/`: `morning-analyze`, `intraday-check`, `hourly-closure`, `weekly-synthesis`, `status` — commit `1686ae1`.
- **Added five FastAPI dashboard endpoints** (`/dashboard/morning`, `/dashboard/intraday`, `/dashboard/closures`, `/dashboard/weekly`, `/dashboard/status`) and the `POST /scoring-proposals` write path, all behind bearer-token auth — commit `05ec4e9`.

Total lines removed across `src/`, `tests/`, `cowork-prompts/`, `vault-templates/`, and the docs is ~926. The reasoning surface went from "four Cowork prompts triggered by file watchers on a synced vault" to "five SKILL.md files registered as cloud Routines, consuming JSON dashboards over HTTPS." Single source of truth (Postgres). Single reasoning surface (Claude Code). One fewer drift surface, one fewer product dependency, one fewer paid subscription.

**Invariants changed in v0.3:**

- Invariant 2 was *"Cowork is the reasoning layer, not the infrastructure layer"* → now *"Three-layer authority: broker beats DB beats reasoning."* Captures the actual ordering on conflict.
- Invariant 3 was *"The vault is the source of truth"* → now *"DB is the only durable store."* Postgres is canonical; Routines are stateless consumers.
- Invariant 4 was *"Inbox-then-process"* (a write-discipline rule for the vault that no longer exists) → now *"Reasoning is stateless and replayable."* The new framing is what actually matters: any Routine can be re-run on the same DB snapshot and reach the same conclusions.
- Invariants 1, 5–9 unchanged.

**Explicitly NOT done in v0.3** (carried as future considerations):

- A custom MCP server for Postgres. The dashboard endpoints + `psql` via Bash were sufficient and avoid yet another moving part.
- Migration tooling to rebuild any existing v0.2.0 vault data — there was none in production (the writer never wrote).
- A custom dashboard frontend. The endpoints exist; build one if/when Discord + skill summaries stop being enough.
