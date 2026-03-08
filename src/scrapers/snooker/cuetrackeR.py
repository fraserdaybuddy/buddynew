"""
scrapers/snooker/cuetrackeR.py — Snooker historical data scraper
JOB-006 Sports Betting Model

Source: cuetracker.net
Scrapes WST tournament results including centuries per match/frame.
"""

import re
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error
from html.parser import HTMLParser
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database import get_conn, DB_PATH, backup
from resolver import Resolver, ResolutionFailed, ResolutionQueued


TOUR = "WST"
SOURCE = "cuetrackeR"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JOB006-research-bot/1.0)"
}

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_snooker (
    staging_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_name     TEXT,
    tournament_year     INTEGER,
    round               TEXT,
    match_date          TEXT,
    p1_raw_name         TEXT NOT NULL,
    p2_raw_name         TEXT NOT NULL,
    p1_frames           INTEGER,
    p2_frames           INTEGER,
    p1_centuries        INTEGER,
    p2_centuries        INTEGER,
    format              TEXT,
    source_url          TEXT,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    failure_reason      TEXT,
    scraped_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_snooker_staging(conn: sqlite3.Connection) -> None:
    conn.executescript(STAGING_SCHEMA)


def fetch(url: str, delay: float = 1.5) -> Optional[str]:
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"[snooker-scraper] HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"[snooker-scraper] Error fetching {url}: {e}")
        return None


def detect_snooker_format(tournament_name: str, round_: str) -> str:
    """Derive best-of format from tournament + round."""
    name = tournament_name.lower()
    round_lower = round_.lower()

    if "world" in name:
        if "final" in round_lower:
            return "BO35"
        if "semi" in round_lower:
            return "BO33"
        if "quarter" in round_lower:
            return "BO25"
        return "BO19"

    if any(t in name for t in ["masters", "uk championship", "tour championship"]):
        if "final" in round_lower:
            return "BO19"
        if "semi" in round_lower:
            return "BO17"
        return "BO13"

    # Ranking events (standard)
    if "final" in round_lower:
        return "BO17"
    if "semi" in round_lower:
        return "BO13"
    if "quarter" in round_lower:
        return "BO11"

    return "BO9"


class CuetrackerScraper:
    """
    Scrape historical WST match results from cuetracker.net.

    CueTracker has match pages at:
      https://cuetracker.net/tournaments/{tournament-slug}/matches

    Each match page lists: player names, frame score, centuries by player.
    """

    BASE_URL = "https://cuetracker.net"

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.resolver = Resolver(db_path)
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with get_conn(self.db_path) as conn:
            ensure_snooker_staging(conn)

    def scrape_match_page(
        self,
        url: str,
        tournament_name: str,
        year: int,
        round_: str = "UNKNOWN",
        match_date: Optional[str] = None,
    ) -> dict:
        """
        Scrape a single match page from CueTracker.
        Returns summary dict.
        """
        print(f"[snooker-scraper] Fetching: {url}")
        html = fetch(url)
        if html is None:
            return {"status": "BLOCKED"}

        # Parse player names, scores, centuries from match page
        # CueTracker structure: player names in h2/h3 tags,
        # centuries in a table or list
        data = self._parse_match_page(html, url)
        if not data:
            return {"status": "PARSE_FAILED"}

        fmt = detect_snooker_format(tournament_name, round_)

        with get_conn(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO staging_snooker
                   (tournament_name, tournament_year, round, match_date,
                    p1_raw_name, p2_raw_name,
                    p1_frames, p2_frames,
                    p1_centuries, p2_centuries,
                    format, source_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tournament_name, year, round_,
                    match_date or f"{year}-01-01",
                    data["p1_name"], data["p2_name"],
                    data.get("p1_frames"), data.get("p2_frames"),
                    data.get("p1_centuries"), data.get("p2_centuries"),
                    fmt, url,
                )
            )

        return {"status": "OK", "data": data}

    def _parse_match_page(self, html: str, url: str) -> Optional[dict]:
        """
        Parse CueTracker match page HTML.
        Returns dict with player names, scores, centuries or None.
        """
        # Player names — look for patterns in the HTML
        # CueTracker uses: <td class="player">Name</td>
        names = re.findall(r'class="player[^"]*"[^>]*>([^<]+)<', html)
        scores = re.findall(r'class="score[^"]*"[^>]*>(\d+)<', html)

        # Centuries — look for century counts per player
        century_matches = re.findall(
            r'(\d+)\s*(?:centur(?:y|ies)|100\+)', html, re.IGNORECASE
        )

        if len(names) < 2:
            # Try alternate pattern
            names = re.findall(r'<h[23][^>]*>\s*([A-Z][a-z]+ [A-Z][a-zA-Z\s\'-]+?)\s*</h[23]>', html)

        if len(names) < 2:
            print(f"[snooker-scraper] Could not parse player names from {url}")
            return None

        p1_name = names[0].strip()
        p2_name = names[1].strip()

        p1_frames = int(scores[0]) if len(scores) > 0 else None
        p2_frames = int(scores[1]) if len(scores) > 1 else None

        p1_centuries = int(century_matches[0]) if len(century_matches) > 0 else None
        p2_centuries = int(century_matches[1]) if len(century_matches) > 1 else None

        return {
            "p1_name": p1_name,
            "p2_name": p2_name,
            "p1_frames": p1_frames,
            "p2_frames": p2_frames,
            "p1_centuries": p1_centuries,
            "p2_centuries": p2_centuries,
        }

    def scrape_tournament_index(
        self,
        tournament_slug: str,
        tournament_name: str,
        year: int,
    ) -> dict:
        """
        Scrape a full tournament from CueTracker by tournament slug.
        e.g. tournament_slug = "world-snooker-championship-2024"
        """
        url = f"{self.BASE_URL}/tournaments/{tournament_slug}/matches"
        print(f"\n[snooker-scraper] Tournament index: {url}")

        html = fetch(url)
        if html is None:
            print(f"[snooker-scraper] BLOCKED: {url}")
            return {"status": "BLOCKED", "matches": 0}

        # Find match page links
        match_links = re.findall(
            r'href="(/tournaments/[^"]+/matches/\d+)"', html
        )
        match_links = list(set(match_links))  # dedupe
        print(f"[snooker-scraper] Found {len(match_links)} match links")

        scraped = 0
        for link in match_links:
            full_url = self.BASE_URL + link
            result = self.scrape_match_page(
                full_url, tournament_name, year
            )
            if result.get("status") == "OK":
                scraped += 1

        return {"status": "OK", "matches": scraped}

    def promote_to_matches(self) -> dict:
        """Resolve player names and promote staged rows to matches table."""
        backup("promote_snooker")
        print("\n[snooker-scraper] Promoting staged rows to matches...")

        with get_conn(self.db_path) as conn:
            pending = conn.execute(
                "SELECT * FROM staging_snooker WHERE status='PENDING'"
            ).fetchall()

        promoted = 0
        queued = 0
        failed = 0

        for row in pending:
            row = dict(row)
            try:
                p1_id = self.resolver.resolve(
                    row["p1_raw_name"], TOUR, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
                p2_id = self.resolver.resolve(
                    row["p2_raw_name"], TOUR, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
            except ResolutionQueued as e:
                queued += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_snooker SET status='QUEUED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue
            except ResolutionFailed as e:
                failed += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_snooker SET status='FAILED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue

            tournament_id = (
                f"WST-{row['tournament_year']}-"
                f"{row['tournament_name'][:20].upper().replace(' ', '_')}"
            )

            with get_conn(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO tournaments
                       (tournament_id, sport, tour, name, year)
                       VALUES (?, 'snooker', 'WST', ?, ?)""",
                    (tournament_id, row["tournament_name"], row["tournament_year"])
                )

                match_id = hashlib.sha256(
                    f"{tournament_id}|{row['round']}|{p1_id}|{p2_id}|{row['match_date']}".encode()
                ).hexdigest()[:16]

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO matches
                           (match_id, tournament_id, sport, round, match_date,
                            player1_id, player2_id,
                            format, legs_sets_total,
                            p1_centuries, p2_centuries,
                            data_source, source_url)
                           VALUES (?, ?, 'snooker', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            match_id, tournament_id,
                            row["round"], row["match_date"],
                            p1_id, p2_id,
                            row["format"],
                            (row.get("p1_frames") or 0) + (row.get("p2_frames") or 0),
                            row.get("p1_centuries"), row.get("p2_centuries"),
                            SOURCE, row.get("source_url"),
                        )
                    )
                    conn.execute(
                        "UPDATE staging_snooker SET status='RESOLVED' WHERE staging_id=?",
                        (row["staging_id"],)
                    )
                    promoted += 1
                except Exception as e:
                    print(f"[snooker-scraper] Match insert error: {e}")
                    failed += 1

        print(
            f"[snooker-scraper] Promoted: {promoted} | "
            f"Queued: {queued} | Failed: {failed}"
        )
        return {"promoted": promoted, "queued": queued, "failed": failed}
