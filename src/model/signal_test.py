"""
signal_test.py — Does the signal exist in the data?

Core question: in mismatch matches, does the actual total consistently fall
BELOW what a naive model (blended player averages, no compression) would predict?

That gap IS the edge. The bookmaker uses the naive model. We use compression.

Two baselines tested:
  1. Population mean — simplest possible naive line
  2. Player average blend — per-player rolling average summed (bookie-style)

Test: actual < naive_fair_line more often in MISMATCH than in PARITY?

If yes → the naive model overestimates for mismatch matches → edge exists.

Fair line philosophy (per simulation update):
  The model runs 10k NegBin draws and takes the median.
  Here we use the NAIVE mu (no compression) to set that median —
  simulating what the bookmaker's fair line would look like.

Usage:
    PYTHONUTF8=1 python -m src.model.signal_test
    PYTHONUTF8=1 python -m src.model.signal_test --sport darts
"""

import sys
import argparse
import numpy as np
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from database import get_conn
from src.model.eda_v2 import (
    load_matches,
    build_profiles_darts,
    build_profiles_snooker,
    build_profiles_tennis,
    build_eda_rows_darts,
    build_eda_rows_snooker,
    build_eda_rows_tennis,
    mean,
)

# ── NegBin dispersion — estimated from historical data ───────────────────────
ALPHA = {"darts": 0.2819, "snooker": 0.4468, "tennis": 0.5623}
N_SIMS = 10_000
RNG    = np.random.default_rng(42)


def naive_fair_line(naive_mu: float, sport: str) -> float:
    """Median of NegBin(naive_mu, alpha) — the bookmaker's naive fair line."""
    if naive_mu <= 0:
        return 0.0
    alpha  = ALPHA[sport]
    n_par  = 1.0 / alpha
    p_par  = 1.0 / (1.0 + alpha * naive_mu)
    return float(np.median(RNG.negative_binomial(n_par, p_par, N_SIMS)))


# ── Signal classification ─────────────────────────────────────────────────────
MISMATCH_T = {"darts": 7.0,  "snooker": 0.15, "tennis": 5.0}
PARITY_T   = {"darts": 3.0,  "snooker": 0.05, "tennis": 2.0}
LATE       = {5, 6, 7, 8, 9, 10}
LONG_FMT   = {"darts": 13,   "snooker": 17,   "tennis": 5}

def classify(gap, sport, round_rank, format_max):
    if gap >= MISMATCH_T[sport]:                                          return "MISMATCH"
    if gap <= PARITY_T[sport] and round_rank in LATE and format_max >= LONG_FMT[sport]:
                                                                          return "PARITY"
    return "NEUTRAL"


# ── Analysis ──────────────────────────────────────────────────────────────────

def run(rows, sport, gap_key, pop_mean):
    """
    For each match:
      naive_mu  = population mean (simplest bookmaker proxy)
      fair_line = naive_fair_line(naive_mu) — bookmaker's 50/50 line
      hit_under = actual < fair_line  (1 if actual is under the naive line)

    Also track player-blend baseline:
      blend_mu  = p1_rolling_avg_event_total + p2_rolling_avg_event_total
                  (per-player season average, no compression — what a
                   more sophisticated bookie uses)
    """
    by_signal = defaultdict(list)

    for r in rows:
        actual = r.get("actual_events")
        gap    = r.get(gap_key)
        if actual is None or gap is None:
            continue

        signal = classify(gap, sport, r.get("round_rank", 3), r.get("format_max", 11))

        # Baseline 1: population mean
        fl_pop  = naive_fair_line(pop_mean, sport)
        hit_pop = 1 if actual < fl_pop else 0

        # Baseline 2: player blend (compression-naive)
        # For darts: p1_rolling_180_rate * avg_legs_for_format + p2 same
        # Proxy: pred_rate_x_units from eda (rate model, no gap adjustment)
        blend_mu = r.get("pred_rate_x_units") or pop_mean
        fl_blend = naive_fair_line(blend_mu, sport)
        hit_blend = 1 if actual < fl_blend else 0

        by_signal[signal].append({
            "actual": actual, "gap": gap,
            "hit_pop": hit_pop, "fl_pop": fl_pop,
            "hit_blend": hit_blend, "fl_blend": fl_blend,
        })

    return by_signal


def report(sport, by_signal, pop_mean):
    unit = {"darts":"180s", "snooker":"centuries", "tennis":"aces"}[sport]
    print(f"\n{'='*62}")
    print(f"  SIGNAL TEST — {sport.upper()}")
    print(f"  Naive fair line = NegBin(pop_mean={pop_mean:.2f})")
    print(f"{'='*62}")

    order = ["MISMATCH","NEUTRAL","PARITY"]
    print(f"  {'Signal':<10} {'N':>5}  {'Actual':>8}  "
          f"{'Fair line':>10}  {'Under%':>8}  {'Gap':>8}")
    print(f"  {'-'*10} {'-'*5}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*8}")

    results = {}
    for sig in order:
        rows = by_signal.get(sig, [])
        if not rows:
            print(f"  {sig:<10} {'—':>5}")
            continue
        n         = len(rows)
        m_actual  = mean([r["actual"]   for r in rows])
        m_fl      = mean([r["fl_pop"]   for r in rows])
        under_pct = mean([r["hit_pop"]  for r in rows]) * 100
        gap       = m_fl - m_actual
        print(f"  {sig:<10} {n:>5}  {m_actual:>8.2f}  "
              f"{m_fl:>10.2f}  {under_pct:>7.1f}%  {gap:>+8.2f}")
        results[sig] = {"n": n, "under_pct": under_pct, "mean_actual": m_actual,
                        "mean_fl": m_fl, "gap": gap}

    # Breakdown by gap bucket within MISMATCH
    mis_rows = by_signal.get("MISMATCH", [])
    if mis_rows:
        if sport == "darts":
            buckets = [("gap 7-12",7,12),("gap 12-17",12,17),("gap 17+",17,999)]
        elif sport == "snooker":
            buckets = [("gap 0.15-0.25",0.15,0.25),("gap 0.25-0.35",0.25,0.35),("gap 0.35+",0.35,999)]
        else:
            buckets = [("gap 5-9",5,9),("gap 9-13",9,13),("gap 13+",13,999)]

        print(f"\n  Within MISMATCH — under% by gap size:")
        for bname, blo, bhi in buckets:
            brows = [r for r in mis_rows if blo <= r["gap"] < bhi]
            if len(brows) < 5: continue
            bu = mean([r["hit_pop"] for r in brows]) * 100
            ba = mean([r["actual"]  for r in brows])
            print(f"    {bname:<16} n={len(brows):>3}  actual={ba:.2f}  under={bu:.1f}%")

    # Verdict
    mis = results.get("MISMATCH", {})
    par = results.get("PARITY",   {})
    print()
    mu  = mis.get("under_pct", 50)
    pu  = par.get("under_pct", 50)
    gap = mis.get("gap", 0)

    if mu > 55 and gap > 0:
        mv = "✓ PASS — naive line sits above actual (edge confirmed)"
    elif mu > 50 and gap > 0:
        mv = "~ WEAK — direction correct, effect moderate"
    else:
        mv = "✗ FAIL — no systematic under"

    pv = "✓ PASS" if pu < 45 else ("~ WEAK" if pu < 50 else "✗ FAIL / no data")

    print(f"  MISMATCH: {mv}")
    print(f"  PARITY:   {pv}")
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["darts","snooker","tennis"])
    args = parser.parse_args()
    sports = [args.sport] if args.sport else ["darts","snooker","tennis"]

    conn = get_conn()
    summary = {}

    for sport in sports:
        matches = load_matches(conn, sport)

        # Population mean from full dataset
        stat_col = {"darts":"total_180s","snooker":"total_centuries","tennis":"total_aces"}[sport]
        pop_vals = [r[0] for r in conn.execute(
            f"SELECT {stat_col} FROM matches WHERE sport=? AND {stat_col} IS NOT NULL", (sport,)
        ).fetchall()]
        pop_mean = mean(pop_vals)

        if sport == "darts":
            profiles = build_profiles_darts(matches)
            rows     = build_eda_rows_darts(matches, profiles)
            gap_key  = "abs_gap"
            # add rate×units prediction as blend baseline
            for r in rows:
                if not r.get("pred_rate_x_units"):
                    r["pred_rate_x_units"] = pop_mean

        elif sport == "snooker":
            fwr_dummy, cent_dummy = 0.704, 0.0745
            profiles = build_profiles_snooker(matches, fwr_dummy, cent_dummy)
            rows     = build_eda_rows_snooker(matches, profiles)
            gap_key  = "abs_gap"
            for r in rows:
                r["pred_rate_x_units"] = r.get("pred_model") or pop_mean

        elif sport == "tennis":
            hold_vals = [100 - r["p2_return_pts_won_pct"]
                         for r in matches if r["p2_return_pts_won_pct"]]
            ace_vals  = [r["p1_aces"]/r["p1_svpt"]
                         for r in matches if r["p1_aces"] and r["p1_svpt"]]
            profiles  = build_profiles_tennis(matches, mean(hold_vals), mean(ace_vals))
            rows      = build_eda_rows_tennis(matches, profiles)
            gap_key   = "abs_hold_gap"
            for r in rows:
                r["pred_rate_x_units"] = r.get("pred_model") or pop_mean

        by_signal = run(rows, sport, gap_key, pop_mean)
        results   = report(sport, by_signal, pop_mean)
        summary[sport] = results

    conn.close()

    print(f"\n{'='*62}")
    print(f"  OVERALL — Does the edge exist?")
    print(f"{'='*62}")
    for sport, r in summary.items():
        mis = r.get("MISMATCH", {})
        u   = mis.get("under_pct", 50)
        g   = mis.get("gap", 0)
        n   = mis.get("n", 0)
        icon = "✓" if u > 55 else ("~" if u > 50 else "✗")
        print(f"  {icon} {sport:<10}  mismatch under%={u:.1f}%  "
              f"naive_line above actual by {g:+.2f}  n={n}")

    print()
    print("  If mismatch under% > 55% → naive model overestimates → edge is real.")
    print("  Collect live Betfair lines from ~Mar 15 to measure gap vs market price.")


if __name__ == "__main__":
    main()
