"""
elo_warmup.py — Load historical Sackmann data to warm up ELO ratings
JOB-006 Sports Betting Model

Problem: elo_loader.py processes only 2024 matches. All players start at 1500
so ELO gap is too small (avg 49 pts) to predict outcomes meaningfully.

Solution: download 2019-2023 ATP+WTA CSVs from Sackmann, walk ELO chronologically
through historical matches, then use the resulting ratings as starting values when
elo_loader.py processes 2024 matches.

Historical data is NOT stored in the matches table (only used for ELO training).
Final ELO values are stored in elo_ratings table tagged with surface.
After this runs, call elo_loader.py again to overwrite 2024 match ELO snapshots.

Usage:
    python src/model/elo_warmup.py [--years 5]   # default: 2019-2023
"""

import argparse
import csv
import io
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "universe.db"

ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JOB006-research-bot/1.0)"}

INITIAL_ELO = 1500.0
SURFACES = ("Hard", "Clay", "Grass")
SURFACE_KEY = "Overall"


def k_factor(n: int) -> float:
    if n < 10:
        return 40.0
    if n < 30:
        return 24.0
    return 16.0


def expected_score(r_a: float, r_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / 400.0))


def normalize_surface(raw: str) -> str | None:
    if not raw:
        return None
    s = raw.strip().title()
    if s in ("Hard", "Clay", "Grass"):
        return s
    if "Hard" in s or "Indoor" in s or "Carpet" in s:
        return "Hard"
    if "Clay" in s or "Dirt" in s:
        return "Clay"
    if "Grass" in s:
        return "Grass"
    return None


def fetch_csv(url: str) -> list | None:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)
    except Exception as e:
        print(f"  [warmup] BLOCKED: {url} ({e})")
        return None


def safe_int(v):
    try:
        return int(float(v)) if v and str(v).strip() else None
    except (ValueError, TypeError):
        return None


class WarmupRatingStore:
    def __init__(self):
        self.ratings: dict = {}
        self.counts: dict = {}

    def get(self, pid: str, surface: str) -> float:
        return self.ratings.get((pid, surface), INITIAL_ELO)

    def count(self, pid: str, surface: str) -> int:
        return self.counts.get((pid, surface), 0)

    def update(self, pid: str, surface: str, new_elo: float) -> None:
        self.ratings[(pid, surface)] = new_elo
        self.counts[(pid, surface)] = self.counts.get((pid, surface), 0) + 1


def process_match(store: WarmupRatingStore, winner_id: str, loser_id: str, surface: str) -> None:
    """Update store with one match outcome."""
    # Surface ELO
    if surface:
        r_w_s = store.get(winner_id, surface)
        r_l_s = store.get(loser_id, surface)
        n_w_s = store.count(winner_id, surface)
        n_l_s = store.count(loser_id, surface)
        e_w = expected_score(r_w_s, r_l_s)
        e_l = expected_score(r_l_s, r_w_s)
        store.update(winner_id, surface, r_w_s + k_factor(n_w_s) * (1.0 - e_w))
        store.update(loser_id, surface, r_l_s + k_factor(n_l_s) * (0.0 - e_l))

    # Overall ELO
    r_w_o = store.get(winner_id, SURFACE_KEY)
    r_l_o = store.get(loser_id, SURFACE_KEY)
    n_w_o = store.count(winner_id, SURFACE_KEY)
    n_l_o = store.count(loser_id, SURFACE_KEY)
    e_w = expected_score(r_w_o, r_l_o)
    e_l = expected_score(r_l_o, r_w_o)
    store.update(winner_id, SURFACE_KEY, r_w_o + k_factor(n_w_o) * (1.0 - e_w))
    store.update(loser_id, SURFACE_KEY, r_l_o + k_factor(n_l_o) * (0.0 - e_l))


def make_player_id(name: str, tour: str) -> str:
    """Create a stable player ID from name (must match elo_loader.py convention)."""
    # Sackmann name → same format as resolver.py: ATP-SURNAME-INITIAL
    # Use the same logic as the existing ATP resolver
    parts = name.strip().split()
    if len(parts) >= 2:
        surname = parts[-1].upper().replace("-", "").replace("'", "")
        initial = parts[0][0].upper()
        return f"{tour}-{surname}-{initial}"
    return f"{tour}-{name.upper().replace(' ', '_')}"


def is_retired(score: str) -> bool:
    """Return True if match ended in retirement/walkover."""
    if not score:
        return True
    return bool(re.search(r"\b(RET|DEF|ABD|W/O|WO)\b", score, re.I))


def run_warmup(years: list[int] = None, tours: list[str] = None) -> None:
    if years is None:
        years = list(range(2019, 2026))  # 2019-2025 inclusive
    if tours is None:
        tours = ["ATP", "WTA"]

    print(f"[warmup] Loading {len(years)} years × {len(tours)} tours for ELO warm-up...")
    print(f"[warmup] Years: {years}  Tours: {tours}")

    store = WarmupRatingStore()
    total_matches = 0
    total_skipped = 0

    # Collect all rows first (need to sort chronologically across years/tours)
    all_rows = []

    for year in years:
        for tour in tours:
            if tour == "ATP":
                url = f"{ATP_BASE}/atp_matches_{year}.csv"
            else:
                url = f"{WTA_BASE}/wta_matches_{year}.csv"

            print(f"  [warmup] Fetching {tour} {year}...", end=" ", flush=True)
            rows = fetch_csv(url)
            if rows is None:
                continue

            for row in rows:
                w_name = row.get("winner_name", "").strip()
                l_name = row.get("loser_name", "").strip()
                score = row.get("score", "")
                if not w_name or not l_name:
                    continue
                if is_retired(score):
                    continue
                date_raw = row.get("tourney_date", "")
                if len(date_raw) == 8:
                    date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
                else:
                    date = date_raw
                all_rows.append({
                    "date": date,
                    "winner_id": make_player_id(w_name, tour),
                    "loser_id": make_player_id(l_name, tour),
                    "surface": normalize_surface(row.get("surface", "")),
                })
            print(f"{len(rows)} rows")
            time.sleep(0.3)  # be polite to GitHub

    # Sort chronologically
    all_rows.sort(key=lambda r: r["date"])
    print(f"\n[warmup] Total historical matches: {len(all_rows)}")

    # Walk ELO
    for row in all_rows:
        process_match(store, row["winner_id"], row["loser_id"], row["surface"])
        total_matches += 1

    print(f"[warmup] ELO walk complete: {total_matches} matches processed")
    print(f"[warmup] Unique player×surface combos: {len(store.ratings)}")

    # Save to elo_ratings table (these become the starting point for elo_loader.py)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM elo_ratings WHERE 1=1")  # full reset

    rows_to_insert = []
    for (pid, surface), elo in store.ratings.items():
        n = store.counts.get((pid, surface), 0)
        rows_to_insert.append((pid, surface, round(elo, 2), n, None))

    conn.executemany(
        "INSERT INTO elo_ratings (player_id, surface, elo, match_count, last_updated) VALUES (?,?,?,?,?)",
        rows_to_insert
    )
    conn.commit()
    conn.close()

    print(f"[warmup] Saved {len(rows_to_insert)} ratings to elo_ratings")

    # Print top 10 Hard ELO sanity check
    conn2 = sqlite3.connect(DB_PATH)
    rows_check = conn2.execute(
        """SELECT er.player_id, er.elo, er.match_count
           FROM elo_ratings er WHERE er.surface='Hard'
           ORDER BY er.elo DESC LIMIT 10"""
    ).fetchall()
    print("\nTop 10 Hard ELO after warm-up (player_id):")
    for i, r in enumerate(rows_check, 1):
        print(f"  {i:2}. {r[0]:<30} {r[1]:>7.1f}  (n={r[2]})")

    conn2.close()
    print("\n[warmup] Done. Now re-running elo_loader.py to populate 2024 match snapshots...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5,
                        help="Number of historical years to load (default: 5 → 2019-2023)")
    args = parser.parse_args()

    current_year = 2025  # most recent year in Sackmann repo + 1
    start_year = current_year - args.years
    years = list(range(start_year, current_year))

    run_warmup(years=years)

    # Re-run elo_loader with warm-up ELO as starting values
    print("\n[warmup] Re-running elo_loader with warm-up starting values...")
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from src.model.elo_loader import run as run_elo_loader

    # Patch elo_loader to use warm-up ratings as initial values
    # elo_loader already reads from elo_ratings for nothing — it starts fresh.
    # We need to modify elo_loader to use pre-loaded starting values.
    # For now: re-run with a flag to use warm ratings.
    run_elo_loader(warm_start=True)
