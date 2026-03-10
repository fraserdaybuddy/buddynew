# JOB-006 Model Strategy
## Thesis → Skill Gap → Variables → Tests → Build

**Version:** 3.0 | **Date:** 2026-03-10

---

## Part 1 — The Thesis

```
Expected events = Opportunity count × Event rate

Darts:   Expected 180s  = legs played          × 180s per leg
Snooker: Expected cents = frames played         × centuries per frame
Tennis:  Expected aces  = service games played  × aces per service game
```

Both components are independently predictable. The bookmaker collapses them into one
blended season average. We estimate each separately and multiply. That is the edge.

**Signal A — MISMATCH UNDER**
Large skill gap → dominant player wins units quickly → fewer units played →
weaker player denied opportunities → total events below market line. Bet: UNDER.

**Signal B — PARITY OVER**
Small skill gap + long format + late stage → both players contest every unit →
match goes deep → both players accumulating events → total above line. Bet: OVER.

---

## Part 2 — Skill Gap Definition

Skill gap = expected dominance of Player A over Player B in the unit that controls
match length (leg / frame / service game). It must predict whether the stronger player
will compress the match by winning units faster.

Two gap types are needed:

```
absolute_gap = |strength_A − strength_B|   → thesis test (how much compression)
signed_gap   = strength_A − strength_B     → simulation direction (who dominates)
```

All scores normalised to z-scores within sport before computing gap, so thresholds
are comparable across sports (gap > 1.0σ = mismatch, < 0.3σ = parity).

### Three layers — build in order

| Layer | Metric | When to use |
|-------|--------|-------------|
| 1 — Proxy | Single available stat | Start here, validate thesis |
| 2 — Composite | Weighted z-score of core stats | Production model |
| 3 — Latent rating | Elo / Bradley-Terry from outcomes | Future phase |

---

### Darts Skill Gap

Predicts leg-win probability. Average captures scoring power, checkout converts it.

**Layer 1:**
```
skill_gap = avg_A − avg_B
```

**Layer 2:**
```
strength = 0.55 × z(3dart_avg) + 0.25 × z(checkout_pct) + 0.20 × z(140plus_per_leg)
skill_gap = strength_A − strength_B
```

| Metric | Coverage | Source |
|--------|----------|--------|
| 3-dart avg | 845 / 1,230 | staging_darts.p1_avg |
| Checkout % | 845 / 1,230 | staging_darts.p1_checkout_pct |
| 140+ per leg | 845 / 1,230 (once legs parsed) | staging_darts.p1_140plus |
| Legs played | 1,220 / 1,230 | parse p1_score + p2_score |

Start Layer 1. Add checkout % and 140+ only if they materially improve prediction
of legs played vs avg alone.

---

### Snooker Skill Gap

Predicts frame-win probability. Ranking is slow-moving; frame win rate is more current.

**Layer 1:**
```
skill_gap = log(ranking_pts_A) − log(ranking_pts_B)
```
Log is correct — the gap between rank 1 and rank 10 is not the same as rank 50 vs 60.

**Layer 2:**
```
strength = 0.45 × z(log_ranking_pts) + 0.35 × z(frame_win_rate) + 0.20 × z(century_rate_per_frame)
skill_gap = strength_A − strength_B
```

| Metric | Coverage | Source |
|--------|----------|--------|
| Ranking points | 0% — not yet sourced | snooker.org API (free) |
| Frame win rate | 2,148 / 2,185 | p1_frames / (p1+p2 frames) from staging |
| Century rate/frame | 2,148 / 2,185 | p1_centuries / total_frames from staging |

Use frame_win_rate as Layer 1 proxy until rankings sourced. Rankings are the blocker.

---

### Tennis Skill Gap

Tennis requires two separate gap measures because the ace market has two drivers:

```
competitiveness_gap  → how long the match goes     → drives service game count
serve_event_profile  → ace production per game      → drives event rate
```

**Layer 1:**
```
serve_strength  = 100 − opponent_ret_pts_won_pct   (proxy for service pts won %)
competitiveness_gap = serve_strength_A − serve_strength_B   (surface-specific)
```

**Layer 2:**
```
strength = 0.50 × z(serve_strength_surface)
         + 0.35 × z(return_strength_surface)
         + 0.15 × z(ace_rate_surface)

competitiveness_gap  = strength_A − strength_B
serve_event_profile  = ace_rate_A + ace_rate_B      (sum — both players contribute)
```

| Metric | Coverage | Source |
|--------|----------|--------|
| ret_pts_won_pct (both) | 5,640 / 5,765 | staging_tennis |
| serve_strength (derived) | 5,640 / 5,765 | 100 − opponent_ret_pts_won_pct |
| Ace rate per svpt | 5,640 / 5,765 | aces / svpt from staging |
| Surface | 5,765 / 5,765 | staging_tennis + tournaments |

No ATP ranking needed — serve/return strength from match data is a better predictor
for this specific market than broad ranking.

---

## Part 3 — Phase 1: Prove the Core Thesis

Minimum variables only. Three claims. Fail any one → diagnose before building further.

### Claim 1 — Skill gap predicts opportunity count
```
legs_played ~ absolute_skill_gap + format                (darts)
frames_played ~ absolute_skill_gap + format              (snooker)
service_games ~ absolute_skill_gap + format + surface    (tennis)
```
Expect: larger gap → fewer units in same format. If flat → thesis fails here.

### Claim 2 — Units × rate predicts total events
```
predicted = expected_units × player_rate_per_unit
```
Expected: lower residual vs actual than naive season average baseline.
If model doesn't beat naive average → model adds no value.

### Claim 3 — Prediction diverges from market line
```
edge = model_probability − market_implied_probability
```
Needs market lines. Proxy until sourced: use population median as synthetic line.
If prediction never diverges meaningfully → market already prices this → no edge.

---

## Part 4 — Phase 2: Full Model Variables

Only built once Phase 1 claims are validated.

### Tier A — Core drivers (60–70% weight)
*Must be present. If missing → no bet.*

| Variable | Darts | Snooker | Tennis |
|----------|-------|---------|--------|
| Skill gap | avg differential | log ranking pts + frame win rate | serve/return strength diff |
| Expected units | legs sim (format + gap) | frames sim (format + gap) | service games (format + gap + surface) |
| P1 event rate | 180s per leg | centuries per frame | aces per service game (surface) |
| P2 event rate | same | same | same |
| Format | BO11/BO13/BO19 | BO9/BO17/BO35 | BO3/BO5 |
| Surface | — | — | grass/hard/clay |

---

### Tier B — Context modifiers (20–30% weight)
*Applied after Tier A fires. Adjusts probability up or down.*

**Stage**
Round of competition amplifies the signal.
- R1 mismatch → stronger UNDER (bigger typical skill gap than later rounds)
- SF/F parity → stronger OVER (elite-only field, contested throughout)
Available: matches.round ✓ all sports

**Recency trend**
Direction of form matters as much as level. A player whose last 5 rates are trending
up is different from one with the same mean but trending down.
- Computed as slope of rolling rate window (oldest to newest)
- Positive slope → rate likely understated → amplifies OVER signal
- Negative slope → rate likely overstated → reduces confidence
Available: computable from rolling form window ✓

**Style interaction (snooker only)**
Century rate classifies player style:
- Attacking: rate > 0.35 per frame
- Balanced: 0.20–0.35
- Safety: < 0.20

Attacking vs attacking → amplifies OVER. Safety vs anyone → suppresses event rate.
Available: computable from century_rate_per_frame ✓

---

### Tier C — Situational signals (5–10% weight)
*Only applied when evidence supports them. Easy to overfit — treat with caution.*

**Head-to-head history**
Some player pairings structurally produce higher or lower totals regardless of form.
This may reflect style compatibility, psychological dynamics, or tactical patterns.
- Compute mean total events in last 5 H2H matches for this specific pairing
- Only apply if N ≥ 5 H2H matches exist
- Weight: small modifier on event rate, not on unit count
Available: derivable from matches table ✓ (requires player pairing lookup)

**Breakout / sparse player handling**
Players with insufficient history get shrinkage-adjusted rates rather than raw rates.

| Tier | Matches | Treatment |
|------|---------|-----------|
| 1 | 10+ | Full observed rate |
| 2 | 3–9 | Shrink toward comparable player prior |
| 3 | 0–2 | Shrink toward field average, raise edge threshold |

```
true_rate = w1 × observed_rate + w2 × comparable_prior + w3 × field_average
```
Weights shift toward field average as sample size shrinks.
Available: computable from player match history ✓

---

### Tier D — Risk filters (always applied, operational controls)
*Not predictive. These govern whether a bet fires, not what the model predicts.*

| Filter | Rule | Action |
|--------|------|--------|
| Stale form | Last match > 30 days ago | NO BET |
| Low sample | < 3 matches in form window | NO BET |
| Missing stats | Event rate = NULL | NO BET |
| Low liquidity | Matched volume < £50 | NO BET |
| Tier 3 player | Emerging / sparse | Edge threshold raised to 9% |
| Tier 2 player | Partial data | Edge threshold raised to 7% |
| Model divergence | Model vs market > 20% | FLAG for review |

---

## Part 5 — What Is NOT Included (and Why)

| Excluded | Reason |
|----------|--------|
| Fatigue / matches per day | Second-order effect, not measurable reliably |
| Venue effects | Insufficient venue-specific data in our DB |
| Table conditions (snooker) | Not in our data |
| Pressure index (deciding leg) | Requires in-play data |
| Time of day / session | Small effect, not worth the complexity |
| ATP/WTA broad ranking | Serve/return strength from match data is more specific |
| Tournament-specific rates | Too sparse per tournament to be stable |

If Phase 1 validates cleanly and we have excess predictive budget, revisit this list.
But not before.

---

## Part 6 — Data Blockers (Ordered by Priority)

| Blocker | Impact | Fix | Effort |
|---------|--------|-----|--------|
| avg/frames/svpt not in matches | Tier A rates uncomputable | Schema migration + promote from staging | Low |
| Legs played not parsed | Darts 180s-per-leg rate blocked | Parse p1_score + p2_score | Low |
| Snooker ranking not stored | Layer 2 composite incomplete | snooker.org API scrape | Medium |
| Market lines empty | Claim 3 and edge calculation blocked | Oddsportal scrape + forward Sportmarket logger | Medium |

---

## Part 7 — Build Sequence

```
Phase 0 — Schema + data enrichment
  Promote avg, checkout%, 140+ from staging_darts → matches
  Parse legs_played from score string
  Promote p1_frames, p2_frames from staging_snooker → matches
  Promote svpt, ret_pts_won_pct from staging_tennis → matches
  Source snooker.org rankings → players table

Phase 1 — Validate core thesis
  Compute skill gap (Layer 1) per sport
  Run Claims 1, 2, 3
  Gate: fail = stop and diagnose. Pass = proceed.

Phase 2 — Form builder
  Rolling 7-match form window with decay weights
  Per-unit event rates (trimmed mean + median for outlier robustness)
  Recency trend slope
  Player tier classification (1/2/3)
  Snooker style classification

Phase 3 — Simulation model
  Monte Carlo: units played distribution (10k runs)
  Monte Carlo: events per unit distribution
  Full PMF over all possible totals
  Signal classifier (MISMATCH_UNDER / PARITY_OVER / NO_SIGNAL)

Phase 4 — Line sourcing (parallel with Phase 2–3)
  Forward Sportmarket line logger
  Oddsportal historical scrape attempt

Phase 5 — Edge calculation + backtest
  Edge = model_p − market_implied_p at every available line
  Walk-forward backtest (no look-ahead)
  Add Tier B and C variables. Measure marginal improvement.

Phase 6 — Production
  Tier D filters always on
  Paper trade minimum 4 weeks / 50 bets
  Live only after Gate 3 clears
```

---

## Part 8 — Decision Gates

| Gate | After | Pass criterion | Fail action |
|------|-------|----------------|-------------|
| Gate 1 | Phase 1 | Claims 1+2 hold for ≥ 2 sports | Pause failing sport, continue others |
| Gate 2 | Phase 2 form | Form predicts next match better than season avg | Review window length and decay weights |
| Gate 3 | Phase 5 backtest | ROI > 0%, ≥ 100 bets, stable across years | Paper only, do not go live |
| Gate 4 | Phase 6 paper | ROI > 2%, drawdown < 15% over 4 weeks | Extend paper period |
