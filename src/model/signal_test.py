"""
signal_test.py — Does the signal exist in the data?

Reads form data from player_form table (populated by form_builder.py).
No inline rolling computation — all metrics come from the DB.

Test: does actual fall below the NAIVE fair line in MISMATCH matches?
      does actual exceed the NAIVE fair line in PARITY matches?

Naive fair line = NegBin(population_mean) median — what a bookmaker using
blended season averages would set as their 50/50 line.

Usage:
    PYTHONUTF8=1 python -m src.model.signal_test
    PYTHONUTF8=1 python -m src.model.signal_test --sport darts
    PYTHONUTF8=1 python -m src.model.signal_test --stage   # break by round
"""

import sys
import argparse
import numpy as np
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from database import get_conn

RNG   = np.random.default_rng(42)
ALPHA = {"darts": 0.2819, "snooker": 0.4468, "tennis": 0.5623}
N_SIM = 10_000

ROUND_RANK = {
    "1/64-finals":1,"1/32-finals":2,"1/16-finals":3,"1/8-finals":4,
    "Quarter-finals":5,"Semi-finals":6,"Final":7,
    "LAST_128":2,"LAST_96":3,"LAST_80":4,"R64":5,"R32":6,"R16":7,
    "QF":8,"SF":9,"F":10,"R128":1,
}
ROUND_LABEL = {
    1:"R1",2:"R2",3:"R3",4:"R4",5:"QF",6:"SF",7:"F",8:"QF",9:"SF",10:"F"
}

MISMATCH_T = {"darts":7.0,  "snooker":0.15, "tennis":9.0}  # tennis raised 5→9 (signal lives at 9+)
PARITY_T   = {"darts":3.0,  "snooker":0.05, "tennis":2.0}
LATE       = {5,6,7,8,9,10}
LONG_FMT   = {"darts":13,   "snooker":17,   "tennis":3}   # tennis BO3 baseline

TENNIS_LEAGUE_AVG_RET = 33.0  # ATP avg: returner wins ~33% of service points


# ── NegBin fair line ──────────────────────────────────────────────────────────

def fair_line(mu: float, sport: str) -> float:
    a = ALPHA[sport]
    n, p = 1/a, 1/(1 + a*mu)
    return float(np.median(RNG.negative_binomial(n, p, N_SIM)))


def mean(xs): return sum(xs)/len(xs) if xs else 0.0

def classify(gap, sport, rk, fmt_max):
    if gap >= MISMATCH_T[sport]:                                    return "MISMATCH"
    if gap <= PARITY_T[sport] and rk in LATE and fmt_max >= LONG_FMT[sport]:
                                                                    return "PARITY"
    return "NEUTRAL"


# ── Load from player_form ─────────────────────────────────────────────────────

def _parse_format_max(fmt, sport=None):
    import re
    if sport == "tennis" or (fmt and "SETS" in fmt.upper()):
        return 3   # tennis BO3 baseline — allows parity to fire
    m = re.search(r'(\d+)', fmt or '')
    return int(m.group(1)) if m else 0


def load_darts(conn) -> list[dict]:
    """
    Darts: gap uses actual within-match avg (m.p1_avg) — more precise classifier.
    Rolling form avg is too smooth for gap classification.
    Rate (mu) uses rolling form rates from player_form.
    """
    rows = conn.execute("""
        SELECT m.match_id, m.match_date, m.round, m.format,
               m.total_180s AS actual,
               m.p1_avg, m.p2_avg,
               pf1.avg_180_rate_per_leg AS rate1,
               pf2.avg_180_rate_per_leg AS rate2,
               pf1.player_tier  AS tier1,
               pf2.player_tier  AS tier2,
               m.p1_legs_won + m.p2_legs_won AS total_legs
        FROM matches m
        JOIN player_form pf1
            ON pf1.player_id = m.player1_id
           AND pf1.sport = 'darts'
           AND pf1.as_of_date = m.match_date
        JOIN player_form pf2
            ON pf2.player_id = m.player2_id
           AND pf2.sport = 'darts'
           AND pf2.as_of_date = m.match_date
        WHERE m.sport = 'darts'
          AND m.total_180s IS NOT NULL
          AND m.p1_avg IS NOT NULL
          AND m.p2_avg IS NOT NULL
    """).fetchall()

    out = []
    for r in rows:
        (mid, date, rnd, fmt, actual, avg1, avg2,
         rate1, rate2, tier1, tier2, legs) = r
        rk      = ROUND_RANK.get(rnd, 3)
        fmt_max = _parse_format_max(fmt)
        gap     = abs(avg1 - avg2)   # actual match avg gap — best classifier
        mu = ((rate1 or 0) + (rate2 or 0)) * (legs or fmt_max * 0.8) if legs else None
        out.append({
            "actual": actual, "gap": gap, "rk": rk, "fmt_max": fmt_max,
            "mu": mu, "tier1": tier1, "tier2": tier2, "round": rnd,
        })
    return out


def load_snooker(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT m.match_id, m.match_date, m.round, m.format,
               m.total_centuries AS actual,
               m.legs_sets_total AS frames,
               pf1.frame_win_rate    AS fwr1,
               pf2.frame_win_rate    AS fwr2,
               pf1.avg_century_rate_per_frame AS cr1,
               pf2.avg_century_rate_per_frame AS cr2,
               pf1.player_tier AS tier1, pf2.player_tier AS tier2
        FROM matches m
        JOIN player_form pf1
            ON pf1.player_id = m.player1_id
           AND pf1.sport = 'snooker'
           AND pf1.as_of_date = m.match_date
        JOIN player_form pf2
            ON pf2.player_id = m.player2_id
           AND pf2.sport = 'snooker'
           AND pf2.as_of_date = m.match_date
        WHERE m.sport = 'snooker'
          AND m.total_centuries IS NOT NULL
          AND pf1.frame_win_rate IS NOT NULL
          AND pf2.frame_win_rate IS NOT NULL
    """).fetchall()

    out = []
    for r in rows:
        (mid, date, rnd, fmt, actual, frames,
         fwr1, fwr2, cr1, cr2, tier1, tier2) = r
        rk      = ROUND_RANK.get(rnd, 3)
        fmt_max = _parse_format_max(fmt)
        gap     = abs(fwr1 - fwr2)
        mu      = ((cr1 or 0) + (cr2 or 0)) * (frames or fmt_max * 0.8) if (cr1 and cr2) else None
        out.append({
            "actual": actual, "gap": gap, "rk": rk, "fmt_max": fmt_max,
            "mu": mu, "tier1": tier1, "tier2": tier2, "round": rnd,
        })
    return out


def load_tennis(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT m.match_id, m.match_date, m.round, m.format,
               m.total_aces AS actual,
               m.p1_svpt, m.p2_svpt,
               t.surface,
               pf1.avg_serve_strength AS ss1,
               pf2.avg_serve_strength AS ss2,
               pf1.avg_ace_rate_per_svpt AS ar1,
               pf2.avg_ace_rate_per_svpt AS ar2,
               pf1.avg_ace_rate_grass, pf1.avg_ace_rate_hard, pf1.avg_ace_rate_clay,
               pf2.avg_ace_rate_grass, pf2.avg_ace_rate_hard, pf2.avg_ace_rate_clay,
               pf1.player_tier AS tier1, pf2.player_tier AS tier2
        FROM matches m
        JOIN player_form pf1
            ON pf1.player_id = m.player1_id
           AND pf1.sport = 'tennis'
           AND pf1.as_of_date = m.match_date
        JOIN player_form pf2
            ON pf2.player_id = m.player2_id
           AND pf2.sport = 'tennis'
           AND pf2.as_of_date = m.match_date
        LEFT JOIN tournaments t ON t.tournament_id = m.tournament_id
        WHERE m.sport = 'tennis'
          AND m.total_aces IS NOT NULL
          AND m.p1_svpt IS NOT NULL
          AND pf1.avg_serve_strength IS NOT NULL
          AND pf2.avg_serve_strength IS NOT NULL
    """).fetchall()

    surface_col = {"Grass":0, "Hard":1, "Clay":2}

    out = []
    for r in rows:
        (mid, date, rnd, fmt, actual, svpt1, svpt2, surface,
         ss1, ss2, ar1_all, ar2_all,
         ar1_g, ar1_h, ar1_c, ar2_g, ar2_h, ar2_c,
         tier1, tier2) = r
        rk      = ROUND_RANK.get(rnd, 3)
        fmt_max = _parse_format_max(fmt, sport="tennis")   # SETS → 3
        gap     = abs(ss1 - ss2)

        # Surface-specific ace rate
        surf = surface or "Hard"
        ar1 = {"Grass":ar1_g,"Hard":ar1_h,"Clay":ar1_c}.get(surf, ar1_all) or ar1_all
        ar2 = {"Grass":ar2_g,"Hard":ar2_h,"Clay":ar2_c}.get(surf, ar2_all) or ar2_all

        # Return quality modifier: adjust each player's ace rate for opponent's return strength
        # If opponent wins more return pts than league avg → they suppress aces
        # ret_pct_p2 = how often p2 wins points when p1 is serving
        # We approximate: p2_ret_pct ≈ 100 - ss1 (ss1 = p1 serve strength = 100 - p2_ret%)
        p2_ret_pct = (100 - ss1) if ss1 else TENNIS_LEAGUE_AVG_RET
        p1_ret_pct = (100 - ss2) if ss2 else TENNIS_LEAGUE_AVG_RET
        ar1_adj = (ar1 or 0) * (TENNIS_LEAGUE_AVG_RET / max(p2_ret_pct, 10))
        ar2_adj = (ar2 or 0) * (TENNIS_LEAGUE_AVG_RET / max(p1_ret_pct, 10))

        # Asymmetric service games: weak server (lower ss) loses service games in mismatch
        # Both players' svpt are actuals — compression already embedded in the data
        mu = (ar1_adj * (svpt1 or 0) + ar2_adj * (svpt2 or 0)) if (ar1 and ar2) else None

        out.append({
            "actual": actual, "gap": gap, "rk": rk, "fmt_max": fmt_max,
            "mu": mu, "tier1": tier1, "tier2": tier2, "round": rnd,
            "surface": surf,
        })
    return out


# ── Analysis ──────────────────────────────────────────────────────────────────

def run(rows, sport, pop_mean):
    fl = fair_line(pop_mean, sport)
    by_signal = defaultdict(list)

    for r in rows:
        if r["actual"] is None or r["gap"] is None: continue
        sig = classify(r["gap"], sport, r["rk"], r["fmt_max"])
        by_signal[sig].append({
            **r,
            "fl":         fl,
            "hit_under":  int(r["actual"] < fl),
            "hit_over":   int(r["actual"] > fl),
        })
    return by_signal, fl


def report(sport, by_signal, pop_mean, fl, show_stage=False):
    unit = {"darts":"180s","snooker":"centuries","tennis":"aces"}[sport]
    print(f"\n{'='*62}")
    print(f"  {sport.upper()}  |  pop_mean={pop_mean:.2f}  naive_fair_line={fl:.1f}")
    print(f"{'='*62}")

    total = sum(len(v) for v in by_signal.values())
    print(f"  Matches from player_form: {total:,}")

    print(f"\n  {'Signal':<10} {'N':>5}  {'Actual':>8}  {'Fair':>6}  "
          f"{'Under%':>7}  {'Over%':>6}  {'Gap':>7}")
    print(f"  {'-'*10} {'-'*5}  {'-'*8}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*7}")

    results = {}
    for sig in ["MISMATCH","NEUTRAL","PARITY"]:
        rows = by_signal.get(sig, [])
        if not rows:
            print(f"  {sig:<10} {'—':>5}")
            continue
        n   = len(rows)
        ma  = mean([r["actual"]    for r in rows])
        up  = mean([r["hit_under"] for r in rows]) * 100
        op  = mean([r["hit_over"]  for r in rows]) * 100
        gap = fl - ma
        print(f"  {sig:<10} {n:>5}  {ma:>8.2f}  {fl:>6.1f}  "
              f"{up:>6.1f}%  {op:>5.1f}%  {gap:>+7.2f}")
        results[sig] = {"n":n, "under_pct":up, "over_pct":op, "mean_actual":ma, "gap":gap}

    # Gap-bucket breakdown within MISMATCH
    mis_rows = by_signal.get("MISMATCH", [])
    if mis_rows:
        if sport == "darts":
            buckets = [("7-12",7,12),("12-17",12,17),("17+",17,999)]
        elif sport == "snooker":
            buckets = [("0.15-0.25",0.15,0.25),("0.25-0.35",0.25,0.35),("0.35+",0.35,999)]
        else:
            buckets = [("5-9",5,9),("9-13",9,13),("13+",13,999)]
        print(f"\n  MISMATCH by gap size:")
        for bname, blo, bhi in buckets:
            b = [r for r in mis_rows if blo <= r["gap"] < bhi]
            if len(b) < 5: continue
            up = mean([r["hit_under"] for r in b])*100
            ma = mean([r["actual"] for r in b])
            print(f"    gap {bname:<10} n={len(b):>3}  actual={ma:.2f}  under={up:.1f}%")

    # Stage breakdown
    if show_stage:
        print(f"\n  By round (UNDER% / OVER% vs naive line):")
        by_rnd = defaultdict(list)
        for sig_rows in by_signal.values():
            for r in sig_rows:
                by_rnd[r["rk"]].append(r)
        print(f"  {'Round':<8} {'N':>4}  {'Actual':>8}  {'Under%':>8}  {'Over%':>7}  {'Signal':>10}")
        for rk in sorted(by_rnd):
            rrows = by_rnd[rk]
            if len(rrows) < 5: continue
            ma = mean([r["actual"]    for r in rrows])
            up = mean([r["hit_under"] for r in rrows])*100
            op = mean([r["hit_over"]  for r in rrows])*100
            mis = sum(1 for r in rrows if r.get("gap",0) >= MISMATCH_T[sport])
            par = sum(1 for r in rrows if r.get("gap",0) <= PARITY_T[sport])
            print(f"  {ROUND_LABEL.get(rk,str(rk)):<8} {len(rrows):>4}  {ma:>8.2f}  "
                  f"{up:>7.1f}%  {op:>6.1f}%  mis={mis} par={par}")

    # Verdicts
    mis = results.get("MISMATCH", {})
    par = results.get("PARITY",   {})
    print()
    u = mis.get("under_pct", 50)
    mv = ("✓ PASS" if u > 55 else "~ WEAK" if u > 50 else "✗ FAIL")
    pv = ("✓ PASS" if par.get("over_pct",0) > 55
          else "~ WEAK" if par.get("over_pct",0) > 50 else "✗ FAIL / no data")
    print(f"  MISMATCH UNDER: {mv}  ({u:.1f}%)")
    print(f"  PARITY   OVER:  {pv}  ({par.get('over_pct',0):.1f}%)")
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport",  choices=["darts","snooker","tennis"])
    parser.add_argument("--stage",  action="store_true", help="Show per-round breakdown")
    args = parser.parse_args()
    sports = [args.sport] if args.sport else ["darts","snooker","tennis"]

    conn    = get_conn()
    summary = {}

    STAT_COL = {"darts":"total_180s","snooker":"total_centuries","tennis":"total_aces"}

    for sport in sports:
        pop_vals = [r[0] for r in conn.execute(
            f"SELECT {STAT_COL[sport]} FROM matches WHERE sport=? AND {STAT_COL[sport]} IS NOT NULL",
            (sport,)).fetchall()]
        pop_mean = sum(pop_vals)/len(pop_vals) if pop_vals else 5.0

        if sport == "darts":       rows = load_darts(conn)
        elif sport == "snooker":   rows = load_snooker(conn)
        else:                      rows = load_tennis(conn)

        by_signal, fl = run(rows, sport, pop_mean)
        results = report(sport, by_signal, pop_mean, fl, args.stage)
        summary[sport] = results

    conn.close()

    print(f"\n{'='*62}")
    print(f"  SUMMARY — signal vs naive fair line")
    print(f"{'='*62}")
    for sport, r in summary.items():
        mis = r.get("MISMATCH", {})
        par = r.get("PARITY",   {})
        u   = mis.get("under_pct", 50)
        o   = par.get("over_pct",  50)
        n_m = mis.get("n", 0)
        n_p = par.get("n", 0)
        ui  = "✓" if u>55 else "~" if u>50 else "✗"
        oi  = "✓" if o>55 else "~" if o>50 else "✗"
        print(f"  {sport:<10}  UNDER [{ui}]{u:.1f}% (n={n_m})  "
              f"OVER [{oi}]{o:.1f}% (n={n_p})")

    print()
    print("  Naive fair line = NegBin(pop_mean) median.")
    print("  All form data from player_form (form_builder output).")
    print("  Real market lines needed for Claim 3 — collect via Sportmarket from ~Mar 15.")


if __name__ == "__main__":
    main()
