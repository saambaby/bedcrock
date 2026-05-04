---
type: meta
file: scoring-rules
version: 1
last_updated: ""
---

# Scoring rules

These weights are read by the scorer at runtime. Edit and the next ingest cycle
picks up the change — no redeploy. Track adjustments in `changelog.md`.

```yaml
weights:
  cluster_per_extra_source: 1.0     # +1 per distinct source agreeing on direction within 30d
  cluster_max: 3.0                  # cap on cluster bonus
  size_above_p90: 2.0               # large-size bonus (heuristic; refine with per-trader percentiles)
  insider_corroboration: 2.0        # Form 4 buy in same direction within 30d
  options_flow_corroboration: 2.0   # UW flow in same direction within 14d
  trader_track_record_bonus_max: 2.0
  trend_alignment: 1.0              # +1 if signal direction matches trend regime
  relative_strength_strong: 1.0     # +1 if rs_vs_sector_60d >= 1.0
```

## Component definitions

- **cluster** — distinct sources/traders agreeing on direction in the last 30 days
- **insider_corroboration** — for non-Form-4 buys, +2 if a Form 4 buy on same ticker in 30d
- **options_flow_corroboration** — for non-flow signals, +2 if matching-direction UW flow in 14d
- **size** — heuristic until per-trader size percentiles are backfilled
- **trader_track_record** — populated weekly from realized P&L of actioned signals
- **trend_alignment** — sign matches `trend` from indicator snapshot
- **relative_strength** — `rs_vs_sector_60d` >= 1.0

## To adopt a proposed change

The weekly synthesis writes proposals to `99 Meta/proposals.md`. Move accepted
ones into the YAML block above. The scorer reads YAML in this file directly
(when implemented in v0.2 — for now defaults in code apply).
