---
name: hourly-closure
description: Hourly post-mortem on positions that closed in the last hour, market hours only (10:00–16:00 ET). For each closure, reconstructs the entry thesis, labels the outcome pattern, extracts a one-line lesson, and posts a per-closure embed to the position-alerts Discord channel. Skips silently if no closures.
disable-model-invocation: true
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---

This is the hourly closure post-mortem. The backend records closure events to the DB whenever a stop or target hits. Your job is to convert each fresh closure into an honest post-mortem while the trade is still in working memory, and post it to the position-alerts channel.

## Step 1 — Pull recent closures

```bash
curl -sS -H "Authorization: Bearer $API_BEARER_TOKEN" \
  "$API_BASE_URL/dashboard/closures?hours=1" > /tmp/closures.json
```

Expected JSON shape: array of `{position_id, ticker, side, entry_at, entry_price, exit_at, exit_price, qty, pnl_usd, pnl_pct, close_reason, setup_at_entry, indicators_at_entry, source_signals[]}`.

If empty, exit silently — no post needed.

## Step 2 — For each closure, reason about it

For every entry in the array, work through:

**What I expected** — Reconstruct the thesis from `source_signals[]` and `setup_at_entry` in 2–3 sentences. Be honest about the actual entry expectation, not a sanitized version.

**What happened** — From entry/exit data: did price move as expected? Faster, slower, opposite direction? Note the hold duration.

**Where the thesis was right / wrong** — Three short bullets:
- Right about: …
- Wrong about: …
- Couldn't have known: …

**Pattern label** — Pick exactly one:
`clean-breakout`, `failed-breakout`, `news-pop-fade`, `base-build`, `mean-reversion-success`, `mean-reversion-failure`, `bedcrock-classic`, `correlation-blowup`, `other`.

**Lesson** — Maximum two sentences. Surgical. This rolls up into the weekly synthesis.

## Step 3 — Post one embed per closure

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  -d "$(jq -n --arg t "$TICKER" --arg r "$REASON" --argjson pnl "$PNL_USD" '
    { embeds: [ {
        title: ($t + " closed — " + $r),
        color: (if $pnl >= 0 then 3066993 else 15158332 end),
        fields: [
          {name: "P&L",        value: ($PNL_LINE),     inline: true},
          {name: "Hold",       value: ($HOLD),         inline: true},
          {name: "Pattern",    value: ($PATTERN),      inline: true},
          {name: "Expected",   value: ($EXPECTED),     inline: false},
          {name: "Happened",   value: ($HAPPENED),     inline: false},
          {name: "Right/Wrong", value: ($RIGHT_WRONG), inline: false},
          {name: "Lesson",     value: ($LESSON),       inline: false}
        ],
        footer: {text: "hourly-closure"}
      } ] }')" \
  "$DISCORD_WEBHOOK_POSITIONS"
```

Color: green (`3066993`) for winners, red (`15158332`) for losers.

## What NOT to do

- Don't be defensive about losing trades. The lesson lives in the loss.
- Don't add information you didn't have at entry. "It would have been obvious" is not a lesson.
- Don't write a post-mortem for a position that hasn't actually closed (the API only returns closures, but double-check `exit_at` exists).
- Don't batch closures into one giant message — one embed per closure keeps each readable.
