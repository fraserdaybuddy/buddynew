"""
stats_puller.py — Nightly Sackmann pull → match_results + model_errors
JOB-006 Sports Betting Model

Fetches ATP + WTA rows from Sackmann for the last N days.
Inserts into match_results. If a bet exists for the match, logs prediction error.

Uses fetch_csv() from src/scrapers/tennis/sackmann.py — do NOT re-implement.

Run:  PYTHONUTF8=1 python -m src.data.stats_puller
Cron: 02:15 UTC daily
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.scrapers.tennis.sackmann import fetch_csv, ATP_BASE, WTA_BASE
from src.database import get_conn, DB_PATH

log = logging.getLogger("stats_puller")

SACKMANN_ATP_URL = ATP_BASE + "/atp_matches_{year}.csv"
SACKMANN_WTA_URL = WTA_BASE + "/wta_matches_{year}.csv"


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def get_recent_matches(days_back: int = 2) -> list[dict]:
    """Return ATP + WTA matches from the last N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y%m%d")
    year   = datetime.utcnow().year
    rows   = []

    for tour, url_tpl in [("atp", SACKMANN_ATP_URL), ("wta", SACKMANN_WTA_URL)]:
        url = url_tpl.format(year=year)
        try:
            all_rows = fetch_csv(url, delay=0.5)
            if all_rows:
                recent = [r for r in all_rows if r.get("tourney_date", "") >= cutoff]
                for r in recent:
                    r["_tour"] = tour
                rows.extend(recent)
                log.info(f"[stats_puller] {tour.upper()} {year}: {len(recent)} recent rows")
        except Exception as e:
            log.warning(f"[stats_puller] Could not fetch {tour} {year}: {e}")

    return rows


# ── Score parsing ─────────────────────────────────────────────────────────────

def parse_score(score_str: str) -> tuple:
    """
    Parse Sackmann score → (actual_games, actual_sets).
    e.g. "6-3 6-2" → (17, 2)
    Handles tiebreak notation like "7-6(4)".
    Returns (None, None) on failure or retirement.
    """
    if not score_str or score_str.strip() in ("", "W/O", "RET", "DEF"):
        return None, None

    # Strip tiebreak suffix e.g. "7-6(4)" → "7-6"
    import re
    cleaned = re.sub(r"\(\d+\)", "", score_str).strip()
    set_parts = cleaned.split()

    total_games = 0
    valid_sets  = 0
    for s in set_parts:
        try:
            a, b = s.split("-")
            total_games += int(a) + int(b)
            valid_sets  += 1
        except (ValueError, IndexError):
            pass  # ignore non-score tokens

    if total_games == 0:
        return None, None
    return total_games, valid_sets


# ── Bet matching ──────────────────────────────────────────────────────────────

def match_to_bet(conn, player_a: str, player_b: str, date_str: str):
    """
    Try to find a ledger row matching these players and date.
    date_str is YYYYMMDD from Sackmann → convert to YYYY-MM-DD for comparison.
    Matches on last name only for robustness.
    Returns bet_id or None.
    """
    date_fmt  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    last_a    = player_a.split()[-1] if player_a else ""
    last_b    = player_b.split()[-1] if player_b else ""

    row = conn.execute("""
        SELECT l.bet_id FROM ledger l
        JOIN matches m ON l.match_id = m.match_id
        WHERE date(m.match_date) = ?
          AND (
              (m.player1_id LIKE ? AND m.player2_id LIKE ?)
           OR (m.player1_id LIKE ? AND m.player2_id LIKE ?)
          )
        LIMIT 1
    """, (date_fmt,
          f"%{last_a}%", f"%{last_b}%",
          f"%{last_b}%", f"%{last_a}%")).fetchone()

    return row[0] if row else None


# ── Model error logging ───────────────────────────────────────────────────────

def log_prediction_error(conn, bet_id: str, actual_games: int) -> None:
    """
    If the ledger row has a predicted_line, compute error and write to model_errors.
    """
    bet = conn.execute("""
        SELECT sport, market_type, surface, elo_gap, predicted_line, bet_direction, won
        FROM ledger WHERE bet_id = ?
    """, (bet_id,)).fetchone()

    if not bet:
        return

    predicted = bet["predicted_line"]
    if predicted is None:
        return

    error = actual_games - predicted
    conn.execute("""
        INSERT OR IGNORE INTO model_errors
          (bet_id, sport, market_type, surface, elo_gap,
           predicted, actual, error, direction, won, logged_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        bet_id,
        bet["sport"],
        bet["market_type"] if "market_type" in bet.keys() else None,
        bet["surface"],
        bet["elo_gap"],
        predicted,
        actual_games,
        round(error, 2),
        bet["bet_direction"],
        bet["won"],
        datetime.utcnow().isoformat(),
    ))


# ── Main pull ─────────────────────────────────────────────────────────────────

def pull_stats(db_path=DB_PATH, days_back: int = 2) -> int:
    conn     = get_conn(db_path)
    matches  = get_recent_matches(days_back=days_back)
    inserted = 0

    for m in matches:
        player_a  = m.get("winner_name", "")
        player_b  = m.get("loser_name", "")
        date_str  = m.get("tourney_date", "")
        score_str = m.get("score", "")
        tour      = m.get("_tour", "atp")

        if not player_a or not date_str:
            continue

        match_id = (
            f"sackmann_{tour}_{date_str}_{player_a}_{player_b}"
            .replace(" ", "_")
        )

        # Skip if already stored
        if conn.execute(
            "SELECT 1 FROM match_results WHERE match_id=?", (match_id,)
        ).fetchone():
            continue

        games, sets = parse_score(score_str)
        bet_id      = match_to_bet(conn, player_a, player_b, date_str)
        match_date  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        conn.execute("""
            INSERT OR IGNORE INTO match_results
              (match_id, bet_id, sport, player_a, player_b, match_date,
               actual_games, actual_sets, score_str, winner, result_source, pulled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            match_id, bet_id, "tennis", player_a, player_b, match_date,
            games, sets, score_str, player_a,
            "sackmann", datetime.utcnow().isoformat(),
        ))
        inserted += 1

        if bet_id and games:
            log_prediction_error(conn, bet_id, games)

    conn.commit()
    conn.close()
    print(f"[stats_puller] Inserted {inserted} new match results ({len(matches)} fetched)")
    return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    pull_stats()
