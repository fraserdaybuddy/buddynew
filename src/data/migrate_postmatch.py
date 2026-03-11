"""
migrate_postmatch.py — Post-match pipeline DB migrations
JOB-006 Sports Betting Model

Adds settlement columns to ledger table.
Creates new tables: match_results, model_errors, account_snapshots.

Safe to run multiple times — fully idempotent.

Run:  PYTHONUTF8=1 python -m src.data.migrate_postmatch
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.database import get_conn, DB_PATH


# Columns to add to ledger (skipped if already present)
LEDGER_COLUMNS = [
    ("settled",       "INTEGER DEFAULT 0"),
    ("won",           "INTEGER"),
    ("actual_profit", "REAL"),
    # settlement columns for model feedback
    ("predicted_line", "REAL"),
    ("surface",        "TEXT"),
    ("elo_gap",        "INTEGER"),
]

NEW_TABLES = """
-- Actual match stats — for model feedback
CREATE TABLE IF NOT EXISTS match_results (
    match_id        TEXT PRIMARY KEY,   -- sackmann ID or constructed key
    bet_id          TEXT,               -- FK to ledger.bet_id (nullable)
    sport           TEXT,
    player_a        TEXT,
    player_b        TEXT,
    match_date      TEXT,               -- YYYY-MM-DD
    actual_games    INTEGER,
    actual_sets     INTEGER,
    actual_legs     INTEGER,
    actual_frames   INTEGER,
    winner          TEXT,
    score_str       TEXT,               -- e.g. "6-3 6-2"
    result_source   TEXT,               -- 'betfair_settled' | 'sackmann' | 'manual'
    pulled_at       TEXT
);

-- Model prediction errors — one row per settled bet with a prediction
CREATE TABLE IF NOT EXISTS model_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id          TEXT,
    sport           TEXT,
    market_type     TEXT,               -- total_games | first_set | total_legs etc.
    surface         TEXT,
    elo_gap         INTEGER,
    predicted       REAL,               -- model predicted value (e.g. 18.3 games)
    actual          REAL,               -- what actually happened
    error           REAL,               -- actual - predicted
    direction       TEXT,               -- 'over' or 'under'
    won             INTEGER,            -- did the bet win?
    logged_at       TEXT
);

-- Account balance snapshots — one row per day
CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_date   TEXT PRIMARY KEY,   -- YYYY-MM-DD
    exchange        TEXT DEFAULT 'betfair',
    balance         REAL,
    exposure        REAL,
    retained_comm   REAL,
    cumulative_pl   REAL,
    snapped_at      TEXT
);
"""


def run(db_path=DB_PATH):
    with get_conn(db_path) as conn:

        # ── 1. Extend ledger table ──────────────────────────────────────────────
        for col_name, col_def in LEDGER_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE ledger ADD COLUMN {col_name} {col_def}")
                print(f"  [migrate] ledger.{col_name}  ADDED")
            except Exception as e:
                if "duplicate column" in str(e).lower():
                    print(f"  [migrate] ledger.{col_name}  already exists — skip")
                else:
                    print(f"  [migrate] ledger.{col_name}  ERROR: {e}")

        # ── 2. Create new tables ────────────────────────────────────────────────
        conn.executescript(NEW_TABLES)
        print("  [migrate] match_results      OK (CREATE IF NOT EXISTS)")
        print("  [migrate] model_errors       OK (CREATE IF NOT EXISTS)")
        print("  [migrate] account_snapshots  OK (CREATE IF NOT EXISTS)")

    print("\n[migrate_postmatch] Done — all migrations applied.")


if __name__ == "__main__":
    print(f"[migrate_postmatch] Target DB: {DB_PATH}\n")
    run()
