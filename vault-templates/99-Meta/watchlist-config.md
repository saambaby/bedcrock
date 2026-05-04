---
type: meta
file: watchlist-config
---

# Watchlist config

Cowork's morning prompt enforces these caps when promoting from inbox to watchlist.

```yaml
watchlist:
  max_active: 30
  max_per_sector: 6
  max_per_sector_etf: 6
  retire_after_days_idle: 30
```

## Promotion criteria

A signal in `00 Inbox/` is promoted to `01 Watchlist/<TICKER>.md` when:
- Score ≥ 6.0 AND no gate blocked, OR
- Cowork judges it strategically interesting even at lower score

## Retirement

Watchlist tickers move to `00 Archive/` (or just deleted) when:
- 30 days with no new signal AND no position
- Catalyst date passed without a setup forming
- Macro context flips (e.g. semis watchlist during semi sector breakdown)
