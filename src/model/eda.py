"""
eda.py — Phase 1 EDA: validate the two-component thesis

Claim 1: absolute skill gap predicts opportunity count (legs / frames / service games)
Claim 2: player_rate × opportunity_count predicts total events better than naive average

All rolling stats computed with strict date-prior cutoff — no look-ahead.
Matches flagged LOW_SAMPLE when player has < 5 prior matches in window.

Usage:
    PYTHONUTF8=1 python -m src.model.eda
    PYTHONUTF8=1 python -m src.model.eda --sport snooker
    PYTHONUTF8=1 python -m src.model.eda --plot
"""

import sys
import argparse
import statistics
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from database import get_conn

FORM_WINDOW   = 15   # max prior matches to include
MIN_SAMPLE    = 5    # below this → LOW_SAMPLE flag
TIER_1_MIN    = 10   # full model
TIER_2_MIN    = 3    # shrinkage


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class MatchRecord:
    match_id:       str
    sport:          str
    match_date:     str
    round:          str
    format:         str
    tournament_id:  str
    player1_id:     str
    player2_id:     str

    # Darts
    p1_avg:         Optional[float] = None
    p2_avg:         Optional[float] = None
    p1_legs_won:    Optional[int]   = None
    p2_legs_won:    Optional[int]   = None
    p1_180s:        Optional[int]   = None
    p2_180s:        Optional[int]   = None

    # Snooker
    p1_frames_won:  Optional[int]   = None
    p2_frames_won:  Optional[int]   = None
    legs_sets_total:Optional[int]   = None
    p1_centuries:   Optional[int]   = None
    p2_centuries:   Optional[int]   = None

    # Tennis
    p1_svpt:        Optional[int]   = None
    p2_svpt:        Optional[int]   = None
    p1_aces:        Optional[int]   = None
    p2_aces:        Optional[int]   = None
    p1_return_pts_won_pct: Optional[float] = None
    p2_return_pts_won_pct: Optional[float] = None

    # Tournament surface (tennis)
    surface:        Optional[str]   = None


@dataclass
class PlayerStat:
    """One match's worth of per-player skill and event data."""
    date:           str
    units_played:   Optional[int]     # legs / frames / service games this player participated in
    events:         Optional[int]     # 180s / centuries / aces by this player
    skill_metric:   Optional[float]   # avg / frame_win_indicator / serve_strength


@dataclass
class RollingProfile:
    player_id:      str
    as_of_date:     str
    n_matches:      int
    skill_metric:   Optional[float]   # rolling avg, frame_win_rate, serve_strength
    event_rate:     Optional[float]   # events per unit (180s/leg, cents/frame, aces/svpt)
    low_sample:     bool
    tier:           int               # 1, 2, or 3


# ── Load data ────────────────────────────────────────────────────────────────

def load_matches(conn, sport: str) -> list[MatchRecord]:
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
    return [MatchRecord(*r) for r in rows]


# ── Rolling stat builder ─────────────────────────────────────────────────────

def build_rolling_profiles(
    matches: list[MatchRecord],
    sport: str,
) -> dict[str, dict[str, RollingProfile]]:
    """
    Returns {match_id: {player_id: RollingProfile}}
    Uses only matches strictly before each match's date.
    """
    # Collect per-player history as we iterate chronologically
    history: dict[str, list[PlayerStat]] = defaultdict(list)
    profiles: dict[str, dict[str, RollingProfile]] = {}

    for m in matches:
        p1_profile = _compute_profile(m.player1_id, m.match_date, history[m.player1_id], sport)
        p2_profile = _compute_profile(m.player2_id, m.match_date, history[m.player2_id], sport)
        profiles[m.match_id] = {m.player1_id: p1_profile, m.player2_id: p2_profile}

        # Now add this match to both players' histories
        _append_history(history, m, sport)

    return profiles


def _compute_profile(
    player_id: str,
    as_of_date: str,
    hist: list[PlayerStat],
    sport: str,
) -> RollingProfile:
    window = hist[-FORM_WINDOW:]  # already date-ordered, strictly prior
    n = len(window)
    tier = 1 if n >= TIER_1_MIN else (2 if n >= TIER_2_MIN else 3)
    low_sample = n < MIN_SAMPLE

    if n == 0:
        return RollingProfile(player_id, as_of_date, 0, None, None, True, 3)

    skill_vals  = [s.skill_metric  for s in window if s.skill_metric  is not None]
    event_vals  = [s.events        for s in window if s.events        is not None and s.units_played]
    unit_vals   = [s.units_played  for s in window if s.events        is not None and s.units_played]

    skill = statistics.mean(skill_vals) if skill_vals else None

    # Event rate: total events / total units across window (more stable than averaging rates)
    if event_vals and unit_vals and sum(unit_vals) > 0:
        event_rate = sum(event_vals) / sum(unit_vals)
    else:
        event_rate = None

    return RollingProfile(player_id, as_of_date, n, skill, event_rate, low_sample, tier)


def _append_history(
    history: dict[str, list[PlayerStat]],
    m: MatchRecord,
    sport: str,
) -> None:
    """Add both players' stats from match m to history."""
    if sport == "darts":
        total_legs = (m.p1_legs_won or 0) + (m.p2_legs_won or 0)
        history[m.player1_id].append(PlayerStat(
            date=m.match_date,
            units_played=total_legs if total_legs > 0 else None,
            events=m.p1_180s,
            skill_metric=m.p1_avg,
        ))
        history[m.player2_id].append(PlayerStat(
            date=m.match_date,
            units_played=total_legs if total_legs > 0 else None,
            events=m.p2_180s,
            skill_metric=m.p2_avg,
        ))

    elif sport == "snooker":
        total_frames = m.legs_sets_total
        # skill_metric per match = 1 if won more frames else 0 (binary win indicator)
        # rolling mean = frame_win_rate
        p1_won_match = (
            1 if (m.p1_frames_won or 0) > (m.p2_frames_won or 0) else 0
            if m.p1_frames_won is not None else None
        )
        p2_won_match = (
            1 if (m.p2_frames_won or 0) > (m.p1_frames_won or 0) else 0
            if m.p2_frames_won is not None else None
        )
        # Use continuous frame win rate per match (not binary win/loss)
        # This captures degree of dominance: winning 9-0 vs 9-8 are different
        p1_fwr = (m.p1_frames_won / total_frames) if (total_frames and m.p1_frames_won is not None) else None
        p2_fwr = (m.p2_frames_won / total_frames) if (total_frames and m.p2_frames_won is not None) else None
        history[m.player1_id].append(PlayerStat(
            date=m.match_date,
            units_played=total_frames,
            events=m.p1_centuries,
            skill_metric=p1_fwr,
        ))
        history[m.player2_id].append(PlayerStat(
            date=m.match_date,
            units_played=total_frames,
            events=m.p2_centuries,
            skill_metric=p2_fwr,
        ))

    elif sport == "tennis":
        total_svpt = (m.p1_svpt or 0) + (m.p2_svpt or 0)
        # serve_strength = how well this player served (100 - opponent's return pts won %)
        p1_serve = (100 - m.p2_return_pts_won_pct) if m.p2_return_pts_won_pct else None
        p2_serve = (100 - m.p1_return_pts_won_pct) if m.p1_return_pts_won_pct else None
        history[m.player1_id].append(PlayerStat(
            date=m.match_date,
            units_played=m.p1_svpt,
            events=m.p1_aces,
            skill_metric=p1_serve,
        ))
        history[m.player2_id].append(PlayerStat(
            date=m.match_date,
            units_played=m.p2_svpt,
            events=m.p2_aces,
            skill_metric=p2_serve,
        ))


# ── Claim 1: skill gap → opportunity count ───────────────────────────────────

@dataclass
class EDARow:
    match_id:       str
    format:         str
    surface:        Optional[str]
    actual_units:   int
    format_max:     int
    absolute_gap:   Optional[float]
    p1_tier:        int
    p2_tier:        int
    low_sample:     bool
    actual_events:  Optional[int]
    predicted_events: Optional[float]  # p1_rate * units + p2_rate * units


def parse_format_max(fmt: str) -> int:
    import re
    m = re.search(r'(\d+)', fmt or '')
    return int(m.group(1)) if m else 0


def build_eda_rows(
    matches: list[MatchRecord],
    profiles: dict[str, dict[str, RollingProfile]],
    sport: str,
) -> list[EDARow]:
    rows = []
    for m in matches:
        pp = profiles.get(m.match_id, {})
        p1p = pp.get(m.player1_id)
        p2p = pp.get(m.player2_id)
        if not p1p or not p2p:
            continue

        # Opportunity count
        if sport == "darts":
            if m.p1_legs_won is None or m.p2_legs_won is None:
                continue
            actual_units  = m.p1_legs_won + m.p2_legs_won
            actual_events = (m.p1_180s or 0) + (m.p2_180s or 0) if (m.p1_180s is not None and m.p2_180s is not None) else None
        elif sport == "snooker":
            if m.legs_sets_total is None:
                continue
            actual_units  = m.legs_sets_total
            actual_events = (m.p1_centuries or 0) + (m.p2_centuries or 0) if (m.p1_centuries is not None and m.p2_centuries is not None) else None
        elif sport == "tennis":
            if m.p1_svpt is None or m.p2_svpt is None:
                continue
            actual_units  = m.p1_svpt + m.p2_svpt
            actual_events = (m.p1_aces or 0) + (m.p2_aces or 0) if (m.p1_aces is not None and m.p2_aces is not None) else None

        # Skill gap
        gap = None
        if p1p.skill_metric is not None and p2p.skill_metric is not None:
            gap = abs(p1p.skill_metric - p2p.skill_metric)

        # Predicted events (Claim 2)
        predicted = None
        if p1p.event_rate is not None and p2p.event_rate is not None:
            if sport == "tennis":
                # Each player's rate applies to their own service points
                p1_units = m.p1_svpt or 0
                p2_units = m.p2_svpt or 0
                predicted = p1p.event_rate * p1_units + p2p.event_rate * p2_units
            else:
                predicted = (p1p.event_rate + p2p.event_rate) * actual_units

        rows.append(EDARow(
            match_id      = m.match_id,
            format        = m.format or "",
            surface       = m.surface,
            actual_units  = actual_units,
            format_max    = parse_format_max(m.format),
            absolute_gap  = gap,
            p1_tier       = p1p.tier,
            p2_tier       = p2p.tier,
            low_sample    = p1p.low_sample or p2p.low_sample,
            actual_events = actual_events,
            predicted_events = predicted,
        ))
    return rows


# ── Statistics ───────────────────────────────────────────────────────────────

def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return 0.0

    def rank(lst):
        s = sorted(range(n), key=lambda i: lst[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and lst[s[j]] == lst[s[j+1]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[s[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    d2 = sum((rx[i] - ry[i])**2 for i in range(n))
    return 1 - 6 * d2 / (n * (n**2 - 1))


def rmse(predicted, actual):
    pairs = [(p, a) for p, a in zip(predicted, actual) if p is not None and a is not None]
    if not pairs:
        return None
    return (sum((p - a)**2 for p, a in pairs) / len(pairs)) ** 0.5


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def gap_bucket(gap: float, sport: str) -> str:
    """Sport-specific thresholds matching natural metric scales."""
    if sport == "darts":          # raw avg points: mean gap ~5.6
        if gap < 3:   return "0–3 (parity)"
        if gap < 7:   return "3–7"
        if gap < 12:  return "7–12"
        return                   "12+ (mismatch)"
    elif sport == "snooker":      # frame_win_rate: 0–1 scale
        if gap < 0.05: return "0–0.05 (parity)"
        if gap < 0.15: return "0.05–0.15"
        if gap < 0.25: return "0.15–0.25"
        return                 "0.25+ (mismatch)"
    else:                         # tennis serve_strength: points scale ~65–80
        if gap < 2:   return "0–2 (parity)"
        if gap < 5:   return "2–5"
        if gap < 9:   return "5–9"
        return                "9+ (mismatch)"


# ── Report ───────────────────────────────────────────────────────────────────

SPORT_UNIT  = {"darts": "legs",  "snooker": "frames", "tennis": "service_pts"}
SPORT_EVENT = {"darts": "180s",  "snooker": "centuries", "tennis": "aces"}
SPORT_SKILL = {"darts": "3dart_avg", "snooker": "match_win_rate", "tennis": "serve_strength"}


def run_sport(matches, profiles, sport, verbose=False):
    rows = build_eda_rows(matches, profiles, sport)
    total = len(rows)

    # ── Filter: exclude low-sample and missing gap
    full = [r for r in rows if not r.low_sample and r.absolute_gap is not None]
    low_sample_count = total - len(full)

    print(f"\n{'='*60}")
    print(f"  EDA — {sport.upper()}")
    print(f"{'='*60}")
    print(f"  Total matches:       {total:,}")
    print(f"  LOW_SAMPLE excluded: {low_sample_count:,}  (< {MIN_SAMPLE} prior matches)")
    print(f"  Analysed:            {len(full):,}")

    if len(full) < 20:
        print(f"  INSUFFICIENT DATA — need ≥ 20 matches with full profiles")
        return None

    # ── CLAIM 1: gap → units ──────────────────────────────────────────────
    print(f"\n  CLAIM 1: |skill_gap| predicts {SPORT_UNIT[sport]} played")
    print(f"  Skill metric: {SPORT_SKILL[sport]}")

    # Group by format, then by gap bucket within format
    from collections import defaultdict
    by_format = defaultdict(list)
    for r in full:
        by_format[r.format].append(r)

    # Show formats with ≥ 20 matches
    # For tennis: segment by surface instead of format (all formats are "SETS")
    if sport == "tennis":
        by_format = defaultdict(list)
        for r in full:
            by_format[r.surface or "Unknown"].append(r)

    top_formats = sorted(by_format, key=lambda f: len(by_format[f]), reverse=True)
    buckets_labels = {
        "darts":   ["0–3 (parity)", "3–7", "7–12", "12+ (mismatch)"],
        "snooker": ["0–0.05 (parity)", "0.05–0.15", "0.15–0.25", "0.25+ (mismatch)"],
        "tennis":  ["0–2 (parity)", "2–5", "5–9", "9+ (mismatch)"],
    }
    blabels = buckets_labels[sport]
    seg_label = "Surface" if sport == "tennis" else "Format"
    print(f"\n  {seg_label:<12} {'N':>5}  {blabels[0]:>14}  {blabels[1]:>10}  {blabels[2]:>10}  {blabels[3]:>14}")
    print(f"  {'-'*12} {'-'*5}  {'-'*14}  {'-'*10}  {'-'*10}  {'-'*14}")

    claim1_signals = []
    for fmt in top_formats:
        frows = by_format[fmt]
        if len(frows) < 20:
            continue
        buckets = defaultdict(list)
        for r in frows:
            buckets[gap_bucket(r.absolute_gap, sport)].append(r.actual_units)
        b0 = buckets.get(blabels[0], [])
        b1 = buckets.get(blabels[1], [])
        b2 = buckets.get(blabels[2], [])
        b3 = buckets.get(blabels[3], [])
        print(f"  {fmt:<12} {len(frows):>5}  "
              f"{mean(b0):>11.1f}({len(b0):>3})  "
              f"{mean(b1):>7.1f}({len(b1):>3})  "
              f"{mean(b2):>7.1f}({len(b2):>3})  "
              f"{mean(b3):>11.1f}({len(b3):>3})")
        if b0 and b3:
            claim1_signals.append(mean(b0) > mean(b3))

    # Overall Spearman
    gaps   = [r.absolute_gap  for r in full if r.absolute_gap is not None]
    units  = [float(r.actual_units) for r in full if r.absolute_gap is not None]

    # Normalise units by format_max to remove format size effect
    units_norm = []
    gaps_norm  = []
    for r in full:
        if r.absolute_gap is not None and r.format_max > 0:
            units_norm.append(r.actual_units / r.format_max)
            gaps_norm.append(r.absolute_gap)

    rho_raw  = spearman(gaps, units)
    rho_norm = spearman(gaps_norm, units_norm) if gaps_norm else 0

    print(f"\n  Spearman ρ (gap vs units):           {rho_raw:+.4f}")
    print(f"  Spearman ρ (gap vs units/format_max):{rho_norm:+.4f}  ← controls for format length")
    print(f"  Expected direction: negative (larger gap → fewer units)")

    if rho_norm < -0.05:
        c1_verdict = "PASS"
    elif rho_norm < 0.05:
        c1_verdict = "WEAK"
    else:
        c1_verdict = "FAIL"
    print(f"\n  Claim 1 verdict: {c1_verdict}")

    # ── CLAIM 2: predicted events vs naive ───────────────────────────────
    pred_rows = [r for r in full if r.predicted_events is not None and r.actual_events is not None]
    print(f"\n  CLAIM 2: model prediction vs naive average")
    print(f"  Rows with both predicted and actual events: {len(pred_rows):,}")

    if len(pred_rows) < 20:
        print(f"  INSUFFICIENT DATA for Claim 2")
        c2_verdict = "INSUFFICIENT"
    else:
        actual_vals = [r.actual_events for r in pred_rows]
        pred_vals   = [r.predicted_events for r in pred_rows]
        naive       = mean(actual_vals)

        rmse_model = rmse(pred_vals, actual_vals)
        rmse_naive = rmse([naive] * len(actual_vals), actual_vals)

        print(f"  Naive baseline (population mean):  {naive:.2f}")
        print(f"  RMSE — model:  {rmse_model:.3f}")
        print(f"  RMSE — naive:  {rmse_naive:.3f}")
        improvement = (rmse_naive - rmse_model) / rmse_naive * 100
        print(f"  Improvement:   {improvement:+.1f}%")

        c2_verdict = "PASS" if improvement > 5 else ("WEAK" if improvement > 0 else "FAIL")
        print(f"\n  Claim 2 verdict: {c2_verdict}")

        # Bias check: does model systematically over/under predict?
        errors = [p - a for p, a in zip(pred_vals, actual_vals)]
        print(f"  Mean prediction error: {mean(errors):+.3f}  (positive = over-predicts)")

    return {"claim1": c1_verdict, "claim2": c2_verdict, "n": len(full)}


# ── Plot ─────────────────────────────────────────────────────────────────────

def plot_claim1(all_rows: dict, sports: list):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot] matplotlib not installed — skipping")
        return

    fig, axes = plt.subplots(1, len(sports), figsize=(6 * len(sports), 5))
    if len(sports) == 1:
        axes = [axes]

    for ax, sport in zip(axes, sports):
        rows = [r for r in all_rows[sport] if not r.low_sample
                and r.absolute_gap is not None and r.format_max > 0]
        if not rows:
            continue

        x = [r.absolute_gap for r in rows]
        y = [r.actual_units / r.format_max for r in rows]

        ax.scatter(x, y, alpha=0.15, s=8, color="steelblue")
        ax.set_xlabel(f"|skill_gap| ({SPORT_SKILL[sport]})")
        ax.set_ylabel(f"units / format_max")
        ax.set_title(f"{sport.title()} — Claim 1\n(funnel expected)")

        # Rolling mean trend line
        if len(x) > 20:
            sorted_pairs = sorted(zip(x, y))
            window = max(5, len(sorted_pairs) // 15)
            trend_x, trend_y = [], []
            for i in range(0, len(sorted_pairs) - window, window // 2):
                chunk = sorted_pairs[i:i + window]
                trend_x.append(statistics.mean(c[0] for c in chunk))
                trend_y.append(statistics.mean(c[1] for c in chunk))
            ax.plot(trend_x, trend_y, color="red", linewidth=2, label="trend")
            ax.legend()

    plt.tight_layout()
    out = ROOT / "eda_claim1.png"
    plt.savefig(out, dpi=120)
    print(f"\n[plot] Saved: {out}")
    plt.show()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["darts", "snooker", "tennis"])
    parser.add_argument("--plot",  action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else ["darts", "snooker", "tennis"]
    conn   = get_conn()
    summary = {}
    all_eda_rows = {}

    for sport in sports:
        matches  = load_matches(conn, sport)
        profiles = build_rolling_profiles(matches, sport)
        result   = run_sport(matches, profiles, sport, args.verbose)
        if result:
            summary[sport] = result
        all_eda_rows[sport] = build_eda_rows(matches, profiles, sport)

    conn.close()

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for sport, r in summary.items():
        c1 = r.get("claim1", "—")
        c2 = r.get("claim2", "—")
        n  = r.get("n", 0)
        icon1 = {"PASS": "✓", "WEAK": "~", "FAIL": "✗", "INSUFFICIENT": "?"}.get(c1, "?")
        icon2 = {"PASS": "✓", "WEAK": "~", "FAIL": "✗", "INSUFFICIENT": "?"}.get(c2, "?")
        print(f"  {sport:<10}  Claim1:[{icon1}]{c1:<14}  Claim2:[{icon2}]{c2:<14}  n={n}")

    passing = [s for s, r in summary.items() if r.get("claim1") == "PASS"]
    failing  = [s for s, r in summary.items() if r.get("claim1") == "FAIL"]

    print()
    if failing:
        print(f"  !! {len(failing)} sport(s) FAILED Claim 1 — thesis does not hold, do not build model")
    passing2 = [s for s, r in summary.items() if r.get("claim2") == "PASS"]
    if passing2:
        print(f"  ✓  {len(passing2)} sport(s) passed Claim 2 — model beats naive average")
    if passing:
        print(f"  ✓  {len(passing)} sport(s) passed Claim 1 — proceed to form_builder.py")

    if args.plot:
        plot_claim1(all_eda_rows, sports)


if __name__ == "__main__":
    main()
