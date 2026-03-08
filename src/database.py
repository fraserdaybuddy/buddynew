"""
database.py — Single source of truth for universe.db
JOB-006 Sports Betting Model

All schema creation, migrations, and low-level access live here.
No other module creates tables or alters schema.
"""

import sqlite3
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "universe.db"
BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"


# ─────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────

def get_conn(path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a connection with foreign keys enforced and row_factory set."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def backup(label: str = "") -> Path:
    """Create a timestamped backup of universe.db before any write operation."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    dest = BACKUP_DIR / f"universe_{ts}{suffix}.db"
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, dest)
    return dest


# ─────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────

SCHEMA = """

-- ── PLAYERS ──────────────────────────────────────────────────────────────────
-- Canonical player registry. One row per real-world player.
-- ID format: {TOUR}-{SURNAME}-{INITIAL}  e.g. PDC-HUMPHRIES-L
CREATE TABLE IF NOT EXISTS players (
    player_id       TEXT PRIMARY KEY,           -- PDC-HUMPHRIES-L
    tour            TEXT NOT NULL,              -- PDC | WST | ATP | WTA
    full_name       TEXT NOT NULL,
    nationality     TEXT,
    active          INTEGER NOT NULL DEFAULT 1, -- 0 = retired/inactive
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── PLAYER ALIASES ────────────────────────────────────────────────────────────
-- Maps raw scraped names → canonical player_id
-- Every name variant a source uses must appear here before being used in matches.
CREATE TABLE IF NOT EXISTS player_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name        TEXT NOT NULL,
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    source          TEXT NOT NULL,              -- dartsdatabase | cuetrackeR | atp | manual
    confidence      REAL NOT NULL,              -- 0.0 – 1.0
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | ACCEPTED | REJECTED
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(raw_name, source)
);

-- ── ALIAS REVIEW QUEUE ────────────────────────────────────────────────────────
-- Aliases with confidence 0.80–0.94 that need human review before use.
-- HARD GATE: queue must be empty before Phase 1 begins.
CREATE TABLE IF NOT EXISTS alias_review_queue (
    queue_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name        TEXT NOT NULL,
    suggested_id    TEXT REFERENCES players(player_id),
    confidence      REAL NOT NULL,
    source          TEXT NOT NULL,
    context         TEXT,                       -- match context for manual review
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolution      TEXT                        -- ACCEPTED | REJECTED | NEW_PLAYER
);

-- ── TOURNAMENTS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id   TEXT PRIMARY KEY,           -- PDC-2024-WORLDS
    sport           TEXT NOT NULL,              -- darts | snooker | tennis
    tour            TEXT NOT NULL,              -- PDC | WST | ATP | WTA
    name            TEXT NOT NULL,
    year            INTEGER NOT NULL,
    start_date      TEXT,
    end_date        TEXT,
    venue           TEXT,
    surface         TEXT,                       -- tennis only: hard | clay | grass
    prize_fund_gbp  REAL,
    source_url      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── MATCHES ───────────────────────────────────────────────────────────────────
-- Core match results table. Append-only — never UPDATE, only INSERT.
-- NULL player_id is forbidden: Resolver must resolve all names before insertion.
CREATE TABLE IF NOT EXISTS matches (
    match_id        TEXT PRIMARY KEY,           -- hash(tournament_id + round + p1 + p2 + date)
    tournament_id   TEXT NOT NULL REFERENCES tournaments(tournament_id),
    sport           TEXT NOT NULL,              -- darts | snooker | tennis
    round           TEXT NOT NULL,              -- R1 | R2 | QF | SF | F | RR
    match_date      TEXT NOT NULL,              -- YYYY-MM-DD
    player1_id      TEXT NOT NULL REFERENCES players(player_id),
    player2_id      TEXT NOT NULL REFERENCES players(player_id),
    winner_id       TEXT REFERENCES players(player_id),

    -- Format
    format          TEXT NOT NULL,              -- BO11 | BO13 | BO19 | SETS_3 | SETS_5 etc.
    legs_sets_total INTEGER,                    -- total legs played (darts) or frames (snooker) or games (tennis)

    -- Darts-specific
    p1_180s         INTEGER,                    -- NULL if not recorded
    p2_180s         INTEGER,
    total_180s      INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_180s IS NOT NULL AND p2_180s IS NOT NULL
                        THEN p1_180s + p2_180s ELSE NULL END
                    ) STORED,

    -- Snooker-specific
    p1_centuries    INTEGER,
    p2_centuries    INTEGER,
    total_centuries INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_centuries IS NOT NULL AND p2_centuries IS NOT NULL
                        THEN p1_centuries + p2_centuries ELSE NULL END
                    ) STORED,

    -- Tennis-specific
    p1_aces         INTEGER,
    p2_aces         INTEGER,
    total_aces      INTEGER GENERATED ALWAYS AS (
                        CASE WHEN p1_aces IS NOT NULL AND p2_aces IS NOT NULL
                        THEN p1_aces + p2_aces ELSE NULL END
                    ) STORED,
    p1_return_pts_won_pct   REAL,               -- % return points won (style mismatch proxy)
    p2_return_pts_won_pct   REAL,
    p1_second_serve_pts_won REAL,               -- serve pressure proxy
    p2_second_serve_pts_won REAL,

    -- Provenance
    data_source     TEXT NOT NULL,              -- dartsdatabase | cuetrackeR | atp_sackmann | manual
    source_url      TEXT,
    data_quality    TEXT NOT NULL DEFAULT 'UNVERIFIED',  -- UNVERIFIED | VERIFIED | DISPUTED
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── PLAYER FORM ───────────────────────────────────────────────────────────────
-- Materialised form window: last 5 matches per player, recency-weighted.
-- Rebuilt nightly by the coordinator — never manually patched.
CREATE TABLE IF NOT EXISTS player_form (
    form_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       TEXT NOT NULL REFERENCES players(player_id),
    sport           TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,              -- form calculated as of this date
    matches_counted INTEGER NOT NULL,
    avg_180s_per_leg        REAL,               -- darts
    avg_centuries_per_frame REAL,               -- snooker
    avg_aces_per_match      REAL,               -- tennis
    avg_return_pts_won      REAL,               -- tennis style score
    form_score      REAL,                       -- composite ranking proxy (PDC/ATP points proxy)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(player_id, sport, as_of_date)
);

-- ── BETFAIR MARKETS ───────────────────────────────────────────────────────────
-- Real Betfair closing odds. ONLY real data — never estimated or synthetic.
-- closing_line MUST come from Betfair Historical Data API or confirmed manual entry.
CREATE TABLE IF NOT EXISTS betfair_markets (
    market_id       TEXT PRIMARY KEY,           -- Betfair market ID
    match_id        TEXT REFERENCES matches(match_id),
    sport           TEXT NOT NULL,
    market_type     TEXT NOT NULL,              -- total_180s | total_centuries | total_aces
    line            REAL NOT NULL,              -- the O/U line e.g. 5.5
    over_odds       REAL,                       -- closing BSP or last traded
    under_odds      REAL,
    total_matched   REAL,                       -- £ matched at close
    data_source     TEXT NOT NULL,              -- betfair_historical_api | manual
    verified        INTEGER NOT NULL DEFAULT 0, -- 1 = human-confirmed real data
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── MODEL RUNS ────────────────────────────────────────────────────────────────
-- Every time the model is run, a record is created here.
CREATE TABLE IF NOT EXISTS model_runs (
    run_id          TEXT PRIMARY KEY,           -- hash(match_id + model_version + timestamp)
    match_id        TEXT NOT NULL REFERENCES matches(match_id),
    sport           TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    p_under         REAL NOT NULL,
    p_over          REAL NOT NULL,
    fair_odds_under REAL NOT NULL,
    fair_odds_over  REAL NOT NULL,
    ci_95_lower     REAL,
    ci_95_upper     REAL,
    edge_under      REAL,                       -- vs betfair closing line
    edge_over       REAL,
    n_simulations   INTEGER NOT NULL DEFAULT 10000,
    seed            TEXT NOT NULL               -- deterministic seed for reproducibility
);

-- ── LEDGER ────────────────────────────────────────────────────────────────────
-- Every bet placed, with full lifecycle tracking.
-- Written immediately on order placement — BEFORE settlement is known.
CREATE TABLE IF NOT EXISTS ledger (
    bet_id              TEXT PRIMARY KEY,       -- hash(match_id + direction + run_id)
    run_id              TEXT REFERENCES model_runs(run_id),
    match_id            TEXT NOT NULL REFERENCES matches(match_id),
    sport               TEXT NOT NULL,
    bet_direction       TEXT NOT NULL,          -- UNDER | OVER
    line                REAL NOT NULL,
    odds_taken          REAL NOT NULL,
    stake_gbp           REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | WON | LOST | VOID | CANCELLED
    profit_loss_gbp     REAL,                   -- NULL until settled
    sportmarket_order_id TEXT,
    placed_at           TEXT NOT NULL DEFAULT (datetime('now')),
    settled_at          TEXT,
    mode                TEXT NOT NULL DEFAULT 'PAPER'  -- PAPER | LIVE
);

-- ── INDICES ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_matches_sport        ON matches(sport);
CREATE INDEX IF NOT EXISTS idx_matches_tournament   ON matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_matches_date         ON matches(match_date);
CREATE INDEX IF NOT EXISTS idx_matches_p1           ON matches(player1_id);
CREATE INDEX IF NOT EXISTS idx_matches_p2           ON matches(player2_id);
CREATE INDEX IF NOT EXISTS idx_player_form_player   ON player_form(player_id, sport, as_of_date);
CREATE INDEX IF NOT EXISTS idx_aliases_raw          ON player_aliases(raw_name, source);
CREATE INDEX IF NOT EXISTS idx_ledger_status        ON ledger(status);
CREATE INDEX IF NOT EXISTS idx_ledger_match         ON ledger(match_id);

"""


def init_db(path: Path = DB_PATH) -> None:
    """Create the database and all tables if they don't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(path) as conn:
        conn.executescript(SCHEMA)
    print(f"[database] Initialised: {path}")


# ─────────────────────────────────────────────
# Verification Queries (for hard gates)
# ─────────────────────────────────────────────

GATE_QUERIES = {
    "phase_0_darts": """
        SELECT
            (SELECT COUNT(*) FROM matches WHERE sport='darts') AS total_darts_matches,
            (SELECT COUNT(*) FROM matches WHERE sport='darts' AND total_180s IS NOT NULL) AS with_180s,
            (SELECT COUNT(*) FROM betfair_markets WHERE sport='darts' AND verified=1) AS real_betfair_odds,
            (SELECT COUNT(*) FROM matches WHERE sport='darts' AND (player1_id IS NULL OR player2_id IS NULL)) AS null_player_ids,
            (SELECT COUNT(*) FROM alias_review_queue WHERE resolved_at IS NULL) AS pending_aliases
    """,
    "phase_0_snooker": """
        SELECT
            (SELECT COUNT(*) FROM matches WHERE sport='snooker') AS total_snooker_matches,
            (SELECT COUNT(*) FROM matches WHERE sport='snooker' AND total_centuries IS NOT NULL) AS with_centuries,
            (SELECT COUNT(*) FROM betfair_markets WHERE sport='snooker' AND verified=1) AS real_betfair_odds,
            (SELECT COUNT(*) FROM matches WHERE sport='snooker' AND (player1_id IS NULL OR player2_id IS NULL)) AS null_player_ids
    """,
    "phase_0_tennis": """
        SELECT
            (SELECT COUNT(*) FROM matches WHERE sport='tennis') AS total_tennis_matches,
            (SELECT COUNT(*) FROM matches WHERE sport='tennis' AND total_aces IS NOT NULL) AS with_aces,
            (SELECT COUNT(*) FROM betfair_markets WHERE sport='tennis' AND verified=1) AS real_betfair_odds,
            (SELECT COUNT(*) FROM matches WHERE sport='tennis' AND (player1_id IS NULL OR player2_id IS NULL)) AS null_player_ids
    """,
}


def run_gate_check(gate_name: str, path: Path = DB_PATH) -> dict:
    """Run a hard gate verification query and return results as dict."""
    if gate_name not in GATE_QUERIES:
        raise ValueError(f"Unknown gate: {gate_name}")
    with get_conn(path) as conn:
        row = conn.execute(GATE_QUERIES[gate_name]).fetchone()
        return dict(row)


def match_id_from(tournament_id: str, round_: str, p1: str, p2: str, date: str) -> str:
    """Generate deterministic match_id from components."""
    raw = f"{tournament_id}|{round_}|{p1}|{p2}|{date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("[database] Schema created successfully.")
    for gate in GATE_QUERIES:
        result = run_gate_check(gate)
        print(f"\n[gate:{gate}]")
        for k, v in result.items():
            print(f"  {k}: {v}")
