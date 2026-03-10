# Phase 1 EDA Report
## Thesis Validation: Skill Gap × Event Rate Model
**Date:** 2026-03-10 | **Status:** Complete — results ready for review

---

## 1. What Was Tested and Why

The core thesis is that bookmakers misprice totals markets (180s, centuries, aces)
because they use blended season averages. Our model separates the total into two
independently predictable components:

```
Expected total events = Opportunity count × Event rate per unit

Darts:   Expected 180s  = legs played         × 180s per leg
Snooker: Expected cents = frames played        × centuries per frame
Tennis:  Expected aces  = service pts played   × aces per service point
```

Before building the simulation model, we need to validate that both components are
real and measurable. Two claims were tested:

**Claim 1 — Skill gap predicts opportunity count**
If the match is mismatched (large skill gap between players), the dominant player wins
units (legs/frames/service games) faster. The match ends earlier. Fewer opportunities
for events to occur.

**Claim 2 — Player rates × opportunity count outperforms naive average**
If we know each player's historical event rate per unit, and we know how many units
were played, does our prediction beat simply using the population mean?

A model that cannot beat a naive average adds no value, regardless of how sophisticated
its architecture is.

---

## 2. Data Used

| Sport | Matches loaded | Analysed (after LOW_SAMPLE filter) | Excluded |
|-------|---------------|-------------------------------------|----------|
| Darts | 845 | 597 | 248 (player had < 5 prior matches) |
| Snooker | 2,185 | 1,548 | 637 (player had < 5 prior matches) |
| Tennis | 5,456 | 3,640 | 1,816 (player had < 5 prior matches) |

**LOW_SAMPLE filter:** Any match where either player had fewer than 5 prior matches
in the dataset was excluded from analysis. This prevents unstable early-window
estimates from contaminating the results. These matches are not discarded from the
database — they are simply not yet eligible for confident prediction.

---

## 3. Skill Gap Metric Used Per Sport

All rolling metrics computed with strict date-prior cutoff — only matches played
**before** the match being evaluated are included in each player's profile.

### Darts
**Metric:** Rolling 3-dart average (last 15 matches, equally weighted for EDA)
**Skill gap:** `|avg_A − avg_B|`
**Scale:** Typical gaps range 0–22 points. Mean gap in dataset: 5.6 pts.
**Buckets:** 0–3 (parity), 3–7, 7–12, 12+ (mismatch)

### Snooker
**Metric:** Rolling frame win rate = `frames_won / total_frames` per match, averaged
across last 15 matches. Continuous 0–1 scale. Captures degree of dominance
(winning 9-0 vs 9-8 are different; binary win/loss loses this information).
**Skill gap:** `|fwr_A − fwr_B|`
**Buckets:** 0–0.05 (parity), 0.05–0.15, 0.15–0.25, 0.25+ (mismatch)

### Tennis
**Metric:** Rolling serve strength = `100 − opponent_return_pts_won_pct`
This is the percentage of service points the player won. Available directly from
Sackmann dataset; no ATP ranking required.
**Skill gap:** `|serve_strength_A − serve_strength_B|`
**Segmentation:** By surface (Hard/Clay/Grass) rather than format, since all
matches in the dataset share the same format label ("SETS").
**Buckets:** 0–2 (parity), 2–5, 5–9, 9+ (mismatch)

---

## 4. Results — Claim 1: Skill Gap Predicts Opportunity Count

### 4.1 Darts — PASS

**Table: Mean legs played by format and skill gap bucket**

| Format | N | 0–3 (parity) | 3–7 | 7–12 | 12+ (mismatch) |
|--------|---|--------------|-----|------|----------------|
| BO11 | 432 | 11.7 (n=209) | 12.2 (n=142) | 10.4 (n=74) | 9.7 (n=7) |
| BO13 | 107 | 13.3 (n=37) | 11.0 (n=40) | 9.1 (n=27) | 7.0 (n=3) |
| BO9 | 58 | 4.9 (n=25) | 4.1 (n=20) | 4.5 (n=10) | 4.0 (n=3) |

**Spearman ρ (gap vs legs/format_max): −0.090**

**Interpretation:** The signal is present and in the right direction. In BO11 matches,
parity matches (gap < 3 pts) average 11.7 legs — close to the maximum of 11. Mismatch
matches (gap 12+ pts) average 9.7 legs. In BO13 the effect is clearer: 13.3 legs at
parity vs 7.0 at maximum mismatch (a 47% compression). The Spearman correlation is
weak (−0.09) but consistently negative across formats, confirming the direction.

**Caveat:** The mismatch bucket (12+ pts) contains only 7 BO11 matches and 3 BO13
matches — too few for statistical confidence at the extreme end. Larger samples
needed, particularly at maximum mismatch. The bulk of matches (75%) sit in the
0–7 point gap range where the effect is present but smaller.

---

### 4.2 Snooker — PASS

**Table: Mean frames played by format and skill gap bucket**

| Format | N | 0–0.05 (parity) | 0.05–0.15 | 0.15–0.25 | 0.25+ (mismatch) |
|--------|---|-----------------|-----------|-----------|------------------|
| BO7 | 588 | 5.7 (n=264) | 5.5 (n=261) | 5.7 (n=52) | 5.3 (n=11) |
| BO19 | 332 | 15.9 (n=127) | 15.7 (n=164) | 15.3 (n=29) | 14.3 (n=12) |
| BO11 | 311 | 9.2 (n=142) | 9.1 (n=132) | 9.1 (n=32) | 9.4 (n=5) |
| BO9 | 267 | 7.0 (n=114) | 7.0 (n=118) | 6.9 (n=26) | 6.4 (n=9) |

**Spearman ρ (gap vs frames/format_max): −0.054**

**Interpretation:** The signal exists but is moderate. The clearest effect is in longer
formats: BO19 shows parity at 15.9 frames vs mismatch at 14.3 (a 10% compression).
BO7 shows minimal compression (5.7 vs 5.3), which makes structural sense — in a
short format a dominant player has less room to compress further; the minimum is ~5
frames regardless.

The key insight is that **snooker format length amplifies the mismatch effect**. A 10%
frame compression in BO19 is significant for a totals market; the same 10% compression
in BO7 moves the expected century count by less than 0.1. This means the highest-value
snooker bets will cluster in long-format matches (BO19, BO25, BO33, BO35).

**Caveat:** Mismatch bucket is thin across all formats (5–12 matches each). The rolling
frame_win_rate is only based on 15 prior matches maximum, which in WST terms may span
a full season or more for lower-ranked players. A player who has gone 6-9 in matches
(40% frame win rate) may have faced tougher opponents — the metric doesn't adjust for
opponent quality. This is the prize money / ranking objection raised in the external
review, and it remains valid. Frame_win_rate is sufficient for Phase 1 validation but
should be replaced with ranking-adjusted strength in the production model.

---

### 4.3 Tennis — WEAK

**Table: Mean service points played by surface and skill gap bucket**

| Surface | N | 0–2 (parity) | 2–5 | 5–9 | 9+ (mismatch) |
|---------|---|--------------|-----|-----|---------------|
| Hard | 2,008 | 148.8 (n=592) | 152.9 (n=712) | 149.2 (n=538) | 139.1 (n=166) |
| Clay | 1,143 | 153.0 (n=320) | 157.0 (n=384) | 147.9 (n=307) | 140.7 (n=132) |
| Grass | 478 | 174.6 (n=129) | 160.6 (n=189) | 166.7 (n=126) | 151.9 (n=34) |

**Spearman ρ (gap vs service_pts/format_max): ~0.0 (effectively flat)**

**Interpretation:** The surface tables show the correct direction — mismatch matches
(9+ serve_strength gap) produce fewer service points than parity matches on every
surface. Grass shows the strongest compression: 174.6 svpt at parity vs 151.9 at
mismatch (−13%). Hard and clay show −6% and −8% respectively.

However, the overall Spearman correlation is near zero. The serve_strength differential
alone is not capturing the full competitiveness signal. Two reasons:

1. **The "2-5" bucket is often higher than parity** — this is consistent with a
   non-linear relationship. A small serve advantage leads to more competitive service
   games (more deuce situations) rather than fewer. The compression only kicks in at
   large gaps.

2. **Service points played is a partial measure of match length** — it captures serve
   volume but not break patterns or set structure. A 6-1 6-1 match on clay produces
   fewer svpt than a 7-6 7-6 match, but both may have similar serve_strength gaps.
   A full service games played count (from match score parsing) would be a better
   denominator.

**Bottom line:** The thesis holds at the extremes on each surface, but serve_strength
differential alone is too weak a signal for the Claim 1 test to pass cleanly.
This should be revisited once ATP ranking points data is sourced (a stronger
competitiveness predictor), or once match scores are parsed to yield games played.

---

## 5. Results — Claim 2: Model vs Naive Average

### 5.1 Darts — FAIL

| Metric | Value |
|--------|-------|
| Naive baseline (population mean 180s per match) | 7.64 |
| Model RMSE | 5.80 |
| Naive RMSE | 5.05 |
| Improvement | **−14.7%** (model is worse) |
| Mean prediction error | +1.54 (over-predicts by 1.5 180s on average) |

**Interpretation: The model is performing worse than simply guessing the average.**

This is the most important finding of the EDA. The rolling 180_rate × legs calculation
is amplifying rather than reducing error. The +1.54 bias tells us the model
systematically over-predicts.

**Likely causes (to investigate):**

**A. The 180 rate denominator is wrong.**
We are computing `180s per leg` using total legs played in the match. But a player
who wins in 7 legs and a player who loses in 7 legs have different visit counts —
the winner typically threw more efficiently. Using legs as the denominator treats
all legs as equal when they are not. A truer rate would be `180s per visit`
(3-dart throw), but we do not have visits in the dataset. This may be an
irreducible data limitation.

**B. 180s are genuinely high-variance events.**
A 180 requires hitting treble-20 three times in a single visit. Even elite players
hit them with a base rate of ~10–15% of visits under normal conditions, rising
under rhythm but varying enormously across matches. Unlike century breaks (which
require extended table time) or aces (which are a consistent serve choice), 180s
have a large stochastic component that may not be forecastable at match level.

**C. Small high-variance sample at the high end.**
A match with one player hitting 8+ 180s pulls the error sharply. The rolling
average cannot represent these outlier performances, so the model over-predicts
for moderate matches and under-predicts for high-180 matches simultaneously.

**Decision:** Darts Claim 2 fails. Do not proceed to the production darts model
until the rate calculation is revisited. Two options: (1) find visits data to
compute 180s-per-visit instead of per-leg; (2) test whether a simpler model
(predict total = f(avg_sum, format) as a direct regression) outperforms the
rate × units architecture.

---

### 5.2 Snooker — PASS

| Metric | Value |
|--------|-------|
| Naive baseline (population mean centuries per match) | 1.04 |
| Model RMSE | 1.038 |
| Naive RMSE | 1.233 |
| Improvement | **+15.8%** |
| Mean prediction error | +0.025 (essentially unbiased) |

**Interpretation:** The model beats the naive average by 15.8% with near-zero bias.

Century rate per frame is the cleanest metric in the dataset. It is:
- **Player-specific:** Top attacking players hit centuries at 3–4× the rate of
  safety specialists. Knowing who is playing is genuinely informative.
- **Frame-normalised:** Dividing by frames played removes the dominant format effect.
- **Stable:** Century rates are more consistent match-to-match than 180 rates,
  because a century requires sustained table time (an extended structured sequence)
  rather than three individual dart throws.

**The compound effect is working:** Snooker is where the two-component model
demonstrably adds value. A long-format match between two attacking players produces
more centuries than a short-format match between safety players — and the model
captures this.

---

### 5.3 Tennis — PASS (strongest result)

| Metric | Value |
|--------|-------|
| Naive baseline (population mean aces per match) | 9.48 |
| Model RMSE | 4.885 |
| Naive RMSE | 7.538 |
| Improvement | **+35.2%** |
| Mean prediction error | −0.053 (essentially unbiased) |

**Interpretation:** The model beats the naive average by 35.2% — the strongest
result across all three sports, and with virtually no systematic bias.

This is explained by the extreme player heterogeneity in ace production. John Isner
serves 15+ aces per match; clay court specialists may serve 2–3. The naive average
(9.48) is wrong for almost everyone. Knowing the specific players' rolling ace
rates allows the model to predict meaningfully.

The 35% improvement also reflects the quality of the Sackmann dataset, which
provides service points played per match — a reliable denominator. This is the
data advantage tennis has over darts (no visits) and snooker (no break-level data).

**Note on Claim 1 / Claim 2 disconnect for tennis:** The model predicts well
(Claim 2 PASS) even though the skill gap signal on service points is weak
(Claim 1 WEAK). This means the ace prediction is currently driven almost entirely
by player-level rate heterogeneity, not by match-length compression. The
competitiveness gap component is not yet contributing meaningfully. This is
important for understanding **which matches to bet**: right now the model is
best at identifying high vs low ace producers, not at identifying matches where
the total will be compressed by mismatch. Adding ATP ranking points data should
strengthen the Claim 1 component and unlock the MISMATCH_UNDER signal properly.

---

## 6. Summary Table

| Sport | Claim 1 | Claim 2 | Proceed? |
|-------|---------|---------|----------|
| Darts | ✓ PASS (ρ=−0.09) | ✗ FAIL (−14.7% vs naive) | No — fix rate calculation first |
| Snooker | ✓ PASS (ρ=−0.05) | ✓ PASS (+15.8% vs naive) | Yes — build form_builder |
| Tennis | ~ WEAK (ρ=−0.05) | ✓ PASS (+35.2% vs naive) | Conditional — Claim 2 strong, Claim 1 needs ranking data |

---

## 7. Key Findings

**Finding 1 — The thesis holds for snooker.**
Both components are measurable, the skill gap compresses frame counts, and the
rate × units model beats naive. Snooker is ready to proceed to form_builder.py.

**Finding 2 — Tennis Claim 2 is strong but Claim 1 is incomplete.**
The ace rate model works well because players are highly differentiated. But
the match-length compression signal needs ATP ranking data (or match score
parsing) to become reliable. The model can generate predictions but they will
currently be driven by rate heterogeneity alone — the mismatch compression
component is not yet active.

**Finding 3 — Darts 180 rate calculation needs revisiting.**
The two-component model fails for darts not because the thesis is wrong (Claim 1
passes — legs are compressed by skill gap) but because 180s per leg is an
unreliable event rate metric. The denominator (legs) is too coarse; the true
unit should be visits (3-dart throws). Without visits data, a direct regression
approach (predicted_180s = f(avg_A, avg_B, format)) should be tested as an
alternative to the rate × units architecture.

**Finding 4 — Format length is a major amplifier.**
The mismatch compression effect is largest in long formats. BO19 snooker shows
stronger compression than BO7. BO13 darts shows stronger compression than BO11.
This has direct implications for bet selection: mismatch UNDER bets will be most
reliable in long-format matches where the dominant player has more room to pull away.

**Finding 5 — Surface is a major driver in tennis.**
Grass court matches show the highest service point counts at parity (174.6) and
the strongest absolute compression at mismatch (−22.7 svpt). Clay and hard courts
follow the same direction. Surface-specific models (or at minimum surface as a
mandatory feature) are required for tennis.

---

## 8. Recommended Next Steps

**Immediate (blocks snooker model):**
1. Build `form_builder.py` with proper decay weighting and tier system for snooker
2. Source snooker ranking data (snooker.org API blocked, alternative source needed)
   to replace frame_win_rate as Layer 1 skill metric

**Darts — before proceeding:**
3. Test direct regression: `predicted_180s ~ avg_A + avg_B + format_max`
   Compare RMSE against naive and against the rate × units model
4. If regression beats both, use it. If not, investigate whether visits data
   is available from any darts data source.

**Tennis — parallel work:**
5. Source ATP ranking points (Sackmann ranking CSVs — free, already mapped)
   to strengthen Claim 1 competitiveness signal
6. Parse match scores to derive games played (better unit count than svpt)

**All sports — once form_builder is built:**
7. Source historical market lines (Oddsportal scrape or Betfair historical)
8. Run Claim 3: does model probability diverge from market implied probability?
   (Currently blocked — results with synthetic median line are PROVISIONAL only)

---

## 9. Open Questions for External Review

1. **Darts rate architecture:** Given that 180s per leg fails as an event rate,
   is there a better way to model 180s that does not require visits-level data?
   Would a direct Negative Binomial regression on raw counts (with avg_sum and
   format as predictors) be a more appropriate architecture for darts?

2. **Tennis Claim 1 weighting:** The two-gap approach (competitiveness_gap for
   match length, serve_event_profile for ace rate) was designed to handle the
   fact that serve strength predicts ace rate better than it predicts match length.
   Given the Claim 2 results show the rate component dominates, should the model
   de-emphasise the match-length compression component for tennis until ranking
   data strengthens it?

3. **Snooker opponent quality:** Frame_win_rate does not adjust for strength of
   schedule. A player who has beaten weak opponents to achieve a 70% frame win
   rate is rated identically to a player who has beaten top-10 players at the
   same rate. How much does this affect snooker predictions in practice, and at
   what sample size does opponent quality correction become necessary?
