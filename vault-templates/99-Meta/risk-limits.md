---
type: meta
file: risk-limits
version: 1
---

# Risk limits

These are baseline; the gate stack reads them. Override per-environment via .env.

```yaml
risk:
  daily_loss_pct: 2.0          # kill-switch on cumulative daily P&L
  per_trade_pct: 1.0           # equity at risk per single trade
  max_open_positions: 8
  min_adv_usd: 5_000_000       # 30-day avg dollar volume floor
  earnings_blackout_days: 3    # block entries within N days of earnings
  event_blackout_days: 2       # FOMC, CPI, NFP — wired in v0.2
  stale_signal_days: 14        # disclosures older than this drop
```

## Notes

- Daily kill switch trips when realized + open P&L for the day reaches -2% of equity.
  After that point, no new entries are sent until next session.
- Per-trade size is risk-based: `qty = (equity * 0.01) / |entry - stop|`.
- ATR floor on stop distance: stops cannot be tighter than 1.5 × ATR(20).
- Max 8 concurrent positions to keep the portfolio diversifiable on a small account.
