"""
scrapers/darts/darts24.py — PRIMARY darts data scraper
JOB-006 Sports Betting Model

Source: darts24.com (FlashScore network)
Provides per match: 3-dart avg, 180s, 140+, 100+, checkouts %, highest checkout.

Data flow:
  1. Tournament results page → list of match URLs + mid values
  2. Per match: /match/{p1-slug}/{p2-slug}/summary/stats/?mid={id}
  3. Parse stats → staging_darts → promote to matches

RULES:
  - Never fabricate data. If a page is unavailable, report BLOCKED.
  - Write to staging table only. Validator promotes to matches.
  - All player names go through Resolver before any match is inserted.
  - NULL policy: if a stat is absent from the page, store NULL — never 0, never estimated.
"""

import re
import hashlib
import sqlite3
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from database import get_conn, DB_PATH, backup
from resolver import Resolver, ResolutionFailed, ResolutionQueued
from config import get_sport

import os
os.environ['PYTHONUTF8'] = '1'
os.environ['NO_COLOR'] = '1'

import io as _io
sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

TOUR = "PDC"
SOURCE = "darts24"
BASE_URL = "https://www.darts24.com"

# ─────────────────────────────────────────────
# Staging schema (extended for darts24 stats)
# ─────────────────────────────────────────────

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_darts (
    staging_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_name     TEXT,
    tournament_year     INTEGER,
    round               TEXT,
    match_date          TEXT,
    p1_raw_name         TEXT NOT NULL,
    p2_raw_name         TEXT NOT NULL,
    p1_score            TEXT,
    p2_score            TEXT,
    p1_180s             INTEGER,
    p2_180s             INTEGER,
    p1_avg              REAL,
    p2_avg              REAL,
    p1_140plus          INTEGER,
    p2_140plus          INTEGER,
    p1_100plus          INTEGER,
    p2_100plus          INTEGER,
    p1_checkout_pct     REAL,
    p2_checkout_pct     REAL,
    p1_checkout_hits    INTEGER,
    p2_checkout_hits    INTEGER,
    p1_checkout_att     INTEGER,
    p2_checkout_att     INTEGER,
    p1_highest_checkout INTEGER,
    p2_highest_checkout INTEGER,
    format              TEXT,
    source_url          TEXT,
    match_id_darts24    TEXT,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    failure_reason      TEXT,
    scraped_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_staging(conn: sqlite3.Connection) -> None:
    conn.executescript(STAGING_SCHEMA)


# ─────────────────────────────────────────────
# Crawl4AI fetch helper
# ─────────────────────────────────────────────

async def _fetch_rendered(url: str, wait_ms: int = 3000) -> Optional[str]:
    """Fetch a JS-rendered page. Returns HTML string or None."""
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            config = CrawlerRunConfig(
                wait_until='networkidle',
                page_timeout=20000,
                js_code=f"await new Promise(r => setTimeout(r, {wait_ms}));"
            )
            result = await crawler.arun(url, config=config)
            return result.html if result.success else None
    except Exception as e:
        print(f"[darts24] Fetch error: {e}")
        return None


def fetch_rendered(url: str, wait_ms: int = 3000) -> Optional[str]:
    return asyncio.run(_fetch_rendered(url, wait_ms))


# ─────────────────────────────────────────────
# Format detection
# ─────────────────────────────────────────────

def detect_format(tournament_name: str, round_: str) -> str:
    name = tournament_name.lower()
    r = round_.lower()

    if 'world championship' in name:
        if 'final' == r:      return 'BO13'
        if 'semi' in r:        return 'BO11'
        if 'quarter' in r:     return 'BO11'
        return 'BO9'
    if 'premier league' in name:
        return 'BO11'
    if 'grand slam' in name:
        if 'final' in r or 'semi' in r: return 'BO13'
        return 'BO11'
    if any(t in name for t in ['matchplay', 'world matchplay']):
        if 'final' in r or 'semi' in r: return 'BO11'
        return 'BO9'
    if 'world grand prix' in name:
        if 'final' in r:       return 'BO11'
        return 'BO9'
    if 'uk open' in name:
        if 'final' in r:       return 'BO11'
        return 'BO7'
    if any(t in name for t in ['players championship finals', 'european championship']):
        if 'final' in r or 'semi' in r: return 'BO11'
        return 'BO9'
    return 'UNKNOWN'


# ─────────────────────────────────────────────
# HTML parsers
# ─────────────────────────────────────────────

def _text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()


def slug_to_name(slug: str) -> str:
    """Convert darts24 slug to display name. e.g. 'littler-luke-0URN6Xks' → 'Luke Littler'"""
    parts = slug.split('-')
    # Drop trailing unique ID (mixed case alphanumeric 6+ chars)
    if parts and re.match(r'^[A-Za-z0-9]{6,}$', parts[-1]) and any(c.isupper() for c in parts[-1]):
        parts = parts[:-1]
    # Slug is surname-firstname, so reverse
    if len(parts) >= 2:
        return ' '.join(p.capitalize() for p in reversed(parts))
    return ' '.join(p.capitalize() for p in parts)


def parse_match_list(html: str) -> list[dict]:
    """
    Parse tournament results page using BeautifulSoup.
    Returns list of: {p1_name, p2_name, p1_score, p2_score, round, match_url, mid}
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    matches = []
    current_round = "UNKNOWN"
    seen_mids = set()

    # Walk all elements — round headers and match rows are siblings
    for el in soup.find_all(True):
        # Round header
        if el.get('class') and any('event__round' in c for c in el.get('class', [])):
            current_round = el.get_text(strip=True)
            continue

        # Match row — has an <a> with href containing ?mid=
        if el.get('data-event-row'):
            link = el.find('a', href=re.compile(r'\?mid='))
            if not link:
                continue

            href = link['href']
            mid_m = re.search(r'\?mid=([^&"]+)', href)
            if not mid_m:
                continue
            mid = mid_m.group(1)
            if mid in seen_mids:
                continue
            seen_mids.add(mid)

            # Extract player slugs from URL
            slug_m = re.search(r'/match/([^/]+)/([^/]+)/\?', href)
            if not slug_m:
                continue

            p1_slug = slug_m.group(1)
            p2_slug = slug_m.group(2)

            # Player names — use --home and --away variants
            p1_el = el.find(class_=re.compile(r'event__participant--home'))
            p2_el = el.find(class_=re.compile(r'event__participant--away'))
            p1_name = p1_el.get_text(strip=True) if p1_el else slug_to_name(p1_slug)
            p2_name = p2_el.get_text(strip=True) if p2_el else slug_to_name(p2_slug)

            # Scores — event__part--home/away (not event__participant)
            s1_el = el.find(class_=re.compile(r'event__part--home'))
            s2_el = el.find(class_=re.compile(r'event__part--away'))
            p1_score = s1_el.get_text(strip=True) if s1_el else None
            p2_score = s2_el.get_text(strip=True) if s2_el else None

            matches.append({
                'p1_raw_name': p1_name,
                'p2_raw_name': p2_name,
                'p1_score': p1_score,
                'p2_score': p2_score,
                'round': current_round,
                'match_url': href if href.startswith('http') else BASE_URL + href,
                'mid': mid,
            })

    return matches


def parse_match_stats(html: str) -> dict:
    """
    Parse match stats page using BeautifulSoup.
    Returns dict with p1/p2 stats. All values Optional.

    Stat row structure (darts24/flashscore):
      <div data-testid="wcl-statistics">
        <div data-testid="wcl-statistics-value"> HOME_VALUE </div>
        <div data-testid="wcl-statistics-category"> STAT_NAME </div>
        <div data-testid="wcl-statistics-value"> AWAY_VALUE </div>
      </div>
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    stats = {}

    for row in soup.find_all(attrs={'data-testid': 'wcl-statistics'}):
        values = row.find_all(attrs={'data-testid': 'wcl-statistics-value'})
        cats = row.find_all(attrs={'data-testid': 'wcl-statistics-category'})
        if not cats or len(values) < 2:
            continue
        cat = cats[0].get_text(strip=True)
        home_val = values[0].get_text(strip=True)
        away_val = values[1].get_text(strip=True)
        stats[cat] = {'home': home_val, 'away': away_val}

    if not stats:
        return {}

    def _float(v: str) -> Optional[float]:
        try:
            return float(re.sub(r'[^\d.]', '', v))
        except (ValueError, TypeError):
            return None

    def _int(v: str) -> Optional[int]:
        try:
            return int(re.sub(r'[^\d]', '', v))
        except (ValueError, TypeError):
            return None

    def _checkout(v: str) -> tuple[Optional[float], Optional[int], Optional[int]]:
        """Parse '35%(7/20)' → (35.0, 7, 20)"""
        m = re.match(r'([\d.]+)%\s*\((\d+)/(\d+)\)', v.strip())
        if m:
            return float(m.group(1)), int(m.group(2)), int(m.group(3))
        pct_m = re.match(r'([\d.]+)%', v.strip())
        if pct_m:
            return float(pct_m.group(1)), None, None
        return None, None, None

    result = {}

    avg = stats.get('Average (3 darts)', {})
    result['p1_avg'] = _float(avg.get('home', ''))
    result['p2_avg'] = _float(avg.get('away', ''))

    s180 = stats.get('180 thrown', {})
    result['p1_180s'] = _int(s180.get('home', ''))
    result['p2_180s'] = _int(s180.get('away', ''))

    s140 = stats.get('140+ thrown', {})
    result['p1_140plus'] = _int(s140.get('home', ''))
    result['p2_140plus'] = _int(s140.get('away', ''))

    s100 = stats.get('100+ thrown', {})
    result['p1_100plus'] = _int(s100.get('home', ''))
    result['p2_100plus'] = _int(s100.get('away', ''))

    co = stats.get('Checkouts', {})
    pct1, hits1, att1 = _checkout(co.get('home', ''))
    pct2, hits2, att2 = _checkout(co.get('away', ''))
    result['p1_checkout_pct'] = pct1
    result['p2_checkout_pct'] = pct2
    result['p1_checkout_hits'] = hits1
    result['p2_checkout_hits'] = hits2
    result['p1_checkout_att'] = att1
    result['p2_checkout_att'] = att2

    hco = stats.get('Highest checkout', {})
    result['p1_highest_checkout'] = _int(hco.get('home', ''))
    result['p2_highest_checkout'] = _int(hco.get('away', ''))

    return result


# ─────────────────────────────────────────────
# Main scraper class
# ─────────────────────────────────────────────

class Darts24Scraper:
    """
    Scrape PDC major tournament match stats from darts24.com.

    Usage:
        scraper = Darts24Scraper()
        scraper.scrape_tournament(
            results_url='https://www.darts24.com/world/pdc-world-championship/results/',
            tournament_name='PDC World Championship',
            year=2025
        )
        scraper.promote_to_matches()
    """

    def __init__(self, db_path: Path = DB_PATH, delay: float = 2.0):
        self.db_path = db_path
        self.resolver = Resolver(db_path)
        self.delay = delay
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with get_conn(self.db_path) as conn:
            ensure_staging(conn)

    def scrape_tournament(
        self,
        results_url: str,
        tournament_name: str,
        year: int,
    ) -> dict:
        """
        Scrape all matches from a tournament results page.
        For each match, fetches the stats sub-page.
        Returns summary: {scraped, inserted, failed, blocked}
        """
        print(f"\n[darts24] Scraping: {tournament_name} {year}")
        print(f"[darts24] URL: {results_url}")

        # Step 1: get match list
        html = fetch_rendered(results_url, wait_ms=3000)
        if html is None:
            print(f"[darts24] BLOCKED: could not fetch {results_url}")
            return {'scraped': 0, 'inserted': 0, 'failed': 0, 'blocked': 1}

        matches = parse_match_list(html)
        print(f"[darts24] Found {len(matches)} matches on results page")

        if not matches:
            print(f"[darts24] WARNING: zero matches parsed — check URL or page structure")
            return {'scraped': 0, 'inserted': 0, 'failed': 0, 'blocked': 0}

        inserted = 0
        failed = 0
        blocked = 0

        for i, m in enumerate(matches):
            print(f"[darts24] Match {i+1}/{len(matches)}: {m['p1_raw_name']} vs {m['p2_raw_name']}", end=' ')

            # Step 2: fetch stats for this match
            stats_url = (
                m['match_url'].rstrip('/')
                .replace('/?mid=', '/summary/stats/?mid=')
            )
            # Polite delay
            time.sleep(self.delay)

            stats_html = fetch_rendered(stats_url, wait_ms=3000)
            if stats_html is None:
                print(f"— BLOCKED")
                blocked += 1
                stats = {}
            else:
                stats = parse_match_stats(stats_html)
                print(f"— avg={stats.get('p1_avg')}/{stats.get('p2_avg')} 180s={stats.get('p1_180s')}/{stats.get('p2_180s')}")

            fmt = detect_format(tournament_name, m['round'])

            try:
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO staging_darts
                           (tournament_name, tournament_year, round, match_date,
                            p1_raw_name, p2_raw_name,
                            p1_score, p2_score,
                            p1_180s, p2_180s,
                            p1_avg, p2_avg,
                            p1_140plus, p2_140plus,
                            p1_100plus, p2_100plus,
                            p1_checkout_pct, p2_checkout_pct,
                            p1_checkout_hits, p2_checkout_hits,
                            p1_checkout_att, p2_checkout_att,
                            p1_highest_checkout, p2_highest_checkout,
                            format, source_url, match_id_darts24)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            tournament_name, year, m['round'],
                            datetime.now().strftime('%Y-%m-%d'),
                            m['p1_raw_name'], m['p2_raw_name'],
                            m.get('p1_score'), m.get('p2_score'),
                            stats.get('p1_180s'), stats.get('p2_180s'),
                            stats.get('p1_avg'), stats.get('p2_avg'),
                            stats.get('p1_140plus'), stats.get('p2_140plus'),
                            stats.get('p1_100plus'), stats.get('p2_100plus'),
                            stats.get('p1_checkout_pct'), stats.get('p2_checkout_pct'),
                            stats.get('p1_checkout_hits'), stats.get('p2_checkout_hits'),
                            stats.get('p1_checkout_att'), stats.get('p2_checkout_att'),
                            stats.get('p1_highest_checkout'), stats.get('p2_highest_checkout'),
                            fmt, stats_url, m['mid'],
                        )
                    )
                inserted += 1
            except Exception as e:
                print(f"[darts24] Insert error: {e}")
                failed += 1

        print(f"\n[darts24] Done. Staged: {inserted} | Blocked: {blocked} | Failed: {failed}")
        return {'scraped': len(matches), 'inserted': inserted, 'failed': failed, 'blocked': blocked}

    def promote_to_matches(self) -> dict:
        """Resolve player names and promote staged rows to matches table."""
        backup("promote_darts24")
        print("\n[darts24] Promoting staged rows to matches...")

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
                    row['p1_raw_name'], TOUR, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
                p2_id = self.resolver.resolve(
                    row['p2_raw_name'], TOUR, SOURCE,
                    context=f"{row['tournament_name']} {row['tournament_year']} {row['round']}"
                )
            except ResolutionQueued as e:
                queued += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_darts SET status='QUEUED', failure_reason=? WHERE staging_id=?",
                        (str(e), row['staging_id'])
                    )
                continue
            except ResolutionFailed as e:
                failed += 1
                with get_conn(self.db_path) as conn:
                    conn.execute(
                        "UPDATE staging_darts SET status='FAILED', failure_reason=? WHERE staging_id=?",
                        (str(e), row['staging_id'])
                    )
                continue

            tournament_id = f"PDC-{row['tournament_year']}-{row['tournament_name'][:20].upper().replace(' ', '_')}"
            with get_conn(self.db_path) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO tournaments
                       (tournament_id, sport, tour, name, year)
                       VALUES (?, 'darts', 'PDC', ?, ?)""",
                    (tournament_id, row['tournament_name'], row['tournament_year'])
                )

                match_id = hashlib.sha256(
                    f"{tournament_id}|{row['round']}|{p1_id}|{p2_id}|{row['match_date']}".encode()
                ).hexdigest()[:16]

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO matches
                           (match_id, tournament_id, sport, round, match_date,
                            player1_id, player2_id, format,
                            p1_180s, p2_180s,
                            data_source, source_url)
                           VALUES (?, ?, 'darts', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            match_id, tournament_id,
                            row['round'], row['match_date'],
                            p1_id, p2_id,
                            row['format'],
                            row.get('p1_180s'), row.get('p2_180s'),
                            SOURCE, row['source_url'],
                        )
                    )
                    conn.execute(
                        "UPDATE staging_darts SET status='RESOLVED' WHERE staging_id=?",
                        (row['staging_id'],)
                    )
                    promoted += 1
                except Exception as e:
                    print(f"[darts24] Match insert error: {e}")
                    failed += 1

        print(f"[darts24] Promoted: {promoted} | Queued: {queued} | Failed: {failed}")
        return {'promoted': promoted, 'queued': queued, 'failed': failed}

    def staging_summary(self) -> dict:
        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM staging_darts GROUP BY status"
            ).fetchall()
            return {r['status']: r['n'] for r in rows}


# ─────────────────────────────────────────────
# Tournament URL map — 2 years of majors
# ─────────────────────────────────────────────

MAJOR_TOURNAMENTS = [
    # 2025 season
    {'name': 'PDC World Championship', 'year': 2025,
     'url': 'https://www.darts24.com/world/pdc-world-championship/2025/results/'},
    {'name': 'PDC Premier League', 'year': 2025,
     'url': 'https://www.darts24.com/world/premier-league/2025/results/'},
    {'name': 'PDC UK Open', 'year': 2025,
     'url': 'https://www.darts24.com/united-kingdom/uk-open/2025/results/'},
    {'name': 'PDC World Matchplay', 'year': 2025,
     'url': 'https://www.darts24.com/world/world-matchplay/2025/results/'},
    {'name': 'PDC World Grand Prix', 'year': 2025,
     'url': 'https://www.darts24.com/world/world-grand-prix/2025/results/'},
    {'name': 'PDC European Championship', 'year': 2025,
     'url': 'https://www.darts24.com/europe/european-championship/2025/results/'},
    {'name': 'PDC Grand Slam of Darts', 'year': 2025,
     'url': 'https://www.darts24.com/world/grand-slam/2025/results/'},
    {'name': 'PDC Players Championship Finals', 'year': 2025,
     'url': 'https://www.darts24.com/world/players-championship-finals/2025/results/'},
    # 2024 season
    {'name': 'PDC World Championship', 'year': 2024,
     'url': 'https://www.darts24.com/world/pdc-world-championship/2024/results/'},
    {'name': 'PDC Premier League', 'year': 2024,
     'url': 'https://www.darts24.com/world/premier-league/2024/results/'},
    {'name': 'PDC UK Open', 'year': 2024,
     'url': 'https://www.darts24.com/united-kingdom/uk-open/2024/results/'},
    {'name': 'PDC World Matchplay', 'year': 2024,
     'url': 'https://www.darts24.com/world/world-matchplay/2024/results/'},
    {'name': 'PDC World Grand Prix', 'year': 2024,
     'url': 'https://www.darts24.com/world/world-grand-prix/2024/results/'},
    {'name': 'PDC European Championship', 'year': 2024,
     'url': 'https://www.darts24.com/europe/european-championship/2024/results/'},
    {'name': 'PDC Grand Slam of Darts', 'year': 2024,
     'url': 'https://www.darts24.com/world/grand-slam/2024/results/'},
    {'name': 'PDC Players Championship Finals', 'year': 2024,
     'url': 'https://www.darts24.com/world/players-championship-finals/2024/results/'},
]


if __name__ == '__main__':
    scraper = Darts24Scraper()
    for t in MAJOR_TOURNAMENTS:
        result = scraper.scrape_tournament(
            results_url=t['url'],
            tournament_name=t['name'],
            year=t['year'],
        )
        print(f"[darts24] {t['name']} {t['year']}: {result}")

    scraper.promote_to_matches()
    print("\n[darts24] Staging summary:", scraper.staging_summary())
