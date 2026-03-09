"""
*** DEPRECATED — 2026-03-09 ***
dartsdatabase.co.uk does NOT provide per-match 180s data.
Event pages only show match scores and 3-dart averages.
PRIMARY darts scraper is now: src/scrapers/darts/darts24.py (darts24.com)

scrapers/darts/dartsdatabase.py — Darts historical data scraper
JOB-006 Sports Betting Model

Source: dartsdatabase.co.uk
Scrapes PDC tournament results including 180s per match.

RULES:
  - Never fabricate data. If a page is unavailable, report BLOCKED.
  - Write to staging table only. Validator promotes to matches.
  - All player names go through Resolver before any match is inserted.
"""

import re
import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error
from html.parser import HTMLParser

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database import get_conn, DB_PATH, backup
from resolver import Resolver, ResolutionFailed, ResolutionQueued
from config import get_sport


TOUR = "PDC"
SOURCE = "dartsdatabase"

# ─────────────────────────────────────────────
# Staging table (separate from matches)
# ─────────────────────────────────────────────

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_darts (
    staging_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_name TEXT,
    tournament_year INTEGER,
    round           TEXT,
    match_date      TEXT,
    p1_raw_name     TEXT NOT NULL,
    p2_raw_name     TEXT NOT NULL,
    p1_score        TEXT,
    p2_score        TEXT,
    p1_180s         INTEGER,
    p2_180s         INTEGER,
    format          TEXT,
    source_url      TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | RESOLVED | FAILED
    failure_reason  TEXT,
    scraped_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_staging(conn: sqlite3.Connection) -> None:
    conn.executescript(STAGING_SCHEMA)


# ─────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; JOB006-research-bot/1.0; "
        "+https://github.com/job006)"
    )
}


def fetch(url: str, delay: float = 1.5) -> Optional[str]:
    """Fetch URL with polite delay. Returns HTML string or None."""
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"[scraper] HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"[scraper] Error fetching {url}: {e}")
        return None


# ─────────────────────────────────────────────
# Format detection
# ─────────────────────────────────────────────

def detect_format(tournament_name: str, round_: str) -> str:
    """
    Derive match format from tournament name and round.
    DartsDatabase doesn't always state format explicitly.
    """
    name = tournament_name.lower()
    round_lower = round_.lower()

    # World Championship
    if "world" in name:
        if any(r in round_lower for r in ["final", "semi"]):
            return "BO13"
        if "quarter" in round_lower:
            return "BO11"
        return "BO9"

    # Premier League
    if "premier" in name:
        return "BO11"

    # Grand Slam
    if "grand slam" in name:
        if any(r in round_lower for r in ["final", "semi"]):
            return "BO13"
        return "BO11"

    # Matchplay / Majors
    if any(t in name for t in ["matchplay", "masters", "uk open", "european"]):
        if any(r in round_lower for r in ["final", "semi"]):
            return "BO11"
        return "BO9"

    # Pro Tour / Players Championship (shorter formats)
    if any(t in name for t in ["players", "pro tour", "open"]):
        return "BO7"

    return "UNKNOWN"


# ─────────────────────────────────────────────
# HTML parser for DartsDatabase result pages
# ─────────────────────────────────────────────

class DartsDatabaseParser(HTMLParser):
    """
    Parse DartsDatabase tournament result pages.
    Extracts: player names, scores, 180s.
    """

    def __init__(self):
        super().__init__()
        self.matches = []
        self._current = {}
        self._in_match_row = False
        self._col = 0
        self._data_buffer = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if tag == "tr" and "match" in cls:
            self._in_match_row = True
            self._current = {}
            self._col = 0

        if self._in_match_row and tag == "td":
            self._col += 1
            self._data_buffer = ""

    def handle_data(self, data):
        if self._in_match_row:
            self._data_buffer += data.strip()

    def handle_endtag(self, tag):
        if not self._in_match_row:
            return

        if tag == "td":
            data = self._data_buffer.strip()
            col = self._col

            # Column mapping (approximate — varies by page version):
            # 1: round, 2: p1 name, 3: p1 score, 4: p2 score, 5: p2 name,
            # 6: p1 180s, 7: p2 180s (not always present)
            if col == 1:
                self._current["round"] = data
            elif col == 2:
                self._current["p1_raw_name"] = data
            elif col == 3:
                self._current["p1_score"] = data
            elif col == 4:
                self._current["p2_score"] = data
            elif col == 5:
                self._current["p2_raw_name"] = data
            elif col == 6:
                self._current["p1_180s"] = self._parse_int(data)
            elif col == 7:
                self._current["p2_180s"] = self._parse_int(data)

        if tag == "tr" and self._in_match_row:
            if self._current.get("p1_raw_name") and self._current.get("p2_raw_name"):
                self.matches.append(dict(self._current))
            self._in_match_row = False
            self._current = {}
            self._col = 0

    def _parse_int(self, s: str) -> Optional[int]:
        try:
            return int(re.sub(r"[^\d]", "", s))
        except (ValueError, TypeError):
            return None


# ─────────────────────────────────────────────
# Main scraper
# ─────────────────────────────────────────────

class DartsDatabaseScraper:
    """
    Scrape historical PDC match results from dartsdatabase.co.uk.

    Usage:
        scraper = DartsDatabaseScraper()
        scraper.scrape_tournament("https://dartsdatabase.co.uk/...", "PDC World Championship", 2024)
        scraper.promote_to_matches()
    """

    BASE_URL = "https://www.dartsdatabase.co.uk"

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.resolver = Resolver(db_path)
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with get_conn(self.db_path) as conn:
            ensure_staging(conn)

    def scrape_tournament(
        self,
        url: str,
        tournament_name: str,
        year: int,
        match_date: Optional[str] = None,
    ) -> dict:
        """
        Scrape a single tournament results page.
        Returns summary: {scraped: N, inserted: N, failed: N}
        """
        print(f"\n[darts-scraper] Scraping: {tournament_name} {year}")
        print(f"[darts-scraper] URL: {url}")

        html = fetch(url)
        if html is None:
            print(f"[darts-scraper] BLOCKED: could not fetch {url}")
            return {"scraped": 0, "inserted": 0, "failed": 0, "status": "BLOCKED"}

        parser = DartsDatabaseParser()
        try:
            parser.feed(html)
        except Exception as e:
            print(f"[darts-scraper] Parse error: {e}")
            return {"scraped": 0, "inserted": 0, "failed": 0, "status": "PARSE_ERROR"}

        raw_matches = parser.matches
        print(f"[darts-scraper] Parsed {len(raw_matches)} raw match rows")

        inserted = 0
        failed = 0

        with get_conn(self.db_path) as conn:
            for m in raw_matches:
                p1 = m.get("p1_raw_name", "").strip()
                p2 = m.get("p2_raw_name", "").strip()

                if not p1 or not p2:
                    failed += 1
                    continue

                round_ = m.get("round", "UNKNOWN")
                fmt = detect_format(tournament_name, round_)

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO staging_darts
                           (tournament_name, tournament_year, round, match_date,
                            p1_raw_name, p2_raw_name,
                            p1_score, p2_score,
                            p1_180s, p2_180s,
                            format, source_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tournament_name, year, round_,
                            match_date or f"{year}-01-01",
                            p1, p2,
                            m.get("p1_score"), m.get("p2_score"),
                            m.get("p1_180s"), m.get("p2_180s"),
                            fmt, url,
                        ),
                    )
                    inserted += 1
                except Exception as e:
                    print(f"[darts-scraper] Insert error: {e} | {p1} vs {p2}")
                    failed += 1

        print(f"[darts-scraper] Staged: {inserted} | Failed: {failed}")
        return {
            "scraped": len(raw_matches),
            "inserted": inserted,
            "failed": failed,
            "status": "OK",
        }

    def promote_to_matches(self) -> dict:
        """
        Resolve player names and promote staged rows to matches table.
        Only promotes rows where BOTH players resolve successfully.
        Reports counts honestly.
        """
        backup("promote_darts")
        print("\n[darts-scraper] Promoting staged rows to matches...")

        with get_conn(self.db_path) as conn:
            pending = conn.execute(
                "SELECT * FROM staging_darts WHERE status='PENDING'"
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
                print(f"[resolver] QUEUED: {e}")
                queued += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_darts SET status='QUEUED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue
            except ResolutionFailed as e:
                print(f"[resolver] FAILED: {e}")
                failed += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_darts SET status='FAILED', failure_reason=? WHERE staging_id=?",
                        (str(e), row["staging_id"])
                    )
                continue

            # Ensure tournament exists
            tournament_id = f"PDC-{row['tournament_year']}-{row['tournament_name'][:20].upper().replace(' ', '_')}"
            with get_conn(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO tournaments
                       (tournament_id, sport, tour, name, year)
                       VALUES (?, 'darts', 'PDC', ?, ?)""",
                    (tournament_id, row["tournament_name"], row["tournament_year"])
                )

                # Generate match_id
                match_id = hashlib.sha256(
                    f"{tournament_id}|{row['round']}|{p1_id}|{p2_id}|{row['match_date']}".encode()
                ).hexdigest()[:16]

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO matches
                           (match_id, tournament_id, sport, round, match_date,
                            player1_id, player2_id,
                            format,
                            p1_180s, p2_180s,
                            data_source, source_url)
                           VALUES (?, ?, 'darts', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            match_id, tournament_id,
                            row["round"], row["match_date"],
                            p1_id, p2_id,
                            row["format"],
                            row["p1_180s"], row["p2_180s"],
                            SOURCE, row["source_url"],
                        )
                    )
                    conn.execute(
                        "UPDATE staging_darts SET status='RESOLVED' WHERE staging_id=?",
                        (row["staging_id"],)
                    )
                    promoted += 1
                except Exception as e:
                    print(f"[scraper] Match insert error: {e}")
                    failed += 1

        print(
            f"[darts-scraper] Promoted: {promoted} | "
            f"Queued (needs review): {queued} | "
            f"Failed: {failed}"
        )
        return {"promoted": promoted, "queued": queued, "failed": failed}

    def staging_summary(self) -> dict:
        """Return current staging table counts."""
        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM staging_darts GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}
