---
name: status
description: On-demand snapshot of bedcrock state — current P&L, open positions count, today's signals processed, and system health. Invoked manually by the user (typically as /status) for ad-hoc check-ins; never auto-triggered. Prints to stdout when run interactively, or posts a compact embed to the system-health Discord channel when run from a Routine.
disable-model-invocation: true
allowed-tools:
  - Bash(curl *)
  - Bash(jq *)
context: same
---

A quick on-demand state read. Use when the user asks "how are we doing right now?" outside the scheduled cadences.

## Step 1 — Pull status

```bash
curl -sS -H "Authorization: Bearer $API_BEARER_TOKEN" \
  "$API_BASE_URL/dashboard/status" > /tmp/status.json
```

Expected JSON shape: `{pnl_today_usd, pnl_week_usd, open_positions_count, signals_today, last_ingest_at, db_ok, broker_ok}`.

## Step 2 — Render

If invoked interactively (no `$DISCORD_WEBHOOK_SYSTEM_HEALTH` set), pretty-print the JSON to stdout in a compact human-readable form (one line per field).

If invoked from a Routine (`$DISCORD_WEBHOOK_SYSTEM_HEALTH` is set), POST a compact embed:

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  -d "$(jq -n '
    { embeds: [ {
        title: "bedcrock status",
        color: (if env.DB_OK == "true" and env.BROKER_OK == "true" then 3066993 else 15158332 end),
        fields: [
          {name: "P&L today",   value: env.PNL_TODAY,   inline: true},
          {name: "P&L week",    value: env.PNL_WEEK,    inline: true},
          {name: "Open",        value: env.OPEN_COUNT,  inline: true},
          {name: "Signals 24h", value: env.SIG_COUNT,   inline: true},
          {name: "Last ingest", value: env.LAST_INGEST, inline: true},
          {name: "Health",      value: env.HEALTH,      inline: true}
        ]
      } ] }')" \
  "$DISCORD_WEBHOOK_SYSTEM_HEALTH"
```
