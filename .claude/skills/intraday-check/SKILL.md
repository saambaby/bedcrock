---
name: intraday-check
description: Light midday status check at 12:00 and 14:00 ET on market days. Re-anchors against the morning gameplan, surfaces priority-ticker movement and any new high-score signals, posts a brief delta to the high-score Discord channel. Does NOT open new theses or second-guess the morning plan.
disable-model-invocation: true
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---

This is the intraday check. The morning gameplan set the day's intent. Your job is incremental: did anything material change?

## Step 1 — Pull the intraday snapshot

```bash
curl -sS -H "Authorization: Bearer $API_BEARER_TOKEN" \
  "$API_BASE_URL/dashboard/intraday" > /tmp/intraday.json
```

Expected JSON shape:

- `open_positions` — entry, current price, P&L $/%, distance to trailing stop, distance to target
- `active_alerts` — priority tickers from the morning plan with current quote vs. trigger
- `new_signals_since_morning` — anything scored above threshold since the morning run

## Step 2 — Reconcile against the morning plan

For each priority ticker:

- Already entered → note "Entered at $X, +/- Y%, watching <stop|target>"
- Disqualified (event, gap-and-fail, range broken the wrong way) → note "Skipped, <reason>"
- More attractive (clean break with volume confirmation) → note "Bumped"

For `new_signals_since_morning`, only flag a name if it is genuinely high-score AND consistent with the morning regime tag. Do not open new theses — that's the morning run's job.

## Step 3 — Position health

For each `open_positions` entry:

- Within 0.5R of stop → flag for attention
- At or beyond first target → flag for trail/scale decision
- Otherwise → silent

## Step 4 — Post the intraday embed

If nothing material changed, post a single short embed:

```
title: "Intraday — HH:MM ET — no material changes"
```

Otherwise compose an embed with fields {Position deltas, New high-score, Trigger updates} and POST to `$DISCORD_WEBHOOK_HIGH_SCORE`:

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  -d "$BODY" "$DISCORD_WEBHOOK_HIGH_SCORE"
```

Use color `15844367` (gold) when there are deltas, `8421504` (gray) for no-change.

## What NOT to do

- Don't second-guess the morning analysis based on intraday noise
- Don't open new theses
- Don't speculate on what hasn't happened yet
