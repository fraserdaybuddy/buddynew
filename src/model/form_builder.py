"""
form_builder.py — Rolling per-player form metrics

Populates player_form table with time-correct rolling stats.
One row per player per match (as_of_date = match date — form as player enters that match).
No look-ahead: only matches strictly before the evaluated match are used.

Architecture:
  DARTS:   avg_3dart + 180_rate_per_leg + trajectory → feeds direct regression
  SNOOKER: frame_win_rate + century_rate_per_frame + style → feeds NegBin
  TENNIS:  ace_rate_per_svpt (surface-split) + return_pts + serve_strength → NegBin

Usage:
    PYTHONUTF8=1 python -m src.model.form_builder              # all sports
    PYTHONUTF8=1 python -m src.model.form_builder --sport darts
    PYTHONUTF8=1 python -m src.model.form_builder --dry-run
"""

import sys
import argparse
import sqlite3
from pathlib import Path
from collections import defaultdict
from typing import Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from database import get_conn, backup

# ── Form window config ────────────────────────────────────────────────────────
FORM_WINDOW   = 7
MIN_SURFACE   = 3     # minimum surface-specific matches before using surface form
STALE_DAYS    = 30

# Decay weights: oldest → newest (7 entries, sum = 1.0)
DECAY_WEIGHTS = [0.05, 0.08, 0.12, 0.15, 0.18, 0.20, 0.22]

# Player tiers
TIER_1_MIN = 10   # full observed rate
TIER_2_MIN = 3    # shrink toward comparable prior
                  # <3 = Tier 3: shrink toward field average

# Snooker style thresholds (century rate per frame)
STYLE_ATTACKING = 0.35
STYLE_BALANCED  = 0.20


# ── Schema migration ──────────────────────────────────────────────────────────

EXTRA_COLS = [
    ("avg_3dart",               "REAL"),    # darts — rolling weighted avg
    ("avg_180_rate_per_leg",    "REAL"),    # darts — trimmed mean
    ("avg_180_rate_median",     "REAL"),    # darts — diagnostic
    ("frame_win_rate",           "REAL"),    # snooker — rolling weighted fwr (skill proxy)
    ("avg_century_rate_per_frame","REAL"),  # snooker — trimmed mean
    ("avg_century_rate_median", "REAL"),    # snooker — diagnostic
    ("century_style",           "TEXT"),    # snooker — attacking/balanced/safety
    ("avg_ace_rate_per_svpt",   "REAL"),    # tennis — overall
    ("avg_ace_rate_grass",      "REAL"),    # tennis — surface-specific
    ("avg_ace_rate_hard",       "REAL"),    # tennis — surface-specific
    ("avg_ace_rate_clay",       "REAL"),    # tennis — surface-specific
    ("avg_serve_strength",      "REAL"),    # tennis — 100 - opp_ret_pts%
    ("avg_return_pts_pct",      "REAL"),    # tennis — return quality
    ("form_trajectory",         "REAL"),    # all — slope of last 7 rates
    ("outlier_in_window",       "INTEGER"), # all — flag
    ("surface_form_missing",    "INTEGER"), # tennis — fallback flag
    ("player_tier",             "INTEGER"), # 1 / 2 / 3
    ("data_quality",            "TEXT"),    # FULL / LOW_SAMPLE / STALE
]


def migrate_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(player_form)")
    existing = {r[1] for r in cur.fetchall()}
    for col, dtype in EXTRA_COLS:
        if col not in existing:
            cur.execute(f"ALTER TABLE player_form ADD COLUMN {col} {dtype}")
    conn.commit()


# ── Maths helpers ─────────────────────────────────────────────────────────────

def _mean(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _weighted_mean(values: list, weights: list) -> Optional[float]:
    """Decay-weighted mean. weights normalised internally."""
    pairs = [(v, w) for v, w in zip(values, weights) if v is not None]
    if not pairs: return None
    total_w = sum(w for _, w in pairs)
    return sum(v * w / total_w for v, w in pairs)


def _trimmed_weighted_mean(values: list, weights: list) -> Optional[float]:
    """
    Drop the single highest and single lowest value, then decay-weight the rest.
    For windows < 5, fall back to plain weighted mean.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return _weighted_mean(values, weights)
    sorted_idx = sorted(range(len(vals)), key=lambda i: vals[i])
    keep = sorted_idx[1:-1]          # drop min and max
    trimmed = [vals[i] for i in keep]
    w_full = DECAY_WEIGHTS[-len(vals):]
    w_trimmed = [w_full[i] for i in keep]
    total = sum(w_trimmed)
    return sum(v * w / total for v, w in zip(trimmed, w_trimmed))


def _median(xs: list) -> Optional[float]:
    xs = sorted([x for x in xs if x is not None])
    if not xs: return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n//2-1] + xs[n//2]) / 2


def _slope(values: list) -> Optional[float]:
    """Linear slope through a list of values (x = index, y = value)."""
    vals = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(vals) < 3: return None
    n = len(vals)
    xs = [v[0] for v in vals]
    ys = [v[1] for v in vals]
    xm, ym = sum(xs)/n, sum(ys)/n
    denom = sum((x-xm)**2 for x in xs)
    if denom == 0: return 0.0
    return sum((x-xm)*(y-ym) for x,y in zip(xs,ys)) / denom


def _is_outlier(rate: float, season_rate: float, threshold: float = 2.5) -> bool:
    return rate > season_rate * threshold if season_rate > 0 else False


def _player_tier(n: int) -> int:
    if n >= TIER_1_MIN: return 1
    if n >= TIER_2_MIN: return 2
    return 3


def _data_quality(n: int) -> str:
    if n >= TIER_1_MIN: return "FULL"
    if n >= TIER_2_MIN: return "LOW_SAMPLE"
    return "STALE"


# ── Darts form ────────────────────────────────────────────────────────────────

class DartsHistory:
    """Per-player match history for darts form computation."""
    __slots__ = ("entries",)
    def __init__(self):
        self.entries: list[dict] = []   # ordered oldest → newest

    def add(self, avg: float, rate_per_leg: float, date: str):
        self.entries.append({"avg": avg, "rate": rate_per_leg, "date": date})

    def form(self) -> dict:
        total_n = len(self.entries)           # total career matches — for tier
        w       = self.entries[-FORM_WINDOW:] # last N for rate calculation
        n       = len(w)
        weights = DECAY_WEIGHTS[-n:] if n > 0 else []

        avgs  = [e["avg"]  for e in w]
        rates = [e["rate"] for e in w]

        avg_weighted    = _weighted_mean(avgs,  weights)
        rate_trimmed    = _trimmed_weighted_mean(rates, weights)
        rate_median     = _median(rates)
        trajectory      = _slope(rates)

        # Outlier flag: any match rate > 2.5× season median
        s_median = _median([e["rate"] for e in self.entries]) or 0
        outlier = any(_is_outlier(r, s_median) for r in rates if r is not None)

        return {
            "n": n,
            "tier": _player_tier(total_n),       # tier from total history
            "data_quality": _data_quality(total_n),
            "avg_3dart": avg_weighted,
            "avg_180_rate_per_leg": rate_trimmed,
            "avg_180_rate_median": rate_median,
            "form_trajectory": trajectory,
            "outlier_in_window": int(outlier),
        }


def build_darts_form(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Process all darts matches chronologically, write form rows."""
    matches = conn.execute("""
        SELECT match_id, match_date, player1_id, player2_id,
               p1_avg, p2_avg, p1_180s, p2_180s,
               p1_legs_won, p2_legs_won
        FROM matches WHERE sport='darts'
        AND p1_avg IS NOT NULL
        ORDER BY match_date ASC, match_id ASC
    """).fetchall()

    history: dict[str, DartsHistory] = defaultdict(DartsHistory)
    rows_written = 0

    for m in matches:
        (mid, date, p1, p2, p1_avg, p2_avg,
         p1_180s, p2_180s, p1_legs, p2_legs) = m

        total_legs = (p1_legs or 0) + (p2_legs or 0)

        for pid, avg, s180 in [(p1, p1_avg, p1_180s), (p2, p2_avg, p2_180s)]:
            form = history[pid].form()
            rate = (s180 / total_legs) if (s180 is not None and total_legs > 0) else None

            if not dry_run:
                _upsert_form(conn, pid, "darts", date, form, {
                    "avg_180s_per_leg": form.get("avg_180_rate_per_leg"),
                    "avg_3dart": form.get("avg_3dart"),
                    "avg_180_rate_per_leg": form.get("avg_180_rate_per_leg"),
                    "avg_180_rate_median": form.get("avg_180_rate_median"),
                    "form_trajectory": form.get("form_trajectory"),
                    "outlier_in_window": form.get("outlier_in_window"),
                    "player_tier": form.get("tier"),
                    "data_quality": form.get("data_quality"),
                })
            rows_written += 1

            # Update history after writing (no look-ahead)
            if avg is not None and rate is not None:
                history[pid].add(avg, rate, date)

    return rows_written


# ── Snooker form ──────────────────────────────────────────────────────────────

class SnookerHistory:
    __slots__ = ("entries",)
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, fwr: float, cent_rate: float, date: str):
        self.entries.append({"fwr": fwr, "cent_rate": cent_rate, "date": date})

    def form(self) -> dict:
        total_n = len(self.entries)
        w       = self.entries[-FORM_WINDOW:]
        n       = len(w)
        weights = DECAY_WEIGHTS[-n:] if n > 0 else []

        fwrs   = [e["fwr"]       for e in w]
        rates  = [e["cent_rate"] for e in w]

        fwr_weighted  = _weighted_mean(fwrs, weights)
        rate_trimmed  = _trimmed_weighted_mean(rates, weights)
        rate_median   = _median(rates)
        trajectory    = _slope(rates)

        style = None
        if rate_trimmed is not None:
            style = ("attacking" if rate_trimmed >= STYLE_ATTACKING
                     else "balanced" if rate_trimmed >= STYLE_BALANCED
                     else "safety")

        s_median = _median([e["cent_rate"] for e in self.entries]) or 0
        outlier = any(_is_outlier(r, s_median) for r in rates if r is not None)

        return {
            "n": n,
            "tier": _player_tier(total_n),
            "data_quality": _data_quality(total_n),
            "frame_win_rate":             fwr_weighted,
            "avg_centuries_per_frame":    rate_trimmed,
            "avg_century_rate_per_frame": rate_trimmed,
            "avg_century_rate_median":    rate_median,
            "century_style": style,
            "form_trajectory": trajectory,
            "outlier_in_window": int(outlier),
        }


def build_snooker_form(conn: sqlite3.Connection, dry_run: bool) -> int:
    matches = conn.execute("""
        SELECT match_id, match_date, player1_id, player2_id,
               p1_frames_won, p2_frames_won, legs_sets_total,
               p1_centuries, p2_centuries
        FROM matches WHERE sport='snooker'
        AND p1_frames_won IS NOT NULL
        ORDER BY match_date ASC, match_id ASC
    """).fetchall()

    history: dict[str, SnookerHistory] = defaultdict(SnookerHistory)
    rows_written = 0

    for m in matches:
        (mid, date, p1, p2,
         p1_frames, p2_frames, total_frames,
         p1_cents, p2_cents) = m

        for pid, frames_won, cents in [(p1, p1_frames, p1_cents), (p2, p2_frames, p2_cents)]:
            form = history[pid].form()

            if not dry_run:
                _upsert_form(conn, pid, "snooker", date, form, {
                    "frame_win_rate":            form.get("frame_win_rate"),
                    "avg_centuries_per_frame":   form.get("avg_centuries_per_frame"),
                    "avg_century_rate_per_frame":form.get("avg_century_rate_per_frame"),
                    "avg_century_rate_median":   form.get("avg_century_rate_median"),
                    "century_style":             form.get("century_style"),
                    "form_trajectory":           form.get("form_trajectory"),
                    "outlier_in_window":         form.get("outlier_in_window"),
                    "player_tier":               form.get("tier"),
                    "data_quality":              form.get("data_quality"),
                })
            rows_written += 1

            fwr = (frames_won / total_frames) if (frames_won is not None and total_frames and total_frames > 0) else None
            cent_rate = (cents / total_frames) if (cents is not None and total_frames and total_frames > 0) else None

            if fwr is not None and cent_rate is not None:
                history[pid].add(fwr, cent_rate, date)

    return rows_written


# ── Tennis form ───────────────────────────────────────────────────────────────

class TennisHistory:
    def __init__(self):
        self.all:  list[dict] = []
        self.surf: dict[str, list[dict]] = defaultdict(list)

    def add(self, ace_rate: float, serve_str: float, ret_pct: float,
            date: str, surface: str):
        entry = {"ace": ace_rate, "serve": serve_str, "ret": ret_pct, "date": date}
        self.all.append(entry)
        if surface:
            self.surf[surface].append(entry)

    def form(self, surface: str) -> dict:
        total_n      = len(self.all)
        surf_entries = self.surf.get(surface, [])
        use_surf     = len(surf_entries) >= MIN_SURFACE
        w = (surf_entries if use_surf else self.all)[-FORM_WINDOW:]
        n = len(w)
        weights = DECAY_WEIGHTS[-n:] if n > 0 else []

        aces   = [e["ace"]   for e in w]
        serves = [e["serve"] for e in w]
        rets   = [e["ret"]   for e in w]

        # Surface-specific rates from full history
        def surface_rate(s):
            entries = self.surf.get(s, [])[-FORM_WINDOW:]
            vals = [e["ace"] for e in entries if e["ace"] is not None]
            if not vals: return None
            wts = DECAY_WEIGHTS[-len(vals):]
            return _weighted_mean(vals, wts)

        trajectory = _slope(aces)
        s_median   = _median([e["ace"] for e in self.all]) or 0
        outlier    = any(_is_outlier(a, s_median) for a in aces if a is not None)

        return {
            "n": n,
            "tier": _player_tier(total_n),
            "data_quality": _data_quality(total_n),
            "avg_ace_rate_per_svpt": _trimmed_weighted_mean(aces, weights),
            "avg_ace_rate_grass":   surface_rate("Grass"),
            "avg_ace_rate_hard":    surface_rate("Hard"),
            "avg_ace_rate_clay":    surface_rate("Clay"),
            "avg_serve_strength":   _weighted_mean(serves, weights),
            "avg_return_pts_pct":   _weighted_mean(rets,   weights),
            "form_trajectory":      trajectory,
            "outlier_in_window":    int(outlier),
            "surface_form_missing": int(not use_surf),
        }


def build_tennis_form(conn: sqlite3.Connection, dry_run: bool) -> int:
    matches = conn.execute("""
        SELECT m.match_id, m.match_date, m.player1_id, m.player2_id,
               m.p1_aces, m.p2_aces, m.p1_svpt, m.p2_svpt,
               m.p1_return_pts_won_pct, m.p2_return_pts_won_pct,
               t.surface
        FROM matches m
        LEFT JOIN tournaments t ON t.tournament_id = m.tournament_id
        WHERE m.sport='tennis' AND m.p1_svpt IS NOT NULL
        ORDER BY m.match_date ASC, m.match_id ASC
    """).fetchall()

    history: dict[str, TennisHistory] = defaultdict(TennisHistory)
    rows_written = 0

    for m in matches:
        (mid, date, p1, p2,
         p1_aces, p2_aces, p1_svpt, p2_svpt,
         p1_ret, p2_ret, surface) = m

        surface = surface or "Hard"

        for pid, aces, svpt, opp_ret, own_ret in [
            (p1, p1_aces, p1_svpt, p2_ret, p1_ret),
            (p2, p2_aces, p2_svpt, p1_ret, p2_ret),
        ]:
            form = history[pid].form(surface)

            if not dry_run:
                _upsert_form(conn, pid, "tennis", date, form, {
                    "avg_aces_per_match": form.get("avg_ace_rate_per_svpt"),
                    "avg_return_pts_won": form.get("avg_return_pts_pct"),
                    "avg_ace_rate_per_svpt": form.get("avg_ace_rate_per_svpt"),
                    "avg_ace_rate_grass":    form.get("avg_ace_rate_grass"),
                    "avg_ace_rate_hard":     form.get("avg_ace_rate_hard"),
                    "avg_ace_rate_clay":     form.get("avg_ace_rate_clay"),
                    "avg_serve_strength":    form.get("avg_serve_strength"),
                    "avg_return_pts_pct":    form.get("avg_return_pts_pct"),
                    "form_trajectory":       form.get("form_trajectory"),
                    "outlier_in_window":     form.get("outlier_in_window"),
                    "surface_form_missing":  form.get("surface_form_missing"),
                    "player_tier":           form.get("tier"),
                    "data_quality":          form.get("data_quality"),
                })
            rows_written += 1

            ace_rate   = (aces / svpt)   if (aces is not None and svpt and svpt > 0) else None
            serve_str  = (100 - opp_ret) if opp_ret is not None else None
            ret_pct    = own_ret

            if ace_rate is not None or serve_str is not None:
                history[pid].add(
                    ace_rate  or 0,
                    serve_str or 0,
                    ret_pct   or 0,
                    date, surface,
                )

    return rows_written


# ── DB write ──────────────────────────────────────────────────────────────────

def _upsert_form(conn, player_id, sport, date, form, extra_fields):
    base = {
        "player_id":       player_id,
        "sport":           sport,
        "as_of_date":      date,
        "matches_counted": form.get("n", 0),
    }
    row = {**base, **extra_fields}
    cols   = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    vals   = list(row.values())
    # ON CONFLICT: replace (as_of_date is per match_date — last one wins)
    conn.execute(
        f"INSERT OR REPLACE INTO player_form ({cols}) VALUES ({placeholders})",
        vals,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["darts","snooker","tennis"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else ["darts","snooker","tennis"]

    if not args.dry_run:
        conn = get_conn()
        backup_path = backup("pre_form_builder")
        print(f"[form_builder] Backup: {backup_path}")
    else:
        conn = get_conn()
        print("[form_builder] DRY RUN — no writes")

    # Extend schema
    migrate_schema(conn)
    print("[form_builder] Schema up to date")

    total = 0
    for sport in sports:
        print(f"[form_builder] Processing {sport}...")
        if sport == "darts":
            n = build_darts_form(conn, args.dry_run)
        elif sport == "snooker":
            n = build_snooker_form(conn, args.dry_run)
        elif sport == "tennis":
            n = build_tennis_form(conn, args.dry_run)
        else:
            continue
        print(f"  {sport}: {n:,} form rows {'(dry)' if args.dry_run else 'written'}")
        total += n

    if not args.dry_run:
        conn.commit()

    # Report player_form row counts
    print()
    print("[form_builder] player_form counts:")
    for sport in ["darts","snooker","tennis"]:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT player_id) FROM player_form WHERE sport=?", (sport,))
        rows, players = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM player_form WHERE sport=? AND player_tier=1", (sport,))
        t1 = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM player_form WHERE sport=? AND player_tier=2", (sport,))
        t2 = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM player_form WHERE sport=? AND player_tier=3", (sport,))
        t3 = cur.fetchone()[0]
        print(f"  {sport:<10}: {rows:>6,} rows  {players:>4} players  "
              f"[T1:{t1:,} T2:{t2:,} T3:{t3:,}]")

    conn.close()
    print(f"\n[form_builder] Done — {total:,} total rows processed")


if __name__ == "__main__":
    main()
