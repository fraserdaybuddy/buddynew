"""
cuetrackeR.py — Snooker historical data scraper
JOB-006 Sports Betting Model

Source: cuetracker.net
URL pattern: https://cuetracker.net/tournaments/{slug}/{year}/{id}

Data per match:
  - Player names, frame scores, centuries per player (counted from 50+ Breaks list)
  - Date, round, format (derived from best-of span)

Page is server-rendered — no Crawl4AI needed. Uses urllib + regex.
"""

import re
import hashlib
import sqlite3
import time
import sys
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database import get_conn, DB_PATH, backup
from resolver import Resolver, ResolutionFailed, ResolutionQueued


TOUR   = "WST"
SOURCE = "cuetrackeR"
BASE   = "https://cuetracker.net"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JOB006-research-bot/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_snooker (
    staging_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_name TEXT,
    tournament_year INTEGER,
    round           TEXT,
    match_date      TEXT,
    p1_raw_name     TEXT NOT NULL,
    p2_raw_name     TEXT NOT NULL,
    p1_frames       INTEGER,
    p2_frames       INTEGER,
    p1_centuries    INTEGER,
    p2_centuries    INTEGER,
    format          TEXT,
    source_url      TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    failure_reason  TEXT,
    scraped_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ─────────────────────────────────────────────
# Tournament registry — real slugs + IDs from cuetracker.net
# ─────────────────────────────────────────────

MAJOR_TOURNAMENTS = [
    # 2024/25 season — IDs confirmed from cuetracker.net/seasons/2024-2025
    {"name": "World Championship",   "year": 2025, "slug": "world-championship",   "id": 6743},
    {"name": "Tour Championship",    "year": 2025, "slug": "tour-championship",    "id": 6724},
    {"name": "Players Championship", "year": 2025, "slug": "players-championship", "id": 6679},
    {"name": "World Grand Prix",     "year": 2025, "slug": "world-grand-prix",     "id": 6667},
    {"name": "Welsh Open",           "year": 2025, "slug": "welsh-open",           "id": 6606},
    {"name": "Masters",              "year": 2025, "slug": "masters",              "id": 6578},
    {"name": "German Masters",       "year": 2025, "slug": "german-masters",       "id": 6442},
    {"name": "UK Championship",      "year": 2024, "slug": "uk-championship",      "id": 6400},
    {"name": "Scottish Open",        "year": 2024, "slug": "scottish-open",        "id": 6384},
    {"name": "English Open",         "year": 2024, "slug": "english-open",         "id": 6289},
    {"name": "Northern Ireland Open","year": 2024, "slug": "northern-ireland-open","id": 6321},
    # 2023/24 season — IDs confirmed from cuetracker.net/seasons/2023-2024
    {"name": "World Championship",   "year": 2024, "slug": "world-championship",   "id": 6112},
    {"name": "Tour Championship",    "year": 2024, "slug": "tour-championship",    "id": 6083},
    {"name": "Players Championship", "year": 2024, "slug": "players-championship", "id": 6036},
    {"name": "Welsh Open",           "year": 2024, "slug": "welsh-open",           "id": 5995},
    {"name": "Masters",              "year": 2024, "slug": "masters",              "id": 5965},
    {"name": "World Grand Prix",     "year": 2024, "slug": "world-grand-prix",     "id": 5979},
    {"name": "German Masters",       "year": 2024, "slug": "german-masters",       "id": 5950},
    {"name": "UK Championship",      "year": 2023, "slug": "uk-championship",      "id": 5861},
    # 2022/23 season — IDs confirmed from cuetracker.net/seasons/2022-2023
    {"name": "World Championship",   "year": 2023, "slug": "world-championship",   "id": 5550},
    {"name": "Tour Championship",    "year": 2023, "slug": "tour-championship",    "id": 5549},
    {"name": "Players Championship", "year": 2023, "slug": "players-championship", "id": 5525},
    {"name": "Welsh Open",           "year": 2023, "slug": "welsh-open",           "id": 5468},
    {"name": "Masters",              "year": 2023, "slug": "masters",              "id": 5429},
    {"name": "German Masters",       "year": 2023, "slug": "german-masters",       "id": 5400},
    {"name": "UK Championship",      "year": 2022, "slug": "uk-championship",      "id": 5357},
]


# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────

def fetch(url: str, delay: float = 1.5) -> Optional[str]:
    time.sleep(delay)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        print(f"[snooker] HTTP {e.code}: {url}")
        return None
    except Exception as e:
        print(f"[snooker] Error fetching {url}: {e}")
        return None


# ─────────────────────────────────────────────
# HTML parsing helpers
# ─────────────────────────────────────────────

def _text(pattern: str, html: str, default: str = "") -> str:
    m = re.search(pattern, html, re.DOTALL)
    return m.group(1).strip() if m else default


def _int(pattern: str, html: str) -> Optional[int]:
    m = re.search(pattern, html, re.DOTALL)
    if m:
        try:
            return int(m.group(1).strip())
        except ValueError:
            pass
    return None


def _count_centuries(breaks_str: str) -> int:
    """Count breaks >= 100 from a comma-separated breaks string."""
    if not breaks_str.strip():
        return 0
    count = 0
    for part in breaks_str.split(","):
        part = part.strip()
        if part.isdigit() and int(part) >= 100:
            count += 1
    return count


def _parse_format(best_of_text: str) -> str:
    """Convert best-of span text e.g. '(35)' → 'BO35'."""
    m = re.search(r'\((\d+)\)', best_of_text)
    if m:
        return f"BO{m.group(1)}"
    return "UNKNOWN"


def parse_tournament_page(html: str, tournament_url: str) -> list[dict]:
    """
    Parse all match blocks from a CueTracker tournament page.

    Returns list of dicts with keys:
        match_id, round, p1_name, p2_name,
        p1_frames, p2_frames, p1_centuries, p2_centuries,
        format, match_date, source_url
    """
    # Split page into individual match blocks
    # Each match block starts with: <div class="match row even/odd" data-match-id="NNN"
    blocks = re.split(r'<div class="match row (?:even|odd)"', html)

    results = []
    current_round = "UNKNOWN"

    for block in blocks[1:]:   # skip preamble before first match
        # Extract match ID from data attribute
        mid_m = re.match(r'\s+data-match-id="(\d+)"', block)
        if not mid_m:
            continue
        ct_match_id = mid_m.group(1)

        # Round name — may appear inside this block or was set by previous heading
        round_m = re.search(r'class="col-md-12 round_name"[^>]*>.*?<h5>(.*?)</h5>', block, re.DOTALL)
        if round_m:
            current_round = round_m.group(1).strip()
        round_name = current_round

        # Player names — from anchor tags inside player_1_name / player_2_name divs
        p1_m = re.search(r'class="player_1_name matchResultText[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        p2_m = re.search(r'class="player_2_name matchResultText[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        if not p1_m or not p2_m:
            continue  # can't use this match without player names

        p1_name = re.sub(r'<[^>]+>', '', p1_m.group(1)).strip()
        p2_name = re.sub(r'<[^>]+>', '', p2_m.group(1)).strip()

        if not p1_name or not p2_name:
            continue

        # Frame scores
        p1_frames = _int(r'class="[^"]*player_1_score[^"]*"[^>]*>\s*<b>\s*(\d+)', block)
        p2_frames = _int(r'class="[^"]*player_2_score[^"]*">\s*(\d+)', block)

        # Best-of / format
        best_of_m = re.search(r'class="best_of text-nowrap">\s*(\(\d+\))', block)
        fmt = _parse_format(best_of_m.group(1)) if best_of_m else "UNKNOWN"

        # Date — take first date from "YYYY-MM-DD" or "YYYY-MM-DD - MM-DD" pattern
        date_m = re.search(r'class="[^"]*played_on[^"]*"[^>]*>\s*(\d{4}-\d{2}-\d{2})', block, re.DOTALL)
        match_date = date_m.group(1) if date_m else None

        # Centuries — from "50+ Breaks" section
        # Structure: matchPropertyAlign with text "50+ Breaks" then 3× col-4 divs: p1, p2, total
        p1_centuries = None
        p2_centuries = None
        breaks_m = re.search(
            r'50\+ Breaks.*?<div class="col-4">(.*?)</div>\s*<div class="col-4">(.*?)</div>',
            block, re.DOTALL
        )
        if breaks_m:
            p1_centuries = _count_centuries(breaks_m.group(1))
            p2_centuries = _count_centuries(breaks_m.group(2))

        results.append({
            "ct_match_id":   ct_match_id,
            "round":         round_name,
            "p1_name":       p1_name,
            "p2_name":       p2_name,
            "p1_frames":     p1_frames,
            "p2_frames":     p2_frames,
            "p1_centuries":  p1_centuries,
            "p2_centuries":  p2_centuries,
            "format":        fmt,
            "match_date":    match_date,
            "source_url":    tournament_url,
        })

    return results


# ─────────────────────────────────────────────
# Format helpers
# ─────────────────────────────────────────────

def _normalise_round(raw: str) -> str:
    """Map CueTracker round names to standard codes."""
    r = raw.lower().strip()
    if "final" in r and "semi" not in r and "quarter" not in r:
        return "F"
    if "semi" in r:
        return "SF"
    if "quarter" in r:
        return "QF"
    if "last 16" in r or "round of 16" in r:
        return "R16"
    if "last 32" in r or "round of 32" in r:
        return "R32"
    if "last 64" in r or "round of 64" in r:
        return "R64"
    if "round 1" in r or "first round" in r:
        return "R1"
    if "round 2" in r or "second round" in r:
        return "R2"
    if "round 3" in r or "third round" in r:
        return "R3"
    if "group" in r or "phase" in r:
        return "GS"
    return raw[:20].upper().replace(" ", "_")


# ─────────────────────────────────────────────
# Main scraper class
# ─────────────────────────────────────────────

class CuetrackerScraper:

    def __init__(self, db_path: Path = DB_PATH, delay: float = 1.5):
        self.db_path = db_path
        self.delay   = delay
        self.resolver = Resolver(db_path)
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with get_conn(self.db_path) as conn:
            conn.executescript(STAGING_SCHEMA)

    def scrape_tournament(
        self,
        slug: str,
        year: int,
        tournament_id: int,
        tournament_name: str,
    ) -> dict:
        url = f"{BASE}/tournaments/{slug}/{year}/{tournament_id}"
        print(f"\n[snooker] Scraping: {tournament_name} {year}")
        print(f"[snooker] URL: {url}")

        html = fetch(url, delay=self.delay)
        if html is None:
            print(f"[snooker] BLOCKED: {url}")
            return {"status": "BLOCKED", "scraped": 0, "inserted": 0, "failed": 0}

        matches = parse_tournament_page(html, url)
        print(f"[snooker] Found {len(matches)} matches")

        inserted = 0
        failed   = 0

        with get_conn(self.db_path) as conn:
            for m in matches:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO staging_snooker
                           (tournament_name, tournament_year, round, match_date,
                            p1_raw_name, p2_raw_name,
                            p1_frames, p2_frames,
                            p1_centuries, p2_centuries,
                            format, source_url)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tournament_name,
                            year,
                            _normalise_round(m["round"]),
                            m["match_date"] or f"{year}-01-01",
                            m["p1_name"],
                            m["p2_name"],
                            m["p1_frames"],
                            m["p2_frames"],
                            m["p1_centuries"],
                            m["p2_centuries"],
                            m["format"],
                            m["source_url"],
                        ),
                    )
                    inserted += 1
                except Exception as e:
                    print(f"[snooker] Insert error: {e} | {m['p1_name']} v {m['p2_name']}")
                    failed += 1

        print(f"[snooker] Done. Staged: {inserted} | Failed: {failed}")
        return {
            "status":   "OK",
            "scraped":  len(matches),
            "inserted": inserted,
            "failed":   failed,
        }

    def promote_to_matches(self) -> dict:
        """Resolve player names and promote staged rows to matches table."""
        backup("promote_snooker")
        print("\n[snooker] Promoting staged rows to matches...")

        with get_conn(self.db_path) as conn:
            pending = conn.execute(
                "SELECT * FROM staging_snooker WHERE status='PENDING'"
            ).fetchall()

        promoted = 0
        queued   = 0
        failed   = 0

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
                            row.get("p1_centuries"),
                            row.get("p2_centuries"),
                            SOURCE,
                            row.get("source_url"),
                        )
                    )
                    conn.execute(
                        "UPDATE staging_snooker SET status='RESOLVED' WHERE staging_id=?",
                        (row["staging_id"],)
                    )
                    promoted += 1
                except Exception as e:
                    print(f"[snooker] Match insert error: {e}")
                    failed += 1

        print(f"[snooker] Promoted: {promoted} | Queued: {queued} | Failed: {failed}")
        return {"promoted": promoted, "queued": queued, "failed": failed}

    def staging_summary(self) -> dict:
        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM staging_snooker GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    scraper = CuetrackerScraper(delay=1.5)
    for t in MAJOR_TOURNAMENTS:
        result = scraper.scrape_tournament(
            slug=t["slug"],
            year=t["year"],
            tournament_id=t["id"],
            tournament_name=t["name"],
        )
        print(f"[snooker] {t['name']} {t['year']}: {result}")

    scraper.promote_to_matches()
    print("\n[snooker] Staging summary:", scraper.staging_summary())
