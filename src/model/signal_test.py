"""
signal_test.py — Thesis validation: does skill mismatch compress stat counts?

Thesis: In a mismatched match (early round, big skill gap) the dominant player
controls the game, reducing raw skill-expression opportunities for both.
→ Early rounds should show FEWER 180s / centuries / aces per unit of play.

If this signal is not present in the data, don't build the model.

Usage:
    cd sports-betting
    PYTHONUTF8=1 python -m src.model.signal_test
    PYTHONUTF8=1 python -m src.model.signal_test --sport darts
    PYTHONUTF8=1 python -m src.model.signal_test --plot
"""

import argparse
import sys
from pathlib import Path
import sqlite3
import re
from dataclasses import dataclass
from typing import Optional
import statistics

# ── Path setup ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from database import get_conn


# ── Round ordering ─────────────────────────────────────────────────────────
# Lower rank = earlier = bigger expected skill gap between players.
# This is our mismatch proxy (no ranking data available).

ROUND_RANK = {
    # Darts
    "1/64-finals": 1,
    "1/32-finals": 2,
    "1/16-finals": 3,
    "1/8-finals":  4,
    "Quarter-finals": 5,
    "Semi-finals": 6,
    "Final": 7,

    # Snooker (WST rounds vary by tournament size)
    "PRE-QUALIFYING_1": 1,
    "PRE-QUALIFYING_2": 1,
    "LAST_144": 2,
    "LAST_128": 2,
    "LAST_112": 3,
    "LAST_96":  3,
    "LAST_80":  4,
    "LAST_48":  4,
    "LAST_12":  4,
    "R64": 5,
    "R32": 6,
    "R16": 7,
    "QF":  8,
    "SF":  9,
    "F":   10,

    # Tennis
    "R128": 1,
    "R64":  2,
    "R32":  3,
    "R16":  4,
    "QF":   5,
    "SF":   6,
    "F":    7,
    "RR":   None,   # round robin — skip, mixed field
    "BR":   None,   # bronze match — skip
}

# Readable labels for output
ROUND_LABEL = {
    1: "Very early (R128/1/64/Prequalify)",
    2: "Early (R64/1/32/Last128)",
    3: "Mid-early (R32/1/16)",
    4: "Mid (R16/1/8/Last80)",
    5: "Mid-late (QF/R32-snooker)",
    6: "Late (SF/R16-snooker)",
    7: "Very late (F/QF-snooker)",
    8: "QF-snooker",
    9: "SF-snooker",
    10: "Final-snooker",
}


def parse_format_max_legs(fmt: str) -> Optional[int]:
    """Extract max legs/frames from format string. BO11 → 11, BO13 → 13."""
    if not fmt:
        return None
    m = re.search(r"(\d+)", fmt)
    return int(m.group(1)) if m else None


# ── Data loading ────────────────────────────────────────────────────────────

@dataclass
class MatchRow:
    match_id: str
    sport: str
    round: str
    round_rank: Optional[int]
    format: str
    format_max: Optional[int]
    legs_sets_total: Optional[int]
    total_stat: Optional[int]       # 180s / centuries / aces
    stat_rate: Optional[float]      # stat / denominator (if available)


def load_matches(conn: sqlite3.Connection, sport: str) -> list[MatchRow]:
    """Load matches for a sport, computing round_rank and stat_rate."""
    if sport == "darts":
        sql = """
            SELECT match_id, sport, round, format,
                   legs_sets_total, total_180s AS total_stat
            FROM matches
            WHERE sport = 'darts' AND total_180s IS NOT NULL
        """
    elif sport == "snooker":
        sql = """
            SELECT match_id, sport, round, format,
                   legs_sets_total, total_centuries AS total_stat
            FROM matches
            WHERE sport = 'snooker' AND total_centuries IS NOT NULL
        """
    elif sport == "tennis":
        sql = """
            SELECT match_id, sport, round, format,
                   legs_sets_total, total_aces AS total_stat
            FROM matches
            WHERE sport = 'tennis' AND total_aces IS NOT NULL
        """
    else:
        raise ValueError(f"Unknown sport: {sport}")

    rows = []
    for r in conn.execute(sql).fetchall():
        rnd = r[2]
        rrank = ROUND_RANK.get(rnd)
        fmt = r[3]
        fmt_max = parse_format_max_legs(fmt)
        legs = r[4]
        total = r[5]

        # Compute rate where we have a reliable denominator
        rate = None
        if total is not None:
            if sport == "snooker" and legs and legs > 0:
                rate = total / legs          # centuries per frame ✓
            elif sport == "darts" and fmt_max and fmt_max > 0:
                rate = total / fmt_max       # 180s per max-leg (proxy, not perfect)
            elif sport == "tennis":
                rate = float(total)          # raw count — no denominator available

        rows.append(MatchRow(
            match_id=r[0],
            sport=sport,
            round=rnd,
            round_rank=rrank,
            format=fmt,
            format_max=fmt_max,
            legs_sets_total=legs,
            total_stat=total,
            stat_rate=rate,
        ))
    return rows


# ── Statistics helpers ──────────────────────────────────────────────────────

def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0

def median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0

def stdev(xs: list[float]) -> float:
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def spearman_correlation(xs: list[float], ys: list[float]) -> float:
    """Compute Spearman rank correlation without scipy."""
    n = len(xs)
    if n < 3:
        return 0.0

    def rank(lst):
        sorted_idx = sorted(range(n), key=lambda i: lst[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and lst[sorted_idx[j]] == lst[sorted_idx[j + 1]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[sorted_idx[k]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n ** 2 - 1))


def mann_whitney_u(group1: list[float], group2: list[float]) -> tuple[float, str]:
    """
    Compute Mann-Whitney U statistic.
    Returns (U, direction) where direction is 'group1_lower' or 'group2_lower'.
    No p-value — just the effect size as rank-biserial correlation.
    """
    n1, n2 = len(group1), len(group2)
    if n1 == 0 or n2 == 0:
        return 0.0, "insufficient_data"

    # u1 = count of (early < late) comparisons → large u1 = early tends lower
    u1 = sum(
        1.0 if a < b else 0.5 if a == b else 0.0
        for a in group1
        for b in group2
    )
    u2 = n1 * n2 - u1

    # Rank-biserial correlation: +1 = group1 always lower, -1 = group1 always higher
    r = (u1 - u2) / (n1 * n2)
    direction = "early_lower" if r > 0 else "late_lower"
    return r, direction


# ── Analysis per sport ──────────────────────────────────────────────────────

def analyse_sport(rows: list[MatchRow], sport: str, use_rate: bool) -> dict:
    """
    Run the signal test for one sport.
    Returns a result dict with stats by round group and correlation.
    """
    # Filter to rows with valid round_rank and the chosen metric
    valid = [r for r in rows if r.round_rank is not None]
    if use_rate:
        valid = [r for r in valid if r.stat_rate is not None]
        values = [r.stat_rate for r in valid]
    else:
        valid = [r for r in valid if r.total_stat is not None]
        values = [float(r.total_stat) for r in valid]

    ranks = [float(r.round_rank) for r in valid]

    if len(valid) < 10:
        return {"error": f"Insufficient data: {len(valid)} rows"}

    # Group by round_rank
    by_rank: dict[int, list[float]] = {}
    for row, val in zip(valid, values):
        by_rank.setdefault(row.round_rank, []).append(val)

    # Sort groups by rank
    sorted_ranks = sorted(by_rank.keys())

    # Build summary table
    summary = []
    for rk in sorted_ranks:
        vals = by_rank[rk]
        summary.append({
            "rank": rk,
            "label": ROUND_LABEL.get(rk, f"Rank {rk}"),
            "n": len(vals),
            "mean": mean(vals),
            "median": median(vals),
            "stdev": stdev(vals),
        })

    # Spearman correlation: round_rank vs stat value
    rho = spearman_correlation(ranks, values)

    # Mann-Whitney: early (rank <= median_rank) vs late
    median_rank = sorted(ranks)[len(ranks) // 2]
    early_vals = [v for row, v in zip(valid, values) if row.round_rank <= median_rank]
    late_vals  = [v for row, v in zip(valid, values) if row.round_rank >  median_rank]
    mw_r, mw_dir = mann_whitney_u(early_vals, late_vals)

    # Thesis verdict
    # Thesis predicts: early rounds → LOWER counts (rho > 0 means higher rank = more counts)
    # rho > 0 → later rounds have more → consistent with thesis
    # mw_dir == 'early_lower' → early has fewer → consistent
    thesis_signals = 0
    if rho > 0.05:
        thesis_signals += 1
    if mw_dir == "early_lower":
        thesis_signals += 1

    verdict = {
        0: "FAIL — no signal",
        1: "WEAK — partial signal",
        2: "PASS — signal confirmed",
    }[thesis_signals]

    return {
        "sport": sport,
        "n_total": len(rows),
        "n_analysed": len(valid),
        "metric": "rate" if use_rate else "raw_count",
        "spearman_rho": rho,
        "mw_effect_r": mw_r,
        "mw_direction": mw_dir,
        "early_n": len(early_vals),
        "early_mean": mean(early_vals),
        "late_n": len(late_vals),
        "late_mean": mean(late_vals),
        "thesis_verdict": verdict,
        "summary_table": summary,
    }


# ── Output ──────────────────────────────────────────────────────────────────

SPORT_STAT_LABEL = {
    "darts":   "180s (raw) / 180s-per-max-leg (rate)",
    "snooker": "centuries (raw) / centuries-per-frame (rate)",
    "tennis":  "aces (raw)",
}

def print_results(result: dict) -> None:
    sport = result["sport"]
    print(f"\n{'='*60}")
    print(f"  SIGNAL TEST — {sport.upper()}")
    print(f"{'='*60}")

    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return

    print(f"  Matches: {result['n_analysed']:,} analysed / {result['n_total']:,} total")
    print(f"  Metric:  {result['metric']}")
    print()
    print(f"  {'Round':<36} {'N':>5} {'Mean':>8} {'Median':>8} {'StDev':>8}")
    print(f"  {'-'*36} {'-'*5} {'-'*8} {'-'*8} {'-'*8}")
    for row in result["summary_table"]:
        print(f"  {row['label']:<36} {row['n']:>5} {row['mean']:>8.3f} {row['median']:>8.3f} {row['stdev']:>8.3f}")

    print()
    print(f"  Spearman ρ (round_rank vs stat):  {result['spearman_rho']:+.4f}")
    print(f"    → positive = later rounds have more {sport} stat")
    print(f"    → negative = earlier rounds have more (counter-thesis)")
    print()
    print(f"  Mann-Whitney effect r:  {result['mw_effect_r']:+.4f}  ({result['mw_direction']})")
    print(f"    Early mean: {result['early_mean']:.3f}  (n={result['early_n']})")
    print(f"    Late mean:  {result['late_mean']:.3f}  (n={result['late_n']})")
    print()

    verdict = result["thesis_verdict"]
    icon = {"PASS": "✓", "WEAK": "~", "FAIL": "✗"}.get(verdict.split()[0], "?")
    print(f"  THESIS VERDICT: [{icon}] {verdict}")
    print()


def plot_results(results: list[dict]) -> None:
    """Optional matplotlib visualisation."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        print("[plot] matplotlib not installed — skipping plot")
        return

    fig = plt.figure(figsize=(15, 5))
    gs = gridspec.GridSpec(1, 3, figure=fig)

    for idx, result in enumerate(results):
        if "error" in result:
            continue
        ax = fig.add_subplot(gs[idx])
        sport = result["sport"]
        table = result["summary_table"]

        x = [r["rank"] for r in table]
        y_mean = [r["mean"] for r in table]
        y_med = [r["median"] for r in table]

        ax.bar(x, y_mean, alpha=0.6, label="Mean")
        ax.plot(x, y_med, "o--", color="red", label="Median", linewidth=2)
        ax.set_title(f"{sport.title()} — stat by round\nρ={result['spearman_rho']:+.3f}  {result['thesis_verdict'].split('—')[0]}")
        ax.set_xlabel("Round rank (1=earliest)")
        ax.set_ylabel(f"Stat ({result['metric']})")
        ax.legend(fontsize=8)

    plt.tight_layout()
    out = ROOT / "signal_test_output.png"
    plt.savefig(out, dpi=120)
    print(f"\n[plot] Saved: {out}")
    plt.show()


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Signal test — mismatch vs count compression")
    parser.add_argument("--sport", choices=["darts", "snooker", "tennis"], help="Run one sport only")
    parser.add_argument("--rate", action="store_true", help="Use normalised rate instead of raw count")
    parser.add_argument("--plot", action="store_true", help="Generate matplotlib chart")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else ["darts", "snooker", "tennis"]

    conn = get_conn()
    results = []

    for sport in sports:
        rows = load_matches(conn, sport)
        print(f"[{sport}] Loaded {len(rows)} rows with stat data")

        # For snooker use rate (has legs_sets_total), others use raw unless --rate forced
        use_rate = args.rate or sport == "snooker"
        result = analyse_sport(rows, sport, use_rate=use_rate)
        results.append(result)
        print_results(result)

    conn.close()

    # Final summary
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in results:
        if "error" in r:
            print(f"  {r['sport']:10} — ERROR")
        else:
            print(f"  {r['sport']:10} — {r['thesis_verdict']}")

    passing = [r for r in results if "error" not in r and r["thesis_verdict"].startswith("PASS")]
    weak    = [r for r in results if "error" not in r and r["thesis_verdict"].startswith("WEAK")]
    failing = [r for r in results if "error" not in r and r["thesis_verdict"].startswith("FAIL")]

    print()
    if failing:
        print(f"  !! {len(failing)} sport(s) FAILED — review before building model")
    if weak:
        print(f"  ~~ {len(weak)} sport(s) show weak signal — proceed with caution")
    if passing:
        print(f"  ✓  {len(passing)} sport(s) CONFIRMED — proceed to form_builder.py")

    if args.plot:
        plot_results(results)


if __name__ == "__main__":
    main()
