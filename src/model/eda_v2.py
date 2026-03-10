"""
eda_v2.py — Phase 1 EDA rerun with three reviewer-recommended changes:

1. Darts: direct OLS regression (avg_A + avg_B + format_max + round + avg_A×avg_B)
   instead of rate × units decomposition. NegBin architecture comes in model build.

2. Tennis Claim 1: hold% differential = |hold_A − hold_B|
   where hold_pct = 100 − opponent_ret_pts_won_pct (surface-specific rolling window)

3. Snooker: replace LOW_SAMPLE exclusion with shrinkage
   Tier 3 (0–2 matches): 0.30 × observed + 0.70 × field_avg
   Tier 2 (3–4 matches): 0.60 × observed + 0.40 × field_avg
   Tier 1 (5+ matches):  observed (no shrinkage)

All rolling stats: strict date-prior cutoff.

Usage:
    PYTHONUTF8=1 python -m src.model.eda_v2
    PYTHONUTF8=1 python -m src.model.eda_v2 --plot
"""

import sys
import argparse
import statistics
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from database import get_conn

FORM_WINDOW = 15
SHRINK_TIER1 = 5   # 5+ matches → no shrinkage
SHRINK_TIER2 = 3   # 3–4 matches → 60/40 blend
                   # 0–2 matches → 30/70 blend

SHRINK_WEIGHTS = {
    "tier1": (1.00, 0.00),   # (observed_weight, field_avg_weight)
    "tier2": (0.60, 0.40),
    "tier3": (0.30, 0.70),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def mean(xs): return sum(xs) / len(xs) if xs else 0.0
def mae(pred, actual):
    pairs = [(p, a) for p, a in zip(pred, actual) if p is not None and a is not None]
    return mean([abs(p - a) for p, a in pairs]) if pairs else None


def player_tier(n: int) -> str:
    if n >= SHRINK_TIER1: return "tier1"
    if n >= SHRINK_TIER2: return "tier2"
    return "tier3"


def shrink(observed: Optional[float], field_avg: float, tier: str) -> float:
    w_obs, w_fld = SHRINK_WEIGHTS[tier]
    if observed is None:
        return field_avg
    return w_obs * observed + w_fld * field_avg


def spearman(xs, ys):
    n = len(xs)
    if n < 5: return 0.0
    def rank(lst):
        s = sorted(range(n), key=lambda i: lst[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and lst[s[j]] == lst[s[j+1]]: j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j+1): r[s[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    d2 = sum((rx[i]-ry[i])**2 for i in range(n))
    return 1 - 6*d2 / (n*(n**2-1))


def ols_r2(xs, ys):
    """Simple OLS R² for one predictor."""
    n = len(xs)
    if n < 5: return 0.0
    xm, ym = mean(xs), mean(ys)
    sxy = sum((x-xm)*(y-ym) for x,y in zip(xs,ys))
    sxx = sum((x-xm)**2 for x in xs)
    if sxx == 0: return 0.0
    b1 = sxy / sxx
    b0 = ym - b1 * xm
    ss_res = sum((y - (b0 + b1*x))**2 for x,y in zip(xs,ys))
    ss_tot = sum((y - ym)**2 for y in ys)
    return 1 - ss_res/ss_tot if ss_tot else 0.0


def multivar_ols(X, y):
    """OLS with numpy for multi-feature regression. Returns (coeffs, r2, predictions)."""
    X_arr = np.array(X)
    y_arr = np.array(y)
    X_b   = np.column_stack([np.ones(len(X_arr)), X_arr])
    try:
        coeffs = np.linalg.lstsq(X_b, y_arr, rcond=None)[0]
        preds  = X_b @ coeffs
        ss_res = np.sum((y_arr - preds)**2)
        ss_tot = np.sum((y_arr - y_arr.mean())**2)
        r2     = 1 - ss_res/ss_tot if ss_tot else 0.0
        return coeffs, r2, preds.tolist()
    except Exception:
        return None, 0.0, [mean(y)] * len(y)


def parse_format_max(fmt):
    import re
    m = re.search(r'(\d+)', fmt or '')
    return int(m.group(1)) if m else 0


# ── Data loading ─────────────────────────────────────────────────────────────

def load_matches(conn, sport):
    sql = """
        SELECT m.match_id, m.sport, m.match_date, m.round, m.format,
               m.tournament_id, m.player1_id, m.player2_id,
               m.p1_avg, m.p2_avg, m.p1_legs_won, m.p2_legs_won,
               m.p1_180s, m.p2_180s,
               m.p1_frames_won, m.p2_frames_won, m.legs_sets_total,
               m.p1_centuries, m.p2_centuries,
               m.p1_svpt, m.p2_svpt, m.p1_aces, m.p2_aces,
               m.p1_return_pts_won_pct, m.p2_return_pts_won_pct,
               t.surface
        FROM matches m
        LEFT JOIN tournaments t ON t.tournament_id = m.tournament_id
        WHERE m.sport = ?
        ORDER BY m.match_date ASC
    """
    rows = conn.execute(sql, (sport,)).fetchall()
    return [dict(r) for r in rows]


ROUND_RANK = {
    "1/64-finals": 1, "1/32-finals": 2, "1/16-finals": 3,
    "1/8-finals": 4, "Quarter-finals": 5, "Semi-finals": 6, "Final": 7,
    "LAST_128": 2, "LAST_96": 3, "LAST_80": 4, "R64": 5, "R32": 6,
    "R16": 7, "QF": 8, "SF": 9, "F": 10,
    "R128": 1, "R32_tennis": 3, "R16_tennis": 4,
}


# ── Rolling player profiles ───────────────────────────────────────────────────

def build_profiles_darts(matches):
    """Returns {match_id: {player_id: {avg, rate_180, n}}}"""
    history = defaultdict(list)  # player_id → list of {date, avg, rate_180, legs}
    profiles = {}

    for m in matches:
        p1, p2 = m["player1_id"], m["player2_id"]
        profiles[m["match_id"]] = {
            p1: _profile_darts(history[p1]),
            p2: _profile_darts(history[p2]),
        }
        # Append to history after computing profiles (no look-ahead)
        total_legs = (m["p1_legs_won"] or 0) + (m["p2_legs_won"] or 0)
        if m["p1_avg"]:
            history[p1].append({
                "date": m["match_date"], "avg": m["p1_avg"],
                "legs": total_legs, "events": m["p1_180s"],
            })
        if m["p2_avg"]:
            history[p2].append({
                "date": m["match_date"], "avg": m["p2_avg"],
                "legs": total_legs, "events": m["p2_180s"],
            })
    return profiles


def _profile_darts(hist):
    w = hist[-FORM_WINDOW:]
    n = len(w)
    tier = player_tier(n)
    avg_vals   = [h["avg"]    for h in w if h["avg"]    is not None]
    event_vals = [h["events"] for h in w if h["events"] is not None and h["legs"]]
    leg_vals   = [h["legs"]   for h in w if h["events"] is not None and h["legs"]]
    avg        = mean(avg_vals) if avg_vals else None
    rate_180   = sum(event_vals)/sum(leg_vals) if sum(leg_vals or [0]) > 0 else None
    return {"n": n, "tier": tier, "avg": avg, "rate_180": rate_180}


def build_profiles_snooker(matches, field_fwr, field_cent_rate):
    """With shrinkage. Returns {match_id: {player_id: {fwr, cent_rate, n, tier}}}"""
    history = defaultdict(list)
    profiles = {}

    for m in matches:
        p1, p2 = m["player1_id"], m["player2_id"]
        profiles[m["match_id"]] = {
            p1: _profile_snooker(history[p1], field_fwr, field_cent_rate),
            p2: _profile_snooker(history[p2], field_fwr, field_cent_rate),
        }
        total_frames = m["legs_sets_total"]
        if m["p1_frames_won"] is not None and total_frames:
            history[p1].append({
                "date": m["match_date"],
                "fwr":  m["p1_frames_won"] / total_frames,
                "frames": total_frames,
                "events": m["p1_centuries"],
            })
        if m["p2_frames_won"] is not None and total_frames:
            history[p2].append({
                "date": m["match_date"],
                "fwr":  m["p2_frames_won"] / total_frames,
                "frames": total_frames,
                "events": m["p2_centuries"],
            })
    return profiles


def _profile_snooker(hist, field_fwr, field_cent_rate):
    w = hist[-FORM_WINDOW:]
    n = len(w)
    tier = player_tier(n)
    fwr_vals   = [h["fwr"]    for h in w if h["fwr"]    is not None]
    event_vals = [h["events"] for h in w if h["events"] is not None and h["frames"]]
    frame_vals = [h["frames"] for h in w if h["events"] is not None and h["frames"]]
    obs_fwr    = mean(fwr_vals)    if fwr_vals  else None
    obs_cent   = sum(event_vals)/sum(frame_vals) if sum(frame_vals or [0]) > 0 else None
    # Apply shrinkage
    fwr_shrunk  = shrink(obs_fwr,  field_fwr,       tier)
    cent_shrunk = shrink(obs_cent, field_cent_rate,  tier)
    return {"n": n, "tier": tier, "fwr": fwr_shrunk, "cent_rate": cent_shrunk}


def build_profiles_tennis(matches, field_hold, field_ace_rate):
    """
    Hold% = 100 − opponent_ret_pts_won_pct (surface-specific rolling window).
    Falls back to all-surface if < 3 surface-specific matches.
    """
    # history keyed by (player_id, surface)
    history_surf = defaultdict(list)
    history_all  = defaultdict(list)
    profiles = {}

    for m in matches:
        p1, p2 = m["player1_id"], m["player2_id"]
        surf = m["surface"] or "Hard"
        profiles[m["match_id"]] = {
            p1: _profile_tennis(history_surf[(p1, surf)], history_all[p1], field_hold, field_ace_rate),
            p2: _profile_tennis(history_surf[(p2, surf)], history_all[p2], field_hold, field_ace_rate),
        }
        # p1_hold = 100 - p2_ret_pts_won_pct (server wins remaining service points)
        if m["p2_return_pts_won_pct"] is not None:
            entry1 = {"date": m["match_date"],
                      "hold": 100 - m["p2_return_pts_won_pct"],
                      "svpt": m["p1_svpt"], "aces": m["p1_aces"]}
            history_surf[(p1, surf)].append(entry1)
            history_all[p1].append(entry1)
        if m["p1_return_pts_won_pct"] is not None:
            entry2 = {"date": m["match_date"],
                      "hold": 100 - m["p1_return_pts_won_pct"],
                      "svpt": m["p2_svpt"], "aces": m["p2_aces"]}
            history_surf[(p2, surf)].append(entry2)
            history_all[p2].append(entry2)
    return profiles


def _profile_tennis(hist_surf, hist_all, field_hold, field_ace_rate):
    # Use surface-specific if ≥ 3 matches, else fall back to all-surface
    w = hist_surf[-FORM_WINDOW:] if len(hist_surf) >= 3 else hist_all[-FORM_WINDOW:]
    n = len(w)
    tier = player_tier(n)
    hold_vals = [h["hold"] for h in w if h["hold"] is not None]
    ace_vals  = [h["aces"] for h in w if h["aces"] is not None and h["svpt"]]
    svpt_vals = [h["svpt"] for h in w if h["aces"] is not None and h["svpt"]]
    obs_hold = mean(hold_vals) if hold_vals else None
    obs_ace  = sum(ace_vals)/sum(svpt_vals) if sum(svpt_vals or [0]) > 0 else None
    hold_shrunk = shrink(obs_hold, field_hold, tier)
    ace_shrunk  = shrink(obs_ace,  field_ace_rate, tier)
    return {"n": n, "tier": tier, "hold": hold_shrunk, "ace_rate": ace_shrunk,
            "surface_specific": len(hist_surf) >= 3}


# ── EDA rows ─────────────────────────────────────────────────────────────────

def build_eda_rows_darts(matches, profiles):
    rows = []
    for m in matches:
        pp = profiles.get(m["match_id"], {})
        p1p = pp.get(m["player1_id"]); p2p = pp.get(m["player2_id"])
        if not p1p or not p2p: continue
        if m["p1_legs_won"] is None or m["p2_legs_won"] is None: continue
        total_legs = m["p1_legs_won"] + m["p2_legs_won"]
        total_180s = ((m["p1_180s"] or 0) + (m["p2_180s"] or 0)
                      if m["p1_180s"] is not None and m["p2_180s"] is not None else None)
        if m["p1_avg"] is None or m["p2_avg"] is None: continue
        fmt_max = parse_format_max(m["format"])
        rnd_rank = ROUND_RANK.get(m["round"], 3)
        rows.append({
            "match_id":    m["match_id"],
            "format":      m["format"],
            "format_max":  fmt_max,
            "actual_units": total_legs,
            "actual_events": total_180s,
            # Skill gap
            "abs_gap":     abs(m["p1_avg"] - m["p2_avg"]),
            "avg_a":       m["p1_avg"],
            "avg_b":       m["p2_avg"],
            "avg_product": m["p1_avg"] * m["p2_avg"],
            "round_rank":  rnd_rank,
            # Rate model (v1 — for comparison)
            "pred_rate_x_units": (
                ((p1p["rate_180"] or 0) + (p2p["rate_180"] or 0)) * total_legs
                if p1p["rate_180"] and p2p["rate_180"] else None
            ),
            "p1_tier": p1p["tier"], "p2_tier": p2p["tier"],
        })
    return rows


def build_eda_rows_snooker(matches, profiles):
    rows = []
    for m in matches:
        pp = profiles.get(m["match_id"], {})
        p1p = pp.get(m["player1_id"]); p2p = pp.get(m["player2_id"])
        if not p1p or not p2p: continue
        if m["legs_sets_total"] is None: continue
        total_cents = ((m["p1_centuries"] or 0) + (m["p2_centuries"] or 0)
                       if m["p1_centuries"] is not None and m["p2_centuries"] is not None else None)
        fmt_max = parse_format_max(m["format"])
        rows.append({
            "match_id":     m["match_id"],
            "format":       m["format"],
            "format_max":   fmt_max,
            "actual_units": m["legs_sets_total"],
            "actual_events": total_cents,
            "abs_gap":      abs(p1p["fwr"] - p2p["fwr"]) if p1p["fwr"] and p2p["fwr"] else None,
            "pred_model":   (
                (p1p["cent_rate"] + p2p["cent_rate"]) * m["legs_sets_total"]
                if p1p["cent_rate"] and p2p["cent_rate"] else None
            ),
            "p1_tier": p1p["tier"], "p2_tier": p2p["tier"],
        })
    return rows


def build_eda_rows_tennis(matches, profiles):
    rows = []
    for m in matches:
        pp = profiles.get(m["match_id"], {})
        p1p = pp.get(m["player1_id"]); p2p = pp.get(m["player2_id"])
        if not p1p or not p2p: continue
        if m["p1_svpt"] is None or m["p2_svpt"] is None: continue
        total_svpt = m["p1_svpt"] + m["p2_svpt"]
        total_aces = ((m["p1_aces"] or 0) + (m["p2_aces"] or 0)
                      if m["p1_aces"] is not None and m["p2_aces"] is not None else None)
        pred = (
            p1p["ace_rate"] * m["p1_svpt"] + p2p["ace_rate"] * m["p2_svpt"]
            if p1p["ace_rate"] and p2p["ace_rate"] else None
        )
        rows.append({
            "match_id":     m["match_id"],
            "surface":      m["surface"] or "Hard",
            "format_max":   3,  # all SETS; assume BO3 baseline
            "actual_units": total_svpt,
            "actual_events": total_aces,
            "abs_hold_gap": abs(p1p["hold"] - p2p["hold"]) if p1p["hold"] and p2p["hold"] else None,
            "pred_model":   pred,
            "p1_tier": p1p["tier"], "p2_tier": p2p["tier"],
        })
    return rows


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse_claim1(rows, gap_key, unit_key, fmt_key, sport, buckets_def):
    full = [r for r in rows if r.get(gap_key) is not None and r.get(fmt_key, 1) > 0]
    print(f"\n  CLAIM 1: |skill_gap| predicts units played")
    print(f"  n_total={len(rows)}  n_with_gap={len(full)}")
    if len(full) < 20:
        print("  INSUFFICIENT DATA"); return None

    # Normalised regression
    xs  = [r[gap_key] for r in full]
    ys  = [r[unit_key] / r[fmt_key] for r in full]
    rho = spearman(xs, ys)
    r2  = ols_r2(xs, ys)

    # Segment table
    seg_key = "surface" if sport == "tennis" else fmt_key
    by_seg = defaultdict(list)
    for r in full:
        by_seg[r.get(seg_key, "?")].append(r)

    print(f"\n  {'Segment':<12} {'N':>5}  ", end="")
    for b in buckets_def: print(f"{b[0]:>10}", end="")
    print()
    print(f"  {'-'*12} {'-'*5}  " + "  ".join(["-"*9]*len(buckets_def)))

    signals = []
    for seg in sorted(by_seg, key=lambda s: len(by_seg[s]), reverse=True):
        seg_rows = by_seg[seg]
        if len(seg_rows) < 20: continue
        bucket_vals = defaultdict(list)
        for r in seg_rows:
            g = r[gap_key]
            for bname, blo, bhi in buckets_def:
                if blo <= g < bhi:
                    bucket_vals[bname].append(r[unit_key])
        row_out = f"  {str(seg):<12} {len(seg_rows):>5}  "
        first_mean, last_mean = None, None
        for i, (bname, blo, bhi) in enumerate(buckets_def):
            vals = bucket_vals.get(bname, [])
            m_val = mean(vals)
            row_out += f"{m_val:>7.1f}({len(vals):>3})  "
            if i == 0: first_mean = m_val
            last_mean = m_val
        print(row_out)
        if first_mean and last_mean and first_mean > 0:
            signals.append(first_mean > last_mean)

    print(f"\n  Spearman ρ (gap vs units/fmt_max): {rho:+.4f}")
    print(f"  R² (gap vs units/fmt_max):         {r2:.4f}")

    # Guide verdict
    if r2 >= 0.15 and rho < 0:   verdict = "PASS"
    elif r2 >= 0.01 and rho < 0: verdict = "WEAK PASS"
    elif rho >= 0:                verdict = "FAIL"
    else:                         verdict = "WEAK PASS"
    print(f"  Guide verdict: {verdict}  (R²≥0.15=PASS, R²<0.10=WEAK, flat/positive=FAIL)")
    return {"verdict": verdict, "r2": r2, "rho": rho}


def analyse_claim2_darts(rows):
    """Test direct regression vs rate×units vs naive."""
    # Filter rows with complete data
    reg_rows = [r for r in rows
                if r["actual_events"] is not None
                and r["avg_a"] is not None
                and r["format_max"] > 0]
    if len(reg_rows) < 20:
        print("  INSUFFICIENT DATA for Claim 2"); return None

    actual  = [r["actual_events"] for r in reg_rows]
    naive   = mean(actual)

    # Naive baseline
    mae_naive = mae([naive]*len(actual), actual)

    # Direct OLS regression: total_180s ~ avg_A + avg_B + format_max + round + avg_A*avg_B
    X = [[r["avg_a"], r["avg_b"], r["format_max"],
          r["round_rank"], r["avg_product"]] for r in reg_rows]
    coeffs, r2_reg, preds_reg = multivar_ols(X, actual)
    mae_reg = mae(preds_reg, actual)

    # Rate × units (v1) for comparison
    rate_rows = [r for r in reg_rows if r["pred_rate_x_units"] is not None]
    if rate_rows:
        mae_rate = mae([r["pred_rate_x_units"] for r in rate_rows],
                       [r["actual_events"] for r in rate_rows])
        naive_rate = mean([r["actual_events"] for r in rate_rows])
        mae_naive_rate = mae([naive_rate]*len(rate_rows),
                             [r["actual_events"] for r in rate_rows])
        rate_imp = (mae_naive_rate - mae_rate)/mae_naive_rate*100
    else:
        mae_rate = None; rate_imp = None

    reg_imp = (mae_naive - mae_reg)/mae_naive*100

    print(f"\n  CLAIM 2: Direct regression vs naive (n={len(reg_rows)})")
    print(f"  Features: avg_A + avg_B + format_max + round + avg_A×avg_B")
    if coeffs is not None:
        labels = ["intercept","avg_A","avg_B","format_max","round","avg_A×avg_B"]
        for l, c in zip(labels, coeffs): print(f"    {l:<15}: {c:+.4f}")
    print(f"  R² (regression):         {r2_reg:.4f}")
    print(f"  MAE naive:               {mae_naive:.3f}")
    print(f"  MAE direct regression:   {mae_reg:.3f}  ({reg_imp:+.1f}% vs naive)")
    if mae_rate:
        print(f"  MAE rate×units (v1):     {mae_rate:.3f}  ({rate_imp:+.1f}% vs naive)")

    # Segment breakdown
    print(f"\n  MAE by gap bucket (direct regression vs naive):")
    buckets = [("parity(0-3)",0,3),("mid(3-7)",3,7),("high(7-12)",7,12),("mismatch(12+)",12,999)]
    for bname, blo, bhi in buckets:
        brows = [r for r in reg_rows if blo <= r["abs_gap"] < bhi]
        if not brows: continue
        bact   = [r["actual_events"] for r in brows]
        bpred  = [preds_reg[reg_rows.index(r)] for r in brows]
        bnaive = mean(bact)
        bm     = mae(bpred, bact)
        bn     = mae([bnaive]*len(bact), bact)
        print(f"    {bname:<22} n={len(brows):>4}  reg={bm:.3f}  naive={bn:.3f}  {((bn-bm)/bn*100 if bn else 0):+.1f}%")

    # Guide verdict
    if reg_imp >= 10:   verdict = "PASS"
    elif reg_imp >= 3:  verdict = "WEAK PASS"
    else:               verdict = "FAIL"
    print(f"\n  Guide verdict: {verdict}  ({reg_imp:+.1f}% MAE improvement)")
    return {"verdict": verdict, "mae_improvement": reg_imp, "r2": r2_reg}


def analyse_claim2_generic(rows, pred_key, sport):
    pred_rows = [r for r in rows if r.get(pred_key) is not None and r["actual_events"] is not None]
    if len(pred_rows) < 20:
        print("  INSUFFICIENT DATA for Claim 2"); return None

    actual  = [r["actual_events"] for r in pred_rows]
    pred    = [r[pred_key] for r in pred_rows]
    naive   = mean(actual)
    mae_m   = mae(pred, actual)
    mae_n   = mae([naive]*len(actual), actual)
    imp     = (mae_n - mae_m)/mae_n*100

    print(f"\n  CLAIM 2: Model vs naive (n={len(pred_rows)})")
    print(f"  MAE naive: {mae_n:.3f}   MAE model: {mae_m:.3f}   improvement: {imp:+.1f}%")
    print(f"  Mean prediction error: {mean([p-a for p,a in zip(pred,actual)]):+.3f}")

    # Segment breakdown
    if sport == "snooker":
        buckets = [("parity(0-0.05)",0,0.05),("mid(0.05-0.15)",0.05,0.15),
                   ("high(0.15-0.25)",0.15,0.25),("mismatch(0.25+)",0.25,999)]
        gap_key = "abs_gap"
    else:
        buckets = [("parity(0-2)",0,2),("mid(2-5)",2,5),("high(5-9)",5,9),("mismatch(9+)",9,999)]
        gap_key = "abs_hold_gap"

    print(f"\n  MAE by gap bucket:")
    for bname, blo, bhi in buckets:
        brows = [r for r in pred_rows if r.get(gap_key) is not None and blo <= r[gap_key] < bhi]
        if not brows: continue
        bact = [r["actual_events"] for r in brows]
        bpred = [r[pred_key] for r in brows]
        bnaive = mean(bact)
        bm = mae(bpred, bact); bn = mae([bnaive]*len(bact), bact)
        print(f"    {bname:<22} n={len(brows):>4}  model={bm:.3f}  naive={bn:.3f}  {((bn-bm)/bn*100 if bn else 0):+.1f}%")

    if imp >= 10:  verdict = "PASS"
    elif imp >= 3: verdict = "WEAK PASS"
    else:          verdict = "FAIL"
    print(f"\n  Guide verdict: {verdict}  ({imp:+.1f}% MAE improvement)")
    return {"verdict": verdict, "mae_improvement": imp}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["darts","snooker","tennis"])
    parser.add_argument("--plot",  action="store_true")
    args = parser.parse_args()
    sports = [args.sport] if args.sport else ["darts","snooker","tennis"]

    conn    = get_conn()
    summary = {}

    # ── DARTS ──────────────────────────────────────────────────────────────
    if "darts" in sports:
        print("\n" + "="*60)
        print("  EDA v2 — DARTS")
        print("="*60)
        matches  = load_matches(conn, "darts")
        profiles = build_profiles_darts(matches)
        rows     = build_eda_rows_darts(matches, profiles)
        total    = len(rows)
        tier_counts = defaultdict(int)
        for r in rows:
            tier_counts[f"p1_{r['p1_tier']}"] += 1
        print(f"  Total rows: {total}")
        print(f"  Tier breakdown (p1): " +
              "  ".join(f"{k}={v}" for k,v in sorted(tier_counts.items())))

        buckets = [("0–3(par)",0,3),("3–7",3,7),("7–12",7,12),("12+(mis)",12,999)]
        c1 = analyse_claim1(rows, "abs_gap", "actual_units", "format_max", "darts", buckets)

        c2 = analyse_claim2_darts(rows)
        summary["darts"] = {"c1": c1, "c2": c2, "n": total}

    # ── SNOOKER ────────────────────────────────────────────────────────────
    if "snooker" in sports:
        print("\n" + "="*60)
        print("  EDA v2 — SNOOKER  (with shrinkage — exclusion replaced)")
        print("="*60)
        matches = load_matches(conn, "snooker")

        # Compute field averages for shrinkage
        fwr_vals  = [r["p1_frames_won"]/r["legs_sets_total"]
                     for r in matches if r["p1_frames_won"] is not None and r["legs_sets_total"]]
        cent_vals = [r["p1_centuries"]/r["legs_sets_total"]
                     for r in matches if r["p1_centuries"] is not None and r["legs_sets_total"]]
        field_fwr  = mean(fwr_vals)
        field_cent = mean(cent_vals)
        print(f"  Field averages: fwr={field_fwr:.3f}  cent_rate={field_cent:.4f}")

        profiles = build_profiles_snooker(matches, field_fwr, field_cent)
        rows     = build_eda_rows_snooker(matches, profiles)
        print(f"  Total rows: {len(rows)}  (previously: 1548 after exclusion)")

        buckets = [("0–0.05(p)",0,0.05),("0.05–0.15",0.05,0.15),
                   ("0.15–0.25",0.15,0.25),("0.25+(m)",0.25,999)]
        c1 = analyse_claim1(rows, "abs_gap", "actual_units", "format_max", "snooker", buckets)
        c2 = analyse_claim2_generic(rows, "pred_model", "snooker")
        summary["snooker"] = {"c1": c1, "c2": c2, "n": len(rows)}

    # ── TENNIS ─────────────────────────────────────────────────────────────
    if "tennis" in sports:
        print("\n" + "="*60)
        print("  EDA v2 — TENNIS  (hold% as Claim 1 metric)")
        print("="*60)
        matches = load_matches(conn, "tennis")

        hold_vals = [100 - r["p2_return_pts_won_pct"]
                     for r in matches if r["p2_return_pts_won_pct"] is not None]
        ace_vals  = [r["p1_aces"]/r["p1_svpt"]
                     for r in matches if r["p1_aces"] is not None and r["p1_svpt"]]
        field_hold = mean(hold_vals)
        field_ace  = mean(ace_vals)
        print(f"  Field averages: hold={field_hold:.2f}%  ace_rate={field_ace:.4f}")

        profiles = build_profiles_tennis(matches, field_hold, field_ace)
        rows     = build_eda_rows_tennis(matches, profiles)
        print(f"  Total rows: {len(rows)}")

        # Surface segmentation
        buckets = [("0–2(par)",0,2),("2–5",2,5),("5–9",5,9),("9+(mis)",9,999)]
        c1 = analyse_claim1(rows, "abs_hold_gap", "actual_units", "format_max",
                            "tennis", buckets)
        c2 = analyse_claim2_generic(rows, "pred_model", "tennis")
        summary["tennis"] = {"c1": c1, "c2": c2, "n": len(rows)}

    conn.close()

    # ── SUMMARY ────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  SUMMARY — EDA v2")
    print("="*60)
    icons = {"PASS":"✓","WEAK PASS":"~","FAIL":"✗","BLOCKED":"?"}
    for sport, r in summary.items():
        c1v = r["c1"]["verdict"] if r["c1"] else "—"
        c2v = r["c2"]["verdict"] if r["c2"] else "—"
        c1r = r["c1"]["r2"] if r["c1"] else 0
        c2i = r["c2"]["mae_improvement"] if r["c2"] else 0
        print(f"  {sport:<10}  "
              f"Claim1:[{icons.get(c1v,'?')}]{c1v:<10} R²={c1r:.4f}  |  "
              f"Claim2:[{icons.get(c2v,'?')}]{c2v:<10} MAE_imp={c2i:+.1f}%  "
              f"n={r['n']}")

    print()
    print("  Guide decision tree:")
    for sport, r in summary.items():
        c1v = r["c1"]["verdict"] if r["c1"] else "BLOCKED"
        c2v = r["c2"]["verdict"] if r["c2"] else "BLOCKED"
        if c1v == "PASS" and c2v == "PASS":
            action = "→ Proceed to Claim 3"
        elif c1v in ("PASS","WEAK PASS") and c2v in ("PASS","WEAK PASS"):
            action = "→ Proceed with 8%+ edge threshold"
        elif c2v == "FAIL":
            action = "→ BLOCKED — fix model architecture"
        else:
            action = "→ Investigate"
        print(f"  {sport:<10}  C1:{c1v}  C2:{c2v}  {action}")


if __name__ == "__main__":
    main()
