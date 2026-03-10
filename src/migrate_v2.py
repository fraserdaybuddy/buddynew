"""
migrate_v2.py — Schema enrichment: promote staging stats into matches table

Adds per-match player stats (avg, checkout%, 140+, legs_won for darts;
frames_won for snooker; svpt for tennis) and populates winner_id.

All joins are constrained by tournament to prevent cross-tournament duplicates.
Player ordering (p1/p2 in staging vs matches) is resolved per-row.

Usage:
    PYTHONUTF8=1 python -m src.migrate_v2
    PYTHONUTF8=1 python -m src.migrate_v2 --dry-run
"""

import sys
import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
from database import get_conn, backup, DB_PATH


# ── New columns ─────────────────────────────────────────────────────────────

NEW_COLUMNS = {
    "darts": [
        ("p1_avg",          "REAL"),
        ("p2_avg",          "REAL"),
        ("p1_checkout_pct", "REAL"),
        ("p2_checkout_pct", "REAL"),
        ("p1_140plus",      "INTEGER"),
        ("p2_140plus",      "INTEGER"),
        ("p1_legs_won",     "INTEGER"),
        ("p2_legs_won",     "INTEGER"),
    ],
    "snooker": [
        ("p1_frames_won",   "INTEGER"),
        ("p2_frames_won",   "INTEGER"),
    ],
    "tennis": [
        ("p1_svpt",         "INTEGER"),
        ("p2_svpt",         "INTEGER"),
    ],
}


def add_columns(conn: sqlite3.Connection, dry_run: bool) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(matches)")
    existing = {r[1] for r in cur.fetchall()}

    for sport_cols in NEW_COLUMNS.values():
        for col, dtype in sport_cols:
            if col not in existing:
                sql = f"ALTER TABLE matches ADD COLUMN {col} {dtype}"
                print(f"  ADD COLUMN: {col} {dtype}")
                if not dry_run:
                    cur.execute(sql)
            else:
                print(f"  SKIP (exists): {col}")


# ── Darts ────────────────────────────────────────────────────────────────────

DARTS_JOIN = """
    SELECT
        m.match_id,
        m.player1_id,
        pa1.player_id AS staging_p1_id,
        pa2.player_id AS staging_p2_id,
        s.p1_avg, s.p2_avg,
        s.p1_checkout_pct, s.p2_checkout_pct,
        s.p1_140plus, s.p2_140plus,
        s.p1_score, s.p2_score
    FROM staging_darts s
    JOIN tournaments t
        ON t.name = s.tournament_name
        AND t.year = s.tournament_year
        AND t.sport = 'darts'
    JOIN player_aliases pa1
        ON pa1.raw_name = s.p1_raw_name AND pa1.source = 'darts24'
    JOIN player_aliases pa2
        ON pa2.raw_name = s.p2_raw_name AND pa2.source = 'darts24'
    JOIN matches m
        ON m.tournament_id = t.tournament_id
        AND m.match_date = s.match_date
        AND m.round = s.round
        AND (
            (m.player1_id = pa1.player_id AND m.player2_id = pa2.player_id)
         OR (m.player1_id = pa2.player_id AND m.player2_id = pa1.player_id)
        )
    WHERE s.p1_avg IS NOT NULL
"""

DARTS_UPDATE = """
    UPDATE matches SET
        p1_avg          = ?,
        p2_avg          = ?,
        p1_checkout_pct = ?,
        p2_checkout_pct = ?,
        p1_140plus      = ?,
        p2_140plus      = ?,
        p1_legs_won     = ?,
        p2_legs_won     = ?,
        winner_id       = ?
    WHERE match_id = ?
"""


def migrate_darts(conn: sqlite3.Connection, dry_run: bool) -> dict:
    cur = conn.cursor()
    rows = cur.execute(DARTS_JOIN).fetchall()

    updated = skipped = errors = 0
    for row in rows:
        (match_id, match_p1_id, stg_p1_id, stg_p2_id,
         p1_avg_s, p2_avg_s,
         p1_co_s, p2_co_s,
         p1_140_s, p2_140_s,
         p1_score_s, p2_score_s) = row

        try:
            p1_legs = int(p1_score_s) if p1_score_s else None
            p2_legs = int(p2_score_s) if p2_score_s else None
        except (ValueError, TypeError):
            errors += 1
            continue

        # Resolve player ordering: staging p1 may not be matches player1
        if match_p1_id == stg_p1_id:
            # Same order
            p1_avg, p2_avg           = p1_avg_s,  p2_avg_s
            p1_co,  p2_co            = p1_co_s,   p2_co_s
            p1_140, p2_140           = p1_140_s,  p2_140_s
            p1_won, p2_won           = p1_legs,   p2_legs
            winner = stg_p1_id if (p1_legs or 0) > (p2_legs or 0) else (
                     stg_p2_id if (p2_legs or 0) > (p1_legs or 0) else None)
        else:
            # Reversed: staging p2 = matches player1
            p1_avg, p2_avg           = p2_avg_s,  p1_avg_s
            p1_co,  p2_co            = p2_co_s,   p1_co_s
            p1_140, p2_140           = p2_140_s,  p1_140_s
            p1_won, p2_won           = p2_legs,   p1_legs
            winner = stg_p2_id if (p2_legs or 0) > (p1_legs or 0) else (
                     stg_p1_id if (p1_legs or 0) > (p2_legs or 0) else None)

        if not dry_run:
            cur.execute(DARTS_UPDATE, (
                p1_avg, p2_avg, p1_co, p2_co,
                p1_140, p2_140, p1_won, p2_won,
                winner, match_id
            ))
        updated += 1

    return {"updated": updated, "skipped": skipped, "errors": errors}


# ── Snooker ──────────────────────────────────────────────────────────────────

SNOOKER_JOIN = """
    SELECT
        m.match_id,
        m.player1_id,
        pa1.player_id AS staging_p1_id,
        pa2.player_id AS staging_p2_id,
        s.p1_frames, s.p2_frames
    FROM staging_snooker s
    JOIN tournaments t
        ON t.name = s.tournament_name
        AND t.year = s.tournament_year
        AND t.sport = 'snooker'
    JOIN player_aliases pa1
        ON pa1.raw_name = s.p1_raw_name AND pa1.source = 'cuetrackeR'
    JOIN player_aliases pa2
        ON pa2.raw_name = s.p2_raw_name AND pa2.source = 'cuetrackeR'
    JOIN matches m
        ON m.tournament_id = t.tournament_id
        AND m.match_date = s.match_date
        AND m.round = s.round
        AND (
            (m.player1_id = pa1.player_id AND m.player2_id = pa2.player_id)
         OR (m.player1_id = pa2.player_id AND m.player2_id = pa1.player_id)
        )
    WHERE s.p1_frames IS NOT NULL
"""

SNOOKER_UPDATE = """
    UPDATE matches SET
        p1_frames_won = ?,
        p2_frames_won = ?,
        winner_id     = ?
    WHERE match_id = ?
"""


def migrate_snooker(conn: sqlite3.Connection, dry_run: bool) -> dict:
    cur = conn.cursor()
    rows = cur.execute(SNOOKER_JOIN).fetchall()

    updated = errors = 0
    for row in rows:
        match_id, match_p1_id, stg_p1_id, stg_p2_id, p1_frames, p2_frames = row

        if match_p1_id == stg_p1_id:
            f1, f2 = p1_frames, p2_frames
            winner = stg_p1_id if (p1_frames or 0) > (p2_frames or 0) else (
                     stg_p2_id if (p2_frames or 0) > (p1_frames or 0) else None)
        else:
            f1, f2 = p2_frames, p1_frames
            winner = stg_p2_id if (p2_frames or 0) > (p1_frames or 0) else (
                     stg_p1_id if (p1_frames or 0) > (p2_frames or 0) else None)

        if not dry_run:
            cur.execute(SNOOKER_UPDATE, (f1, f2, winner, match_id))
        updated += 1

    return {"updated": updated, "errors": errors}


# ── Tennis ───────────────────────────────────────────────────────────────────

TENNIS_JOIN = """
    SELECT
        m.match_id,
        m.player1_id,
        pa1.player_id AS staging_p1_id,
        s.p1_svpt, s.p2_svpt
    FROM staging_tennis s
    JOIN tournaments t
        ON t.name = s.tournament_name
        AND t.year = s.tournament_year
        AND t.sport = 'tennis'
    JOIN player_aliases pa1
        ON pa1.raw_name = s.p1_raw_name AND pa1.source = 'sackmann_atp'
    JOIN player_aliases pa2
        ON pa2.raw_name = s.p2_raw_name AND pa2.source = 'sackmann_atp'
    JOIN matches m
        ON m.tournament_id = t.tournament_id
        AND m.match_date = s.match_date
        AND m.round = s.round
        AND (
            (m.player1_id = pa1.player_id AND m.player2_id = pa2.player_id)
         OR (m.player1_id = pa2.player_id AND m.player2_id = pa1.player_id)
        )
    WHERE s.p1_svpt IS NOT NULL
"""

TENNIS_UPDATE = """
    UPDATE matches SET
        p1_svpt   = ?,
        p2_svpt   = ?,
        winner_id = ?
    WHERE match_id = ?
"""


def migrate_tennis(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """
    Sackmann data: p1 = match winner, p2 = loser (consistent convention).
    winner_id = match player whose staging p1 maps to.
    """
    cur = conn.cursor()
    rows = cur.execute(TENNIS_JOIN).fetchall()

    updated = errors = 0
    for row in rows:
        match_id, match_p1_id, stg_p1_id, p1_svpt, p2_svpt = row

        if match_p1_id == stg_p1_id:
            s1, s2 = p1_svpt, p2_svpt
            winner = stg_p1_id   # Sackmann p1 = winner
        else:
            s1, s2 = p2_svpt, p1_svpt
            winner = stg_p1_id   # Still stg_p1_id because Sackmann p1 = winner regardless

        if not dry_run:
            cur.execute(TENNIS_UPDATE, (s1, s2, winner, match_id))
        updated += 1

    return {"updated": updated, "errors": errors}


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, no writes")
    args = parser.parse_args()

    if args.dry_run:
        print("[migrate_v2] DRY RUN — no changes will be written\n")
    else:
        backup_path = backup("pre_migrate_v2")
        print(f"[migrate_v2] Backup created: {backup_path}\n")

    conn = get_conn()

    print("── Step 1: Add new columns ──")
    add_columns(conn, args.dry_run)

    print("\n── Step 2: Migrate darts ──")
    r = migrate_darts(conn, args.dry_run)
    print(f"  updated={r['updated']}  errors={r['errors']}")

    print("\n── Step 3: Migrate snooker ──")
    r = migrate_snooker(conn, args.dry_run)
    print(f"  updated={r['updated']}  errors={r['errors']}")

    print("\n── Step 4: Migrate tennis ──")
    r = migrate_tennis(conn, args.dry_run)
    print(f"  updated={r['updated']}  errors={r['errors']}")

    if not args.dry_run:
        conn.commit()

    if args.dry_run:
        print("\n[migrate_v2] Dry run complete — no changes written")
        conn.close()
        return

    print("\n── Step 5: Verification ──")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM matches WHERE sport='darts' AND p1_avg IS NOT NULL")
    print(f"  darts with avg:          {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM matches WHERE sport='darts' AND p1_legs_won IS NOT NULL")
    print(f"  darts with legs_won:     {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM matches WHERE sport='snooker' AND p1_frames_won IS NOT NULL")
    print(f"  snooker with frames_won: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM matches WHERE sport='tennis' AND p1_svpt IS NOT NULL")
    print(f"  tennis with svpt:        {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM matches WHERE winner_id IS NOT NULL")
    print(f"  matches with winner_id:  {cur.fetchone()[0]}")

    conn.close()
    print("\n[migrate_v2] Migration complete")


if __name__ == "__main__":
    main()
