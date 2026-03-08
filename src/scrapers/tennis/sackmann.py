"""
scrapers/tennis/sackmann.py — Tennis historical data from Jeff Sackmann's dataset
JOB-006 Sports Betting Model

Source: github.com/JeffSackmann/tennis_atp (and tennis_wta)
Free, comprehensive, updated regularly. CSV files going back to 1968.
Columns we care about: winner_name, loser_name, w_ace, l_ace,
w_svpt, l_svpt, w_2ndWon, l_2ndWon (return pressure proxy)

Data available per tour:
  ATP: atp_matches_{year}.csv
  WTA: wta_matches_{year}.csv
"""

import csv
import io
import time
import urllib.request
from pathlib import Path
from typing import Optional
import hashlib
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database import get_conn, DB_PATH, backup
from resolver import Resolver, ResolutionFailed, ResolutionQueued
from scrapers.darts.dartsdatabase import ensure_staging  # reuse staging concept


TOUR_ATP = "ATP"
TOUR_WTA = "WTA"
SOURCE = "sackmann_atp"

# GitHub raw CSV base URLs
ATP_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master"
WTA_BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JOB006-research-bot/1.0)"
}


STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_tennis (
    staging_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tour                TEXT NOT NULL,          -- ATP | WTA
    tournament_name     TEXT,
    tournament_year     INTEGER,
    surface             TEXT,
    round               TEXT,
    match_date          TEXT,
    p1_raw_name         TEXT NOT NULL,          -- winner
    p2_raw_name         TEXT NOT NULL,          -- loser
    p1_score            TEXT,
    p1_aces             INTEGER,
    p2_aces             INTEGER,
    p1_svpt             INTEGER,                -- serve points — for rate calc
    p2_svpt             INTEGER,
    p1_2nd_won          INTEGER,                -- 2nd serve points won by p1
    p2_2nd_won          INTEGER,
    p1_ret_pts_won_pct  REAL,                   -- return pressure proxy (derived)
    p2_ret_pts_won_pct  REAL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    failure_reason      TEXT,
    source_url          TEXT,
    scraped_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_tennis_staging(conn: sqlite3.Connection) -> None:
    conn.executescript(STAGING_SCHEMA)


def fetch_csv(url: str, delay: float = 0.5) -> Optional[list[dict]]:
    """Fetch a CSV URL and return list of row dicts."""
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)
    except Exception as e:
        print(f"[tennis-scraper] Error fetching {url}: {e}")
        return None


def safe_int(val: str) -> Optional[int]:
    try:
        return int(float(val)) if val and val.strip() else None
    except (ValueError, TypeError):
        return None


def safe_float(val: str) -> Optional[float]:
    try:
        return float(val) if val and val.strip() else None
    except (ValueError, TypeError):
        return None


def calc_return_pct(ret_pts_won: Optional[int], opp_svpt: Optional[int]) -> Optional[float]:
    """Return % of opponent serve points won = return pressure score."""
    if ret_pts_won is None or opp_svpt is None or opp_svpt == 0:
        return None
    return round(ret_pts_won / opp_svpt * 100, 2)


class SackmannScraper:
    """
    Load Jeff Sackmann's ATP/WTA CSV files into staging_tennis.

    Usage:
        scraper = SackmannScraper()
        scraper.load_year(2023, tour="ATP")
        scraper.load_year(2022, tour="ATP")
        scraper.promote_to_matches()
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.resolver_atp = Resolver(db_path)
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with get_conn(self.db_path) as conn:
            ensure_tennis_staging(conn)

    def load_year(self, year: int, tour: str = "ATP") -> dict:
        """
        Download and stage one year of ATP or WTA match data.
        """
        tour = tour.upper()
        if tour == "ATP":
            url = f"{ATP_BASE}/atp_matches_{year}.csv"
        elif tour == "WTA":
            url = f"{WTA_BASE}/wta_matches_{year}.csv"
        else:
            raise ValueError(f"Unknown tour: {tour}")

        print(f"\n[tennis-scraper] Loading {tour} {year}: {url}")

        rows = fetch_csv(url)
        if rows is None:
            print(f"[tennis-scraper] BLOCKED: could not fetch {url}")
            return {"loaded": 0, "status": "BLOCKED"}

        inserted = 0
        skipped = 0

        with get_conn(self.db_path) as conn:
            for row in rows:
                winner = row.get("winner_name", "").strip()
                loser = row.get("loser_name", "").strip()

                if not winner or not loser:
                    skipped += 1
                    continue

                # Aces
                w_ace = safe_int(row.get("w_ace", ""))
                l_ace = safe_int(row.get("l_ace", ""))

                # Serve points (for rate calculation)
                w_svpt = safe_int(row.get("w_svpt", ""))
                l_svpt = safe_int(row.get("l_svpt", ""))

                # 2nd serve won (opponent's return pts won proxy)
                # w_2ndWon = points won on winner's 2nd serve
                # loser's return pressure = (l_svpt - w_2ndWon) / l_svpt
                w_2nd_won = safe_int(row.get("w_2ndWon", ""))
                l_2nd_won = safe_int(row.get("l_2ndWon", ""))

                # Return pressure: % of opponent's serve points won
                # p1 (winner) return pct = points won returning loser's serve
                # = loser's total svpt - loser's svpt won = l_svpt - l's won
                # Sackmann cols: w_svpt = winner serve pts played
                # l_bpFaced, l_bpSaved etc available too
                # Simple proxy: p1_ret_pts_won = l_svpt - l_svpt_won
                # l_svpt_won isn't direct — use: l_svpt - w serve pts won on return
                # Actually: winner's return pts won = loser's svpt - loser's serve pts won
                # loser's serve pts won not directly available but: l_svpt - (l_1stWon + l_2ndWon)
                l_1st_won = safe_int(row.get("l_1stWon", ""))
                w_1st_won = safe_int(row.get("w_1stWon", ""))

                p1_ret_pct = None
                p2_ret_pct = None
                if l_svpt and l_1st_won is not None and l_2nd_won is not None:
                    l_svpt_won = l_1st_won + l_2nd_won
                    p1_ret_pct = calc_return_pct(l_svpt - l_svpt_won, l_svpt)
                if w_svpt and w_1st_won is not None and w_2nd_won is not None:
                    w_svpt_won = w_1st_won + w_2nd_won
                    p2_ret_pct = calc_return_pct(w_svpt - w_svpt_won, w_svpt)

                match_date = row.get("tourney_date", "")
                if match_date and len(match_date) == 8:
                    match_date = f"{match_date[:4]}-{match_date[4:6]}-{match_date[6:]}"

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO staging_tennis
                           (tour, tournament_name, tournament_year, surface, round,
                            match_date, p1_raw_name, p2_raw_name, p1_score,
                            p1_aces, p2_aces,
                            p1_svpt, p2_svpt,
                            p1_2nd_won, p2_2nd_won,
                            p1_ret_pts_won_pct, p2_ret_pts_won_pct,
                            source_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tour,
                            row.get("tourney_name", ""),
                            year,
                            row.get("surface", ""),
                            row.get("round", ""),
                            match_date,
                            winner, loser,
                            row.get("score", ""),
                            w_ace, l_ace,
                            w_svpt, l_svpt,
                            w_2nd_won, l_2nd_won,
                            p1_ret_pct, p2_ret_pct,
                            url,
                        )
                    )
                    inserted += 1
                except Exception as e:
                    print(f"[tennis-scraper] Insert error: {e}")
                    skipped += 1

        print(f"[tennis-scraper] Staged: {inserted} | Skipped: {skipped}")
        return {"loaded": inserted, "skipped": skipped, "status": "OK"}

    def load_years(self, years: list[int], tour: str = "ATP") -> dict:
        """Load multiple years."""
        total = {"loaded": 0, "skipped": 0}
        for year in years:
            result = self.load_year(year, tour)
            total["loaded"] += result.get("loaded", 0)
            total["skipped"] += result.get("skipped", 0)
        return total

    def promote_to_matches(self) -> dict:
        """
        Resolve player names and promote staged rows to matches table.
        """
        backup("promote_tennis")
        print("\n[tennis-scraper] Promoting staged rows to matches...")

        with get_conn(self.db_path) as conn:
            pending = conn.execute(
                "SELECT * FROM staging_tennis WHERE status='PENDING'"
            ).fetchall()

        promoted = 0
        queued = 0
        failed = 0

        for row in pending:
            row = dict(row)
            tour = row["tour"]

            try:
                p1_id = self.resolver_atp.resolve(
                    row["p1_raw_name"], tour, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
                p2_id = self.resolver_atp.resolve(
                    row["p2_raw_name"], tour, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
            except ResolutionQueued as e:
                queued += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_tennis SET status='QUEUED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue
            except ResolutionFailed as e:
                failed += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_tennis SET status='FAILED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue

            tournament_id = (
                f"{tour}-{row['tournament_year']}-"
                f"{row['tournament_name'][:20].upper().replace(' ', '_').replace('/', '_')}"
            )

            with get_conn(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO tournaments
                       (tournament_id, sport, tour, name, year, surface)
                       VALUES (?, 'tennis', ?, ?, ?, ?)""",
                    (tournament_id, tour, row["tournament_name"],
                     row["tournament_year"], row.get("surface"))
                )

                match_id = hashlib.sha256(
                    f"{tournament_id}|{row['round']}|{p1_id}|{p2_id}|{row['match_date']}".encode()
                ).hexdigest()[:16]

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO matches
                           (match_id, tournament_id, sport, round, match_date,
                            player1_id, player2_id,
                            format,
                            p1_aces, p2_aces,
                            p1_return_pts_won_pct, p2_return_pts_won_pct,
                            data_source, source_url)
                           VALUES (?, ?, 'tennis', ?, ?, ?, ?, 'SETS', ?, ?, ?, ?, ?, ?)""",
                        (
                            match_id, tournament_id,
                            row["round"], row["match_date"],
                            p1_id, p2_id,
                            row.get("p1_aces"), row.get("p2_aces"),
                            row.get("p1_ret_pts_won_pct"),
                            row.get("p2_ret_pts_won_pct"),
                            SOURCE, row.get("source_url"),
                        )
                    )
                    conn.execute(
                        "UPDATE staging_tennis SET status='RESOLVED' WHERE staging_id=?",
                        (row["staging_id"],)
                    )
                    promoted += 1
                except Exception as e:
                    print(f"[tennis-scraper] Match insert error: {e}")
                    failed += 1

        print(
            f"[tennis-scraper] Promoted: {promoted} | "
            f"Queued (needs review): {queued} | "
            f"Failed: {failed}"
        )
        return {"promoted": promoted, "queued": queued, "failed": failed}
