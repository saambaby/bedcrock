---
type: dashboard
purpose: "At-a-glance vault home. Update Dataview queries here, not elsewhere."
---

# Trading Dashboard

> Requires the Dataview plugin in Obsidian.

## 🔥 Inbox — needs Cowork attention

```dataview
TABLE WITHOUT ID
  file.link AS "Note",
  type AS "Type",
  ticker AS "Ticker",
  score AS "Score",
  source AS "Source"
FROM "00 Inbox"
WHERE status = "new"
SORT score desc
LIMIT 30
```

## ⭐ High-score signals (score ≥ 6) past 7d

```dataview
TABLE WITHOUT ID
  file.link AS "Signal",
  ticker AS "Ticker",
  score AS "Score",
  action AS "Action",
  trader AS "Trader",
  disclosed_at AS "Disclosed"
FROM "00 Inbox"
WHERE type = "signal"
  AND number(score) >= 6
  AND date(disclosed_at) >= date(today) - dur(7 days)
SORT score desc
```

## 📈 Open positions

```dataview
TABLE WITHOUT ID
  file.link AS "Position",
  ticker AS "Ticker",
  side AS "Side",
  entry_price AS "Entry",
  stop AS "Stop",
  target AS "Target",
  entry_at AS "Opened"
FROM "02 Open Positions"
WHERE status = "open"
SORT entry_at desc
```

## 📋 Watchlist — live theses

```dataview
TABLE WITHOUT ID
  file.link AS "Ticker",
  sector AS "Sector",
  trigger AS "Trigger",
  last_reviewed AS "Last reviewed"
FROM "01 Watchlist"
WHERE status = "live"
SORT last_reviewed desc
```

## 🏁 Recent closures (past 30d)

```dataview
TABLE WITHOUT ID
  file.link AS "Trade",
  ticker AS "Ticker",
  pnl_pct AS "P&L %",
  pnl_usd AS "P&L $",
  close_reason AS "Reason",
  exit_at AS "Closed"
FROM "03 Closed"
WHERE date(exit_at) >= date(today) - dur(30 days)
SORT exit_at desc
```

## 👥 Tracked traders — top performers (60d)

```dataview
TABLE WITHOUT ID
  file.link AS "Trader",
  kind AS "Kind",
  hit_rate AS "Hit rate",
  avg_excess_return AS "Avg excess return",
  trades_in_window AS "Trades"
FROM "04 Traders"
WHERE status = "tracked"
SORT number(hit_rate) desc
LIMIT 20
```

## 🚫 Blocked-by-gate signals (past 7d)

These were strong scores but got gated. Review weekly to refine gates.

```dataview
TABLE WITHOUT ID
  file.link AS "Signal",
  ticker AS "Ticker",
  score AS "Score",
  gates_failed AS "Gates"
FROM "00 Inbox"
WHERE type = "signal"
  AND gate_blocked = true
  AND date(disclosed_at) >= date(today) - dur(7 days)
SORT score desc
```

## 🔕 Snoozed

See [[99-Meta/snoozed]]

## 🔧 System

- [[99-Meta/scoring-rules]] — current scoring weights
- [[99-Meta/risk-limits]] — current risk parameters
- [[99-Meta/watchlist-config]] — always-track + banned lists
- [[99-Meta/changelog]] — change history
