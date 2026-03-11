"""
elo_loader.py — Surface ELO ratings for tennis players
JOB-006 Sports Betting Model

Computes surface-specific ELO ratings (Hard/Clay/Grass) and overall ELO
by walking chronologically through all tennis matches.

Writes pre-match ELO values into matches table columns:
  p1_elo_surface  REAL  — player1's ELO on this surface before the match
  p2_elo_surface  REAL  — player2's ELO on this surface before the match
  p1_elo_overall  REAL  — player1's overall ELO before the match
  p2_elo_overall  REAL  — player2's overall ELO before the match

Also maintains elo_ratings table with current (most recent) ratings:
  player_id, surface, elo, match_count, last_updated

ELO formula:
  E_A = 1 / (1 + 10^((R_B - R_A) / 400))
  new_R_A = R_A + K(n) * (outcome_A - E_A)

K-factor decay:
  n < 10  → K = 40  (provisional)
  n < 30  → K = 24
  n >= 30 → K = 16

Initial rating: 1500 for all players on all surfaces.

Retired/walkover matches: excluded from ELO updates (outcome not reliable).
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "universe.db"

INITIAL_ELO = 1500.0
SURFACES = ("Hard", "Clay", "Grass")
SURFACE_KEY = "Overall"

ELO_SCHEMA = """
CREATE TABLE IF NOT EXISTS elo_ratings (
    player_id     TEXT NOT NULL,
    surface       TEXT NOT NULL,   -- Hard | Clay | Grass | Overall
    elo           REAL NOT NULL DEFAULT 1500,
    match_count   INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT,
    PRIMARY KEY (player_id, surface)
);
"""

ADD_MATCH_COLS = [
    "ALTER TABLE matches ADD COLUMN p1_elo_surface REAL",
    "ALTER TABLE matches ADD COLUMN p2_elo_surface REAL",
    "ALTER TABLE matches ADD COLUMN p1_elo_overall REAL",
    "ALTER TABLE matches ADD COLUMN p2_elo_overall REAL",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def k_factor(match_count: int) -> float:
    if match_count < 10:
        return 40.0
    if match_count < 30:
        return 24.0
    return 16.0


def expected_score(r_a: float, r_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((r_b - r_a) / 400.0))


def update_elo(r_a: float, r_b: float, n_a: int, outcome_a: float) -> tuple:
    """
    Returns (new_r_a, delta) where:
      outcome_a = 1.0 if A won, 0.0 if A lost
    """
    e_a = expected_score(r_a, r_b)
    k = k_factor(n_a)
    delta = k * (outcome_a - e_a)
    return r_a + delta, delta


# ---------------------------------------------------------------------------
# Rating store (in-memory, flushed to DB at end)
# ---------------------------------------------------------------------------

class RatingStore:
    """Holds current ELO and match counts for all players × surfaces."""

    def __init__(self):
        # ratings[(player_id, surface)] = elo
        self.ratings: dict = {}
        # counts[(player_id, surface)] = n
        self.counts: dict = {}
        # last_date[(player_id, surface)] = date string
        self.last_date: dict = {}

    def get(self, player_id: str, surface: str) -> float:
        return self.ratings.get((player_id, surface), INITIAL_ELO)

    def count(self, player_id: str, surface: str) -> int:
        return self.counts.get((player_id, surface), 0)

    def set(self, player_id: str, surface: str, elo: float, date: str) -> None:
        self.ratings[(player_id, surface)] = elo
        self.counts[(player_id, surface)] = self.counts.get((player_id, surface), 0) + 1
        self.last_date[(player_id, surface)] = date

    def snapshot(self, player_id: str, surface: str) -> tuple:
        """Return (elo, count) before updating."""
        return self.get(player_id, surface), self.count(player_id, surface)


def normalize_surface(raw: str) -> str:
    """Map raw surface string to Hard/Clay/Grass, or None if unknown."""
    if not raw:
        return None
    s = raw.strip().title()
    if s in ("Hard", "Clay", "Grass"):
        return s
    if "Hard" in s or "Indoor" in s or "Carpet" in s:
        return "Hard"
    if "Clay" in s or "Dirt" in s:
        return "Clay"
    if "Grass" in s:
        return "Grass"
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def add_columns(conn: sqlite3.Connection) -> None:
    for sql in ADD_MATCH_COLS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def run(db_path: Path = DB_PATH, warm_start: bool = False) -> None:
    """
    Args:
        db_path    — path to universe.db
        warm_start — if True, seed the RatingStore from the current elo_ratings table
                     (populated by elo_warmup.py) before walking 2024 matches.
                     If False, all players start at INITIAL_ELO = 1500.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"[elo] DB: {db_path}")
    if warm_start:
        print("[elo] warm_start=True — seeding from elo_ratings (historical warm-up)")

    # Ensure schema
    conn.executescript(ELO_SCHEMA)
    add_columns(conn)
    conn.commit()

    # Load all tennis matches in chronological order
    # Exclude retired and walkover matches
    matches = conn.execute(
        """SELECT m.match_id, m.match_date, m.player1_id, m.player2_id, m.winner_id,
                  m.retired, t.surface
           FROM matches m
           JOIN tournaments t ON m.tournament_id = t.tournament_id
           WHERE m.sport = 'tennis'
             AND m.player1_id IS NOT NULL
             AND m.player2_id IS NOT NULL
             AND m.winner_id IS NOT NULL
             AND COALESCE(m.retired, 0) = 0
           ORDER BY m.match_date ASC, m.match_id ASC"""
    ).fetchall()

    print(f"[elo] Processing {len(matches)} matches (excluding retired/W/O)...")

    store = RatingStore()

    # Seed from warm-up ratings if requested
    if warm_start:
        seed_rows = conn.execute(
            "SELECT player_id, surface, elo, match_count FROM elo_ratings"
        ).fetchall()
        seeded = 0
        for row in seed_rows:
            pid = row["player_id"]
            surface = row["surface"]
            # Only seed if this player appears in our 2024 matches
            store.ratings[(pid, surface)] = row["elo"]
            store.counts[(pid, surface)] = row["match_count"]
            seeded += 1
        print(f"[elo] Seeded {seeded} player×surface ratings from warm-up")
    updates = []  # (p1_elo_surface, p2_elo_surface, p1_elo_overall, p2_elo_overall, match_id)

    for m in matches:
        p1 = m["player1_id"]
        p2 = m["player2_id"]
        winner = m["winner_id"]
        date = m["match_date"]
        surface = normalize_surface(m["surface"])

        outcome_p1 = 1.0 if winner == p1 else 0.0
        outcome_p2 = 1.0 - outcome_p1

        # --- Surface ELO ---
        if surface:
            r1_s, n1_s = store.snapshot(p1, surface)
            r2_s, n2_s = store.snapshot(p2, surface)

            new_r1_s, _ = update_elo(r1_s, r2_s, n1_s, outcome_p1)
            new_r2_s, _ = update_elo(r2_s, r1_s, n2_s, outcome_p2)

            store.set(p1, surface, new_r1_s, date)
            store.set(p2, surface, new_r2_s, date)
        else:
            r1_s = r2_s = None

        # --- Overall ELO ---
        r1_o, n1_o = store.snapshot(p1, SURFACE_KEY)
        r2_o, n2_o = store.snapshot(p2, SURFACE_KEY)

        new_r1_o, _ = update_elo(r1_o, r2_o, n1_o, outcome_p1)
        new_r2_o, _ = update_elo(r2_o, r1_o, n2_o, outcome_p2)

        store.set(p1, SURFACE_KEY, new_r1_o, date)
        store.set(p2, SURFACE_KEY, new_r2_o, date)

        updates.append((
            round(r1_s, 2) if r1_s is not None else None,
            round(r2_s, 2) if r2_s is not None else None,
            round(r1_o, 2),
            round(r2_o, 2),
            m["match_id"],
        ))

    # Batch write pre-match ELO to matches
    print(f"[elo] Writing {len(updates)} ELO snapshots to matches...")
    conn.executemany(
        """UPDATE matches
           SET p1_elo_surface = ?,
               p2_elo_surface = ?,
               p1_elo_overall = ?,
               p2_elo_overall = ?
           WHERE match_id = ?""",
        updates
    )

    # Flush current ratings to elo_ratings table
    print("[elo] Writing current ratings to elo_ratings...")
    conn.execute("DELETE FROM elo_ratings WHERE 1=1")  # full refresh
    rating_rows = []
    for (player_id, surface), elo in store.ratings.items():
        n = store.counts.get((player_id, surface), 0)
        last = store.last_date.get((player_id, surface))
        rating_rows.append((player_id, surface, round(elo, 2), n, last))

    conn.executemany(
        """INSERT INTO elo_ratings (player_id, surface, elo, match_count, last_updated)
           VALUES (?, ?, ?, ?, ?)""",
        rating_rows
    )

    conn.commit()
    conn.close()

    # Summary
    conn2 = sqlite3.connect(db_path)
    c = conn2.cursor()
    c.execute("SELECT COUNT(*) FROM elo_ratings")
    print(f"[elo] Ratings stored: {c.fetchone()[0]}")
    c.execute("SELECT surface, COUNT(*), ROUND(AVG(elo),1), ROUND(MIN(elo),1), ROUND(MAX(elo),1) FROM elo_ratings GROUP BY surface ORDER BY surface")
    print(f"\n{'Surface':<10} {'Players':>8} {'Avg ELO':>9} {'Min':>7} {'Max':>7}")
    print("-" * 45)
    for r in c.fetchall():
        print(f"{r[0]:<10} {r[1]:>8} {r[2]:>9} {r[3]:>7} {r[4]:>7}")

    c.execute("""SELECT COUNT(*) FROM matches
                 WHERE sport='tennis' AND p1_elo_surface IS NOT NULL""")
    print(f"\n[elo] Matches with surface ELO populated: {c.fetchone()[0]}")
    c.execute("""SELECT COUNT(*) FROM matches
                 WHERE sport='tennis' AND p1_elo_overall IS NOT NULL""")
    print(f"[elo] Matches with overall ELO populated:  {c.fetchone()[0]}")

    # Top 10 current Hard ELO
    c.execute("""
        SELECT pl.full_name, er.elo, er.match_count
        FROM elo_ratings er
        JOIN players pl ON pl.player_id = er.player_id
        WHERE er.surface = 'Hard'
        ORDER BY er.elo DESC LIMIT 10
    """)
    print(f"\nTop 10 Hard ELO:")
    for i, r in enumerate(c.fetchall(), 1):
        print(f"  {i:2}. {r[0]:<25} {r[1]:>7.1f}  (n={r[2]})")

    conn2.close()
    print("\n[elo] Done.")


if __name__ == "__main__":
    run()
