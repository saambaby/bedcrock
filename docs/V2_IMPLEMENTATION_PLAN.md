# Bedcrock v2 — Multi-Agent Implementation Plan

How to ship the 14 commits in [`bedcrock-plan.md §V2.12`](../bedcrock-plan.md) using parallel Claude Code subagents in git worktrees, with explicit dependency ordering, per-agent briefs, and a merge protocol.

**Total estimated wall time:** ~1 day with parallel execution (vs. ~3 days sequential).
**Total estimated agent-time:** ~8–10 agent-hours.
**Orchestrator role:** human (you) + the main Claude Code session you're reading this in. The orchestrator dispatches agents, reviews diffs, merges branches, and runs the integration suite.

---

## 1. Goal & success criteria

**Goal:** all 14 commits from `bedcrock-plan.md §V2.12` land on `main` in the order specified, with tests passing, and v0.1 paper-trading workflow continues to function (no regressions).

**Done means:**
- [ ] `pytest tests/` passes (with the new tests added by Wave C)
- [ ] `python -m src.workers.healthcheck` runs cleanly against a paper-mode IBKR connection
- [ ] One end-to-end paper trade flows: signal ingested → scored → drafted → confirmed via `/confirm` → bracket placed at IBKR with stop `tif=GTC` (verifiable via TWS Order window)
- [ ] All Phase 1 → Phase 2 acceptance checklist items from `bedcrock-plan.md §V2.10` are testable (some require multi-day soak; this plan only requires the *test infra* to be in place)
- [ ] `docs/ENV.md`, `docs/DEPLOYMENT.md`, `bedcrock/AUDIT.md` updated to reflect v2

---

## 2. Wave structure

```
Wave A (Foundation, 1 sequential agent)
  └── Wave B (6 parallel feature agents, each in own worktree)
        └── Wave C (1 sequential test agent)
              └── Wave D (1 sequential docs + integration agent)
```

**Why this shape:**
- Wave A pre-stages all schema + dependency + config changes so Wave B agents inherit a stable substrate. Without this, six agents touching `src/db/models.py` and `src/config.py` simultaneously guarantees merge conflicts.
- Wave B agents own disjoint feature surfaces. Each gets its own worktree so they cannot collide. Their PRs merge into a `v2-staging` branch in arbitrary order.
- Wave C runs *after* all Wave B agents merge, because tests need to exercise the integrated surface, not isolated branches.
- Wave D is purely documentation + final integration verification. Sequential because docs benefit from one author's voice.

**Coordination model:** every agent operates on a branch named `v2/<wave>/<feature>` based on either `main` (Wave A) or `v2-staging` (Wave B onward). The orchestrator creates `v2-staging` after Wave A merges and rebases it forward as later waves complete.

---

## 3. Wave A — Foundation (1 agent, sequential)

**Agent type:** `general-purpose`, `isolation: worktree`.

**Branch:** `v2/A/foundation` off `main`.

**Why this is one agent (not parallel):** every Wave B agent needs the new schema/enum/config in place. Doing this as a single coherent commit also makes the alembic migration atomic — no Wave B migration ordering games.

**Scope (in dependency order within this agent):**

1. **`pyproject.toml`** — replace `ib-insync>=0.9.86` with `ib_async==2.1.0`.

2. **`src/broker/ibkr.py`** — full sweep:
   - `from ib_insync import` → `from ib_async import`
   - Replace blocking `asyncio.to_thread(ib.X, ...)` calls with native async equivalents (`accountSummaryAsync`, `qualifyContractsAsync`, `reqTickersAsync`, `reqExecutionsAsync`)
   - `ib.placeOrder(contract, order)` is already non-blocking in `ib_async`; remove the `to_thread` wrapper
   - Do **not** add GTC TIF or connection retry yet — those land in Wave B agents
   - Do **not** delete the existing `submit_bracket` method's defaults; just swap the imports

3. **`src/orders/monitor.py`** — drop the `_keep_alive` event-loop bridging:
   ```python
   async def _keep_alive():
       """ib_async is pure asyncio — no bridging needed."""
       while not self._stopped and self._broker._ib.isConnected():
           await asyncio.sleep(5)
   ```
   Remove the `ib.sleep(1) + await asyncio.sleep(0.1)` dance.

4. **`src/db/models.py`** — pre-stage schema changes for downstream agents:
   - `CloseReason` enum: add `EXTERNAL = "external"`
   - `SignalSource` enum: add `MARKET_MOVEMENT = "market_movement"`
   - New `DailyState` model: `(date, mode, daily_pnl_pct, equity_at_open, updated_at)` with PK on `(date, mode)`
   - `Position.broker_order_id`: add `unique=True`

5. **`alembic/versions/0002_v2_foundation.py`** — single migration covering all of step 4. Include the dedupe DELETE for any existing duplicate `broker_order_id` rows.

6. **`src/config.py`** — pre-stage all v2 settings:
   - `risk_max_position_size_pct: float = 0.05` (used by Wave B sizing agent)
   - `risk_sector_concentration_limit: float = 0.25` (used by Wave B sector-gate agent)
   - `movement_volume_spike_threshold: float = 3.0` (used by Wave B heavy-movement agent)
   - `movement_gap_threshold: float = 0.05`
   - `movement_check_interval_seconds: int = 300`
   - `@model_validator(mode="after") _validate_mode_port` per `bedcrock-plan.md §V2.9`

7. **Smoke test:** run `python -c "from src.broker.ibkr import IBKRBroker; from src.scoring.scorer import Scorer; from src.config import settings; print('imports OK')"` — confirms the migration didn't break import resolution.

**Deliverable:** one PR titled `v2: foundation (ib_async migration + schema + config)` with the changes above as a single commit (or 2 commits if the alembic migration cleanly separates).

**Tests required in this wave:**
- Existing `tests/*.py` must still pass (no behaviour changes yet, only imports + schema additions).
- One new test: `tests/test_config.py::test_mode_port_validator_rejects_mismatch`.

**Acceptance:** orchestrator merges `v2/A/foundation` to `main`, then creates `v2-staging` branch off the new `main`. Wave B agents branch off `v2-staging`.

**Estimated time:** 60–90 min agent time.

---

## 4. Wave B — Features (6 parallel agents)

Each agent is dispatched in its own worktree, branched off `v2-staging`. Run all six in parallel via a single message with six `Agent` tool calls.

The orchestrator merges them back into `v2-staging` in arbitrary order as they complete. Conflicts are unlikely because the file scopes are disjoint (verified in §6 below).

### Agent B1 — Broker safety (GTC + reconciler + connection retry)

**Branch:** `v2/B/broker-safety` off `v2-staging`.

**Owns commits:** 2, 3, 5 (partial), 6 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. `src/broker/ibkr.py::submit_bracket` — set `tif="GTC"` and `outsideRth=True` on the take-profit and stop-loss children. Parent stays `tif="DAY"`. Code reference: `bedcrock-plan.md §V2.2`.
2. `src/broker/ibkr.py::connect` — add `readonly: bool = False` parameter, exponential-backoff retry (5 attempts: 1s, 2s, 4s, 8s, 16s), terminal alert via `post_system_health` on failure. Code reference: `bedcrock-plan.md §V2.4` (audit §3.6 + §3.8).
3. **New file** `src/safety/__init__.py` (empty `__init__`).
4. **New file** `src/safety/reconciler.py` containing:
   - `audit_open_order_tifs(broker)` — re-issues any non-GTC bracket child. Code reference: `bedcrock-plan.md §V2.2`.
   - `reconcile_against_broker(broker, db)` — startup orphan/stale detection. Code reference: `bedcrock-plan.md §V2.4`.
5. `src/orders/monitor.py::LiveMonitor.start()` — call `await reconcile_against_broker(self._broker, db)` after `await self._broker.connect()`, before `_poll` and `_keep_alive` start.
6. `src/workers/monitor_worker.py` — add a 30s task that calls `audit_open_order_tifs(broker)` alongside the existing `_reconcile_orders` poll.

**Tests in branch (`tests/test_broker_safety.py` — new file):**
- `test_bracket_children_are_gtc` — mock `IB`, call `submit_bracket`, assert child orders have `tif=="GTC"` and `outsideRth==True`.
- `test_audit_repairs_non_gtc_child` — fixture an open trade with `tif="DAY"`, call `audit_open_order_tifs`, assert it cancels + re-issues.
- `test_reconcile_orphan_alert` — IBKR has AAPL position, DB doesn't, assert AuditLog entry created and `post_position_alert` mocked-called.
- `test_reconcile_stale_marks_closed` — DB has MSFT open, IBKR doesn't, assert position marked closed with `close_reason=EXTERNAL`.
- `test_connect_retry_succeeds_on_third_attempt` — mock `connectAsync` to fail twice then succeed.

**Acceptance:** all 5 tests pass. `pytest tests/test_broker_safety.py -v` clean.

**Estimated time:** 90 min.

---

### Agent B2 — Monitor idempotency

**Branch:** `v2/B/monitor-idempotency` off `v2-staging`.

**Owns commit:** 4 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. `src/orders/monitor.py::_on_entry_fill` — add idempotency check at top: `SELECT Position WHERE broker_order_id = ?` returns non-None ⇒ log skip, ensure draft status = FILLED, return. Code reference: `bedcrock-plan.md §V2.3`.
2. `src/orders/monitor.py::_reconcile_orders` — add `already_filled` check: if `Position.broker_order_id` matches an existing row, mark draft FILLED and skip. Code reference: same section.

**Note:** the `UNIQUE` constraint on `Position.broker_order_id` already landed in Wave A's alembic migration; this agent does not touch the schema.

**Tests in branch (`tests/test_orders.py` additions):**
- `test_double_fill_idempotency` — call `_on_entry_fill` twice with same `broker_order_id`, assert exactly one Position row.
- `test_ws_and_poll_concurrent_no_duplicate_position` — fire `_on_entry_fill` and `_reconcile_orders` concurrently with `asyncio.gather`, assert one Position row.
- `test_reconciler_repairs_drift_when_ws_won` — Position exists but draft.status still SENT, run `_reconcile_orders`, assert draft.status flips to FILLED.

**Acceptance:** 3 tests pass. Existing `tests/test_orders.py` tests still pass.

**Estimated time:** 60 min.

---

### Agent B3 — Daily kill switch wiring

**Branch:** `v2/B/daily-kill-switch` off `v2-staging`.

**Owns commit:** 7 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. `src/workers/monitor_worker.py` — add async task `update_daily_pnl(db)` running every 60s during market hours:
   - Read today's `EquitySnapshot` for `mode = settings.mode`, ordered by `created_at ASC`, take first.
   - Fetch current account equity from broker.
   - Compute `pnl_pct = (current - start_of_day) / start_of_day * 100`.
   - Upsert into `DailyState` (created in Wave A) for `(date.today(), settings.mode)`.
2. `src/scoring/gates.py::_gate_daily_kill_switch` — replace stub with real implementation:
   - Read `DailyState` for today + mode.
   - If `daily_pnl_pct <= -settings.risk_daily_loss_pct`, return `blocked=True, overrideable=False`.
   - Code reference: `bedcrock-plan.md §V2.5` (audit §3.5).
3. `src/workers/eod_worker.py` (existing) — ensure it writes the start-of-day `EquitySnapshot` *before* `update_daily_pnl` would run. If this isn't already the case, add it.

**Tests in branch (`tests/test_gates.py` additions + new `tests/test_daily_pnl.py`):**
- `test_daily_kill_switch_blocks_at_negative_threshold` — fixture DailyState with pnl=-2.5%, assert gate blocks at default 2% threshold.
- `test_daily_kill_switch_passes_above_threshold` — fixture pnl=-1%, assert pass.
- `test_daily_kill_switch_passes_when_no_state` — no DailyState row (e.g., before market open), assert pass (fail-open).
- `test_update_daily_pnl_computes_correctly` — fixture EquitySnapshot at 100k, mock account equity at 98k, call `update_daily_pnl`, assert DailyState row has pnl_pct = -2.0.

**Acceptance:** 4 tests pass.

**Estimated time:** 75 min.

---

### Agent B4 — Sizing + sector gate

**Branch:** `v2/B/sizing-and-sector` off `v2-staging`.

**Owns commits:** 8, 9 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. `src/orders/builder.py::OrderBuilder.build_draft` — after `qty_by_risk` is computed, compute `qty_by_concentration = (account.equity * settings.risk_max_position_size_pct) / entry`, take `quantity = min(qty_by_risk, qty_by_concentration)`. Log when concentration cap binds. Code reference: `bedcrock-plan.md §V2.7`.
2. `src/scoring/gates.py::GateEvaluator._gate_correlation` — replace the stub at `gates.py:52` with the real implementation. Code reference: `bedcrock-plan.md §V2.6`.
3. `src/scoring/gates.py` — add `SECTOR_ETF_MAP` constant (top of file) covering at least Defense (ITA), Biotech (XBI), Tech (XLK), Discretionary (XLY), Energy (XLE), Financials (XLF). Add `OTHER` fallback.
4. Wire `_gate_correlation` into `GateEvaluator.evaluate` — replace the stub `GateResult(gate=GateName.CORRELATION, blocked=False)` with the real call.

**Tests in branch:**
- `tests/test_orders.py::test_position_size_capped_by_concentration` — fixture entry=100, stop=99 (1% stop → risk-based qty huge), equity=100k, assert quantity capped at `equity * 0.05 / 100 = 50`.
- `tests/test_orders.py::test_position_size_unchanged_when_risk_lower` — fixture wide stop, assert quantity = qty_by_risk.
- `tests/test_gates.py::test_sector_gate_blocks_overconcentration` — fixture 3 open ITA positions totaling 22% of equity, propose 4th defense ticker, assert blocked.
- `tests/test_gates.py::test_sector_gate_passes_when_under_limit` — fixture one ITA position at 5%, assert new ITA proposal passes.
- `tests/test_gates.py::test_sector_gate_failopen_when_no_indicators` — assert pass when indicators=None.

**Acceptance:** 5 tests pass.

**Estimated time:** 90 min.

---

### Agent B5 — Heavy-movement ingestor

**Branch:** `v2/B/heavy-movement` off `v2-staging`.

**Owns commit:** 10 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. **New file** `src/ingestors/heavy_movement.py` — `HeavyMovementIngestor` per `bedcrock-plan.md §V2.5`. Inherits `IngestorBase`, `name="heavy_movement"`, `interval_seconds=settings.movement_check_interval_seconds`.
2. `src/scoring/scorer.py::Scorer.score` — add `MARKET_MOVEMENT` handling at the top: if `signal.source == MARKET_MOVEMENT`, score is 0 unless a non-MARKET_MOVEMENT signal exists on the same ticker in last 14d, in which case score with `flow_corroboration_market` slot. Code reference: `bedcrock-plan.md §V2.5`.
3. `src/scoring/scorer.py::Scorer._score_cluster` — exclude MARKET_MOVEMENT from cluster source-counting; add 0.5-point bonus if a same-direction MARKET_MOVEMENT signal exists in last 14d.
4. `src/schemas/__init__.py::ScoreBreakdown` — add `flow_corroboration_market: float = 0.0` field.
5. `src/workers/ingest_worker.py` — register `HeavyMovementIngestor` in `IngestorRegistry`.
6. `src/ingestors/ohlcv.py` — verify `get_recent_bars(ticker, days=N)` exists (per audit §3.7 it should). If signature differs from what the scope expects, add a thin compatibility wrapper.

**Tests in branch (`tests/test_heavy_movement.py` — new file + `tests/test_scoring.py` additions):**
- `test_heavy_movement_emits_on_volume_spike` — fixture 21 bars with bar 21 having 4x average volume, assert one Signal row created with `source=MARKET_MOVEMENT`.
- `test_heavy_movement_skips_below_threshold` — fixture all-normal bars, assert zero Signals.
- `test_heavy_movement_kills_major_gap_down` — fixture bar with -15% gap, assert no Signal (per `GAP_DOWN_KILL`).
- `test_heavy_movement_only_for_watchlist` — empty watchlist, assert zero Signals (no scanning unrelated tickers).
- `tests/test_scoring.py::test_market_movement_alone_scores_zero` — fixture lone MARKET_MOVEMENT signal, no priors, assert score=0.
- `tests/test_scoring.py::test_market_movement_corroborates_existing` — fixture MARKET_MOVEMENT + Form 4 in last 14d, assert `flow_corroboration_market > 0`.
- `tests/test_scoring.py::test_cluster_bonus_for_movement` — fixture 2 fundamental sources + 1 MARKET_MOVEMENT, assert cluster score includes 0.5 bonus.

**Acceptance:** 7 tests pass.

**Estimated time:** 2 hours.

---

### Agent B6 — Mini-backtester

**Branch:** `v2/B/backtester` off `v2-staging`.

**Owns commit:** 11 from `bedcrock-plan.md §V2.12`.

**Scope:**
1. **New file** `src/backtest/__init__.py` (empty `__init__`).
2. **New file** `src/backtest/replay.py` containing `ReplayReport` dataclass + `replay()` function + helpers (`_simulate_trade`, `_sharpe`, `_profit_factor`, `_score_signal`). Code reference: `bedcrock-plan.md §V2.8`.
3. `src/workers/eod_worker.py` — add a task that runs at Sunday 17:00 ET (1 hour before Cowork's weekly synthesis):
   - Read `99 Meta/scoring-rules-proposed.md` (use existing vault reader if available, else simple file read + YAML parse).
   - For each proposed weight set, call `replay(db, proposed_weights)`.
   - Write `ReplayReport` to `06 Weekly/{date}-replay-{rule_name}.md` so Cowork's synthesis can read it.
4. **Caveat doc** at top of `src/backtest/replay.py` listing limitations: OHLCV-only (no bid/ask), constant slippage, no survivorship correction, advisory only.

**Tests in branch (`tests/test_backtester.py` — new file):**
- `test_sharpe_zero_returns_zero` — empty input, assert 0.
- `test_sharpe_positive_for_winning_strategy` — list of mostly positive returns, assert > 0.
- `test_replay_simulates_trade_at_t_plus_1_open` — fixture signal + 5 bars, assert simulated entry on day 2's open.
- `test_replay_exits_on_stop` — fixture bars where day 4 hits stop, assert exit_reason="stop".
- `test_replay_exits_on_target` — fixture bars where day 6 hits target, assert exit_reason="target".
- `test_replay_recommends_reject_when_oos_worse` — proposed weights that score worse on out-of-sample, assert recommendation="REJECT".

**Acceptance:** 6 tests pass.

**Estimated time:** 2.5 hours.

---

### Agent B7 — Cleanup (low-risk, parallel-safe)

**Branch:** `v2/B/cleanup` off `v2-staging`.

**Owns commit:** 12 from `bedcrock-plan.md §V2.12`.

**Scope:** delete the duplicates already documented in `bedcrock/AUDIT.md` §"Resume-session reconciliation":
1. Delete `src/schemas/signal.py` (the standalone dataclass `ScoreBreakdown` — Pydantic version in `__init__.py` wins).
2. Delete `src/schemas/order.py` (the simpler `DraftOrderPayload` — comprehensive version in `__init__.py` wins).
3. Delete the `score_pending_signals(db)` helper if it exists in scorer (dead code per AUDIT.md).
4. Add `tests/test_imports.py` with assertions:
   ```python
   def test_score_breakdown_canonical_module():
       from src.schemas import ScoreBreakdown
       assert ScoreBreakdown.__module__ == "src.schemas"

   def test_no_duplicate_draft_order_payload():
       import importlib.util
       assert importlib.util.find_spec("src.schemas.order") is None
   ```

**Tests in branch:** the 2 above + `pytest tests/` clean (no broken imports).

**Acceptance:** all existing tests pass; the 2 new ones pass.

**Estimated time:** 30 min.

---

### Wave B parallel dispatch

Send a single message with seven `Agent` tool calls (one per B agent above), all marked `run_in_background: true`. Each gets `subagent_type: general-purpose` and `isolation: worktree`.

Each prompt should:
- State the agent's number (B1–B7)
- Quote the exact "Scope" and "Tests in branch" sections from this plan
- Reference `bedcrock-plan.md §V2.X` for code patterns
- End with: "Commit on branch `v2/B/<feature>`. Do not merge. Report back with the commit hash and `pytest <new-test-files> -v` output."

---

## 5. Wave C — Test integration (1 agent, sequential)

**Pre-requisite:** all 7 Wave B agents merged into `v2-staging` by the orchestrator. Resolve any merge conflicts inline (expected to be minimal — see §6 conflict matrix).

**Agent type:** `general-purpose`, `isolation: worktree`.

**Branch:** `v2/C/integration-tests` off `v2-staging` (post-merge).

**Scope:**
1. Run `pytest tests/ -v` and capture the full output. Fix any failures caused by Wave B integration (e.g., a fixture in B1 collides with a fixture in B3; B5's scorer changes break a B4 test assumption).
2. Add **invariant tests** that span multiple agents' work — these are the tests Wave B agents could not write because they require integrated state:
   - `tests/test_v2_invariants.py::test_signal_to_position_e2e_paper_dryrun` — full mocked flow: signal → score (with all gates) → draft → confirm → broker mock → Position row → reconciler clean.
   - `tests/test_v2_invariants.py::test_market_movement_does_not_create_drafts_alone` — emit one MARKET_MOVEMENT signal with no priors, assert no DraftOrder created.
   - `tests/test_v2_invariants.py::test_sector_gate_blocks_concentration_across_open_positions` — seed 3 ITA positions, attempt 4th, assert blocked end-to-end.
   - `tests/test_v2_invariants.py::test_daily_kill_switch_blocks_new_drafts` — set DailyState pnl=-3%, attempt to build a draft, assert returns None due to gate.
   - `tests/test_v2_invariants.py::test_reconciler_audits_ALL_open_orders` — seed 5 open trades (1 with `tif="DAY"`), call `audit_open_order_tifs`, assert exactly 1 repaired.
3. Run `python -m src.workers.healthcheck` against a paper-mode IBKR connection (if available in the test env; if not, use a mocked broker fixture). Confirm clean output.
4. Add `tests/conftest.py` shared fixtures if duplication appears across the new test files.

**Acceptance:**
- `pytest tests/ -v` returns 0 failures.
- All 5 invariant tests above pass.
- The agent's report includes a file list of every test file added/modified and a per-test pass/fail summary.

**Estimated time:** 2 hours.

---

## 6. Wave D — Docs + final integration (1 agent, sequential)

**Branch:** `v2/D/docs` off `v2-staging` (post-Wave-C merge).

**Scope:**
1. **`docs/ENV.md`** — add new env vars: `RISK_MAX_POSITION_SIZE_PCT`, `RISK_SECTOR_CONCENTRATION_LIMIT`, `MOVEMENT_VOLUME_SPIKE_THRESHOLD`, `MOVEMENT_GAP_THRESHOLD`, `MOVEMENT_CHECK_INTERVAL_SECONDS`. Document the mode↔port validator behaviour.
2. **`docs/DEPLOYMENT.md`** — add IBC + Xvfb + nightly logout section (per audit §3.6). Document the `gnzsnz/ib-gateway-docker` recommended path.
3. **`docs/AUDIT.md`** — append a "v2 status" section noting which audit findings landed and which are deferred to v0.3.
4. **`README.md`** — update the project layout block to show new `src/safety/`, `src/backtest/`, `src/ingestors/heavy_movement.py`. Update the design invariants list to include the 3 new v2 invariants.
5. **`bedcrock-plan.md`** — flip `status: draft` to `status: active`. Add a "Implementation completed: YYYY-MM-DD" line in the frontmatter.
6. **Final integration check:**
   - Run `pytest tests/ -v` one last time on the merged `v2-staging`.
   - Run `python -c "from src import broker, scoring, orders, ingestors, safety, backtest, db, api, workers, discord_bot; print('all packages import')"`.
   - If alembic env supports it, run `alembic upgrade head` against a throwaway test database.
7. **Open the PR** from `v2-staging` to `main` with a description listing all 14 commits and pointing to `bedcrock-plan.md` for the spec.

**Acceptance:**
- All 5 doc files updated.
- Final test run is green.
- PR opened (orchestrator/human merges).

**Estimated time:** 60 min.

---

## 6. Conflict matrix (Wave B agents)

Verifies that the seven parallel agents touch disjoint surfaces. `*` marks the only intentional shared file; merge resolution should be trivial.

| File | B1 broker-safety | B2 monitor-idemp | B3 daily-kill | B4 sizing+sector | B5 heavy-mvmnt | B6 backtest | B7 cleanup |
|---|---|---|---|---|---|---|---|
| `src/broker/ibkr.py` | ✓ submit_bracket + connect | | | | | | |
| `src/orders/monitor.py` | ✓ start() only | ✓ _on_entry_fill + _reconcile_orders | | | | | |
| `src/orders/builder.py` | | | | ✓ build_draft | | | |
| `src/scoring/gates.py` | | | ✓ _gate_daily_kill_switch | ✓ _gate_correlation + SECTOR_ETF_MAP | | | |
| `src/scoring/scorer.py` | | | | | ✓ score + _score_cluster | | (delete dead helper) |
| `src/workers/monitor_worker.py` | ✓ add audit_open_order_tifs task | | ✓ add update_daily_pnl task | | | | |
| `src/workers/ingest_worker.py` | | | | | ✓ register | | |
| `src/workers/eod_worker.py` | | | (verify start-of-day snapshot) | | | ✓ replay hookup | |
| `src/safety/reconciler.py` (new) | ✓ owner | | | | | | |
| `src/safety/__init__.py` (new) | ✓ owner | | | | | | |
| `src/ingestors/heavy_movement.py` (new) | | | | | ✓ owner | | |
| `src/backtest/replay.py` (new) | | | | | | ✓ owner | |
| `src/backtest/__init__.py` (new) | | | | | | ✓ owner | |
| `src/schemas/__init__.py` * | | | | | ✓ ScoreBreakdown field add | | |
| `src/schemas/signal.py` | | | | | | | ✓ delete |
| `src/schemas/order.py` | | | | | | | ✓ delete |
| `tests/test_broker_safety.py` (new) | ✓ owner | | | | | | |
| `tests/test_orders.py` | | ✓ additions | | ✓ additions | | | |
| `tests/test_gates.py` | | | ✓ additions | ✓ additions | | | |
| `tests/test_scoring.py` | | | | | ✓ additions | | |
| `tests/test_heavy_movement.py` (new) | | | | | ✓ owner | | |
| `tests/test_backtester.py` (new) | | | | | | ✓ owner | |
| `tests/test_imports.py` (new) | | | | | | | ✓ owner |

**Two cells with multiple ✓:**
1. `src/scoring/gates.py` — B3 modifies `_gate_daily_kill_switch`, B4 modifies `_gate_correlation`. Different functions, different lines, will git-merge cleanly.
2. `src/orders/monitor.py` — B1 modifies `start()` (adds reconcile call), B2 modifies `_on_entry_fill` and `_reconcile_orders`. Different methods, different lines, clean merge.
3. `src/workers/monitor_worker.py` — B1 adds an audit task, B3 adds a daily-pnl task. Both are additions; if they land near each other in `setup_scheduler()` or the equivalent, manual merge for ordering is trivial.
4. `tests/test_orders.py` — B2 and B4 both add cases. Different test names, clean merge.
5. `tests/test_gates.py` — B3 and B4 both add cases. Different test names, clean merge.

**Verdict:** safe to dispatch all 7 in parallel.

---

## 7. Coordination protocol

### Branching

```
main
  └── v2/A/foundation                  (Wave A, merges to main)
        └── v2-staging                 (created by orchestrator after Wave A merges)
              ├── v2/B/broker-safety   (Wave B parallel)
              ├── v2/B/monitor-idempotency
              ├── v2/B/daily-kill-switch
              ├── v2/B/sizing-and-sector
              ├── v2/B/heavy-movement
              ├── v2/B/backtester
              ├── v2/B/cleanup
              ├── v2/C/integration-tests   (Wave C, after all B merge)
              └── v2/D/docs                (Wave D, after C merge)
                    └── final PR: v2-staging → main
```

### Per-agent dispatch template

Use this prompt scaffold for each Wave B agent:

```
You are agent <ID> implementing bedcrock v2 commit <#>.

Context:
- Project: /Users/sambaby/Development/@saam.baby/arp/bedcrock
- Plan: bedcrock-plan.md (v2 spec) — read sections <X.Y> for code patterns
- Implementation plan: docs/V2_IMPLEMENTATION_PLAN.md — your scope is §4 Agent <ID>
- Branch: create v2/B/<feature> off v2-staging

Scope (verbatim from plan):
[paste the Scope block]

Tests required (verbatim):
[paste the Tests in branch block]

Constraints:
- Do NOT touch files outside your scope (see §6 conflict matrix)
- Do NOT modify the alembic migrations directory (Wave A owns 0002_v2_foundation.py; do not add 0003+)
- Do NOT merge — leave the branch open. Orchestrator will merge.
- If you need to add a config setting, it should already exist in src/config.py (added by Wave A); if it doesn't, fail fast and report rather than adding it yourself.

Deliverable:
- Commit titled per the table in bedcrock-plan.md §V2.12
- pytest output for the new test files
- Report under 200 words
```

### When an agent reports back

The orchestrator:
1. `cd` into the agent's worktree
2. `git diff v2-staging --stat` — sanity-check file scope matches §6
3. `pytest tests/<agent's test files>` — confirm green
4. If both check out: `git checkout v2-staging && git merge --no-ff v2/B/<feature>`
5. If conflict: resolve manually using §6 as the conflict map
6. After all 7 Wave B agents merge, run `pytest tests/` against `v2-staging` to verify nothing is broken before dispatching Wave C

### Failure recovery

- **Wave A breaks:** halt. Don't dispatch Wave B. Fix in-place; this is the foundational commit.
- **A single Wave B agent breaks:** the others continue. Re-dispatch the broken one with a refined prompt; merge order doesn't matter.
- **Wave B integration breaks (post-merge):** Wave C agent fixes it as part of its scope (it has the full integrated state).
- **Wave C breaks:** orchestrator fixes manually; Wave C is small enough to handle inline.
- **Anything broken in Wave D:** doc-only changes are reversible; just don't merge.

---

## 8. Estimated timing

| Wave | Parallel agents | Wall time | Agent time |
|---|---|---|---|
| A | 1 | ~75 min | ~75 min |
| B | 7 | ~2.5 hours (longest agent: B5 + B6 at 2-2.5h) | ~10 hours total |
| C | 1 | ~2 hours | ~2 hours |
| D | 1 | ~60 min | ~60 min |
| **Total** | — | **~6.5 hours** | **~14 agent-hours** |

Plus orchestrator overhead (merge resolution, conflict review, dispatch coordination): ~1–2 hours of human time.

---

## 9. Optional dry-run mode

Before dispatching real agents, the orchestrator can "rehearse" by:
1. Reading each agent brief to a `Plan` agent (`subagent_type: Plan`) and asking it to identify gaps.
2. Running `Agent B7 cleanup` first as a sanity check (smallest scope, lowest risk; if it can't be done cleanly, the worktree-isolation pattern has issues).

If dry-run reveals scope gaps, edit this plan before the main dispatch.

---

## 10. Acceptance for the entire v2 implementation

Final orchestrator checklist before merging `v2-staging` → `main`:

- [ ] All 14 commits present on `v2-staging` (verifiable via `git log --oneline main..v2-staging`)
- [ ] `pytest tests/ -v` returns 0 failures, ≥30 tests
- [ ] `python -m src.workers.healthcheck` clean against a paper IBKR
- [ ] One real paper trade confirmed end-to-end with `tif=GTC` on the bracket children (verified in TWS)
- [ ] `bedcrock/AUDIT.md` v2-status section appended
- [ ] `bedcrock-plan.md` frontmatter flipped to `status: active`
- [ ] `bedcrock-plan.md` (v1) frontmatter updated to `status: superseded` with a pointer to v2
- [ ] PR opened with the 14-commit summary

When ticked, merge to `main` and tag `v0.2.0`.

---

*Implementation plan for `bedcrock-plan.md`. Read that file first; this is the build choreography, not the spec.*
