---
name: morning-analyze
description: Build today's trading gameplan at 06:30 ET on weekdays. Triages overnight signals, refreshes active theses, identifies the day's priority tickers with triggers/stops/targets, and posts a morning embed to the high-score Discord channel. Invoked by the morning Routine; can also be run manually before the open.
disable-model-invocation: true
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---

This is the morning gameplan run. The backend has been ingesting overnight (politician trades, hedge fund filings, insider buys, options flow). Your job is to read the consolidated dashboard, decide what matters today, and publish a single decisive plan to the high-score Discord channel.

## Step 1 — Pull the morning snapshot

```bash
curl -sS -H "Authorization: Bearer $API_BEARER_TOKEN" \
  "$API_BASE_URL/dashboard/morning" > /tmp/morning.json
```

Expected JSON shape:

- `regime` — `{spy_trend, vix, sector_leadership}` — one-line market context
- `open_positions` — currently held positions with entry/stop/target and current P&L
- `recent_signals` — last 24h of scored signals not yet acted on
- `today_earnings_calendar` — names reporting today (avoid fresh entries into earnings)
- `gates_blocked_yesterday` — signals the gates rejected; review whether any were wrong

If the curl fails, post a single error embed to `$DISCORD_WEBHOOK_SYSTEM_HEALTH` and stop.

## Step 2 — Tag the regime

From `regime`, classify the day in one short phrase. Examples: `risk-on, breakout-friendly`, `chop, mean-reversion only`, `defensive, VIX > 22`. This sets the bar for today's setups — in chop, only A+ signals; in trend, B+ are fine.

## Step 3 — Triage recent signals

For each entry in `recent_signals`:

1. Is it consistent with the regime tag?
2. Does it cluster with another signal on the same name (multi-source confirmation)?
3. Is the underlying on `today_earnings_calendar`? If yes, deprioritize.
4. Does it conflict with an open position?

Drop singletons in unfavorable regimes. Cluster + thesis is the bar — never trade off one signal alone.

## Step 4 — Compose ACT TODAY list (max 5)

Pick at most five names. For each, write one row:

| Ticker | Side | Trigger | Stop | Target | Why today |

Triggers should be specific (price level, time, or event), not vague. If you can't articulate a trigger, the name doesn't belong on the list.

## Step 5 — Note what you're explicitly NOT doing

One or two seductive setups you're passing on, with a one-line reason. This is where the discipline lives.

## Step 6 — Surface anything urgent

If you notice a blocked-by-gate signal that looks mis-rejected, a trader whose recent track record dropped sharply, or correlation risk across the open book, call it out as a single bullet at the top.

## Step 7 — Post the morning embed

Compose a Discord embed and POST it to `$DISCORD_WEBHOOK_HIGH_SCORE`:

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  -d "$(jq -n --arg regime "$REGIME_TAG" --arg today "$(date +%F)" '
    { embeds: [ {
        title: ("Morning gameplan — " + $today),
        description: $regime,
        color: 3447003,
        fields: [
          {name: "Act today",      value: $ACT_TODAY,   inline: false},
          {name: "Watching",       value: $WATCHING,    inline: false},
          {name: "Not doing",      value: $NOT_DOING,   inline: false},
          {name: "Open positions", value: $OPEN_SUMMARY, inline: false},
          {name: "Urgent",         value: $URGENT,       inline: false}
        ],
        footer: {text: "morning-analyze"}
      } ] }')" \
  "$DISCORD_WEBHOOK_HIGH_SCORE"
```

Keep each field under ~900 chars (Discord embed field cap is 1024). If the act-today list is empty, say so explicitly — silence is not the message, "no qualifying setups today" is.

The goal is convergence: across the week, the priorities should get sharper, the noise quieter. Don't try to be exhaustive.
