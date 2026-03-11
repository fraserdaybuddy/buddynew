"""
migrate_tennis.py — Populate sets/games/best_of/retired from staging score strings
JOB-006 Sports Betting Model

Adds columns to matches:
  total_games  INTEGER   — sum of games across all sets (including partial on RET)
  best_of      INTEGER   — 3 or 5
  retired      INTEGER   — 1 if match ended by retirement/walkover/default

Populates:
  matches.legs_sets_total   — sets played (complete sets only)
  matches.winner_id         — player1_id (p1 = winner in Sackmann data)
  matches.total_games       — from score parse
  matches.best_of           — from tournament name + score
  matches.retired           — 1/0

Grand Slam BO5 list (ATP only — WTA Slams are BO3):
  Australian Open, Roland Garros, Wimbledon, Us Open
Davis Cup ties also BO5 but excluded from betting model.
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "universe.db"

GRAND_SLAMS_ATP_BO5 = {"Australian Open", "Roland Garros", "Wimbledon", "Us Open"}


# ---------------------------------------------------------------------------
# Score parser
# ---------------------------------------------------------------------------

_SET_RE = re.compile(r"(\d+)-(\d+)(?:\(\d+\))?")


def parse_score(score_str: str) -> tuple:
    """
    Parse a raw score string into (sets_played, total_games, retired).

    Returns:
        sets_played  — number of set scores found (complete + partial)
        total_games  — sum of all games in those sets
        retired      — True if match ended with RET/DEF/ABD/W/O

    Rules:
    - 'W/O' or 'WO' → (0, 0, True)
    - Tokens RET/DEF/ABD stripped, retired=True, rest parsed normally
    - Each set score 'A-B(tb)' contributes A+B games
    - Incomplete last set on retirement is included in total_games
      (relevant if the market settles on actual games played)
    """
    if not score_str:
        return (None, None, False)

    s = score_str.strip()
    retired = False

    if s in ("W/O", "WO"):
        return (0, 0, True)

    if re.search(r"\b(RET|DEF|ABD)\b", s):
        retired = True
        s = re.sub(r"\b(RET|DEF|ABD)\b", "", s).strip()

    sets = _SET_RE.findall(s)
    if not sets:
        return (None, None, retired)

    total_games = sum(int(a) + int(b) for a, b in sets)
    sets_played = len(sets)
    return (sets_played, total_games, retired)


def infer_best_of(tournament_name: str, tour: str, sets_played: int) -> int:
    """
    Return 3 or 5.

    Logic:
    - If 4 or more sets played → must be BO5
    - ATP Grand Slams → BO5
    - Davis Cup ties → BO5 (but excluded from model anyway)
    - Everything else → BO3
    """
    if sets_played is not None and sets_played >= 4:
        return 5
    if tour == "ATP" and tournament_name in GRAND_SLAMS_ATP_BO5:
        return 5
    if "Davis Cup" in (tournament_name or ""):
        return 5
    return 3


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

ADD_COLUMNS = [
    "ALTER TABLE matches ADD COLUMN total_games INTEGER",
    "ALTER TABLE matches ADD COLUMN best_of INTEGER",
    "ALTER TABLE matches ADD COLUMN retired INTEGER DEFAULT 0",
]


def add_columns(conn: sqlite3.Connection) -> None:
    for sql in ADD_COLUMNS:
        try:
            conn.execute(sql)
            print(f"[migrate] Added column: {sql.split('ADD COLUMN')[1].strip()}")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                pass  # already exists
            else:
                raise


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def run() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"[migrate] DB: {DB_PATH}")
    add_columns(conn)
    conn.commit()

    # Load staging rows that are RESOLVED
    staging_rows = conn.execute(
        """SELECT staging_id, tournament_name, tournament_year, tour,
                  round, match_date, p1_score, p1_raw_name, p2_raw_name
           FROM staging_tennis
           WHERE status = 'RESOLVED'"""
    ).fetchall()
    print(f"[migrate] Resolved staging rows: {len(staging_rows)}")

    # Build tournament_id lookup: (name, year) → tournament_id
    t_rows = conn.execute(
        "SELECT tournament_id, name, year, sport FROM tournaments WHERE sport = 'tennis'"
    ).fetchall()
    t_map = {(r["name"], r["year"]): r["tournament_id"] for r in t_rows}

    updated = 0
    skipped = 0
    no_match = 0

    for st in staging_rows:
        tid = t_map.get((st["tournament_name"], st["tournament_year"]))
        if not tid:
            skipped += 1
            continue

        # Find matching match row
        # Join on tournament_id + round + match_date
        # There may be multiple matches same round/date (e.g. Davis Cup ties)
        # Disambiguate using player names via player_aliases
        candidates = conn.execute(
            """SELECT m.match_id, m.player1_id, m.player2_id
               FROM matches m
               WHERE m.tournament_id = ? AND m.round = ? AND m.match_date = ?
                 AND m.sport = 'tennis'""",
            (tid, st["round"], st["match_date"])
        ).fetchall()

        match_id = None
        if len(candidates) == 1:
            match_id = candidates[0]["match_id"]
            p1_id = candidates[0]["player1_id"]
        elif len(candidates) > 1:
            # Disambiguate by p1_raw_name alias lookup
            for cand in candidates:
                alias_check = conn.execute(
                    """SELECT 1 FROM player_aliases
                       WHERE player_id = ? AND LOWER(raw_name) = LOWER(?)""",
                    (cand["player1_id"], st["p1_raw_name"])
                ).fetchone()
                if alias_check:
                    match_id = cand["match_id"]
                    p1_id = cand["player1_id"]
                    break
            if not match_id:
                skipped += 1
                continue
        else:
            no_match += 1
            continue

        sets_played, total_games, retired = parse_score(st["p1_score"])
        best_of = infer_best_of(st["tournament_name"], st["tour"], sets_played)

        conn.execute(
            """UPDATE matches
               SET legs_sets_total = ?,
                   total_games     = ?,
                   best_of         = ?,
                   retired         = ?,
                   winner_id       = CASE WHEN winner_id IS NULL THEN ? ELSE winner_id END
               WHERE match_id = ?""",
            (sets_played, total_games, best_of, 1 if retired else 0, p1_id, match_id)
        )
        updated += 1

    conn.commit()
    conn.close()

    print(f"[migrate] Updated:  {updated}")
    print(f"[migrate] Skipped:  {skipped}  (no tournament_id match)")
    print(f"[migrate] No match: {no_match} (no match row found)")

    # Summary stats
    conn2 = sqlite3.connect(DB_PATH)
    c = conn2.cursor()
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN legs_sets_total IS NOT NULL THEN 1 ELSE 0 END) as has_sets,
            SUM(CASE WHEN total_games IS NOT NULL THEN 1 ELSE 0 END) as has_games,
            SUM(CASE WHEN best_of = 3 THEN 1 ELSE 0 END) as bo3,
            SUM(CASE WHEN best_of = 5 THEN 1 ELSE 0 END) as bo5,
            SUM(CASE WHEN retired = 1 THEN 1 ELSE 0 END) as retired
        FROM matches WHERE sport = 'tennis'
    """)
    row = c.fetchone()
    print(f"\n[migrate] Results:")
    print(f"  Total tennis matches : {row[0]}")
    print(f"  Has sets_played      : {row[1]}")
    print(f"  Has total_games      : {row[2]}")
    print(f"  BO3                  : {row[3]}")
    print(f"  BO5                  : {row[4]}")
    print(f"  Retired / W/O        : {row[5]}")
    conn2.close()


if __name__ == "__main__":
    run()
