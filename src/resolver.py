"""
resolver.py — Player identity resolver
JOB-006 Sports Betting Model

Maps raw scraped player names → canonical player_ids.
RULES (from blueprint):
  confidence >= 0.95  → auto-accept
  confidence 0.80–0.94 → queue for human review
  confidence < 0.80   → REJECT, do not insert match

NO match row may be inserted with a NULL player_id.
The resolver must handle 100% of players in the dataset.
"""

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from database import get_conn, DB_PATH, backup


# ─────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────

CONFIDENCE_AUTO_ACCEPT = 0.95
CONFIDENCE_QUEUE       = 0.80
CONFIDENCE_REJECT      = 0.0   # below CONFIDENCE_QUEUE


# ─────────────────────────────────────────────
# Name normalisation
# ─────────────────────────────────────────────

def normalise(name: str) -> str:
    """Lowercase, strip titles/suffixes, collapse whitespace."""
    name = name.lower().strip()
    # Remove common prefixes/suffixes
    for token in ["van den", "van der", "de la"]:
        pass  # preserve these — important for Dutch/Spanish names
    # Normalise apostrophes and hyphens
    name = name.replace("'", "").replace("-", " ")
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name


def name_to_player_id(tour: str, full_name: str) -> str:
    """
    Derive a candidate player_id from tour + full name.
    Format: {TOUR}-{SURNAME}-{INITIAL}
    e.g. "Luke Humphries" → "PDC-HUMPHRIES-L"
    """
    parts = full_name.strip().split()
    if len(parts) >= 2:
        surname = parts[-1].upper()
        initial = parts[0][0].upper()
    else:
        surname = parts[0].upper()
        initial = "X"
    return f"{tour.upper()}-{surname}-{initial}"


def similarity(a: str, b: str) -> float:
    """String similarity ratio 0.0–1.0."""
    return SequenceMatcher(None, normalise(a), normalise(b)).ratio()


# ─────────────────────────────────────────────
# Core resolver
# ─────────────────────────────────────────────

class Resolver:
    """
    Resolve raw names to canonical player_ids.

    Usage:
        r = Resolver()
        player_id = r.resolve("Luke Humphries", tour="PDC", source="dartsdatabase")
        # Returns player_id string, or raises ResolutionFailed
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path

    def resolve(
        self,
        raw_name: str,
        tour: str,
        source: str,
        context: Optional[str] = None,
    ) -> str:
        """
        Resolve a raw name to a canonical player_id.

        Returns:
            player_id string if resolved with confidence >= 0.95

        Raises:
            ResolutionQueued  — confidence 0.80–0.94, queued for review
            ResolutionFailed  — confidence < 0.80, cannot resolve
        """
        # Variables to hold deferred raise — must raise AFTER the with block
        # so SQLite commits the writes before the exception propagates.
        _raise_queued = None
        _raise_failed = None

        with get_conn(self.db_path) as conn:

            # 1. Check alias table first (exact match on raw_name + source)
            row = conn.execute(
                "SELECT player_id, confidence, status FROM player_aliases "
                "WHERE raw_name = ? AND source = ?",
                (raw_name, source),
            ).fetchone()

            if row:
                if row["status"] == "ACCEPTED":
                    return row["player_id"]
                elif row["status"] == "REJECTED":
                    _raise_failed = ResolutionFailed(
                        f"Name '{raw_name}' previously REJECTED for source '{source}'"
                    )
                elif row["status"] == "PENDING":
                    _raise_queued = ResolutionQueued(
                        f"Name '{raw_name}' is in review queue (PENDING)"
                    )

            if _raise_failed or _raise_queued:
                pass  # exit with block cleanly so nothing to commit

            else:
                # 2. Try to find best match in players table
                players = conn.execute(
                    "SELECT player_id, full_name FROM players WHERE tour = ?",
                    (tour,),
                ).fetchall()

                if not players:
                    # No players exist yet — derive and create
                    return self._create_new_player(conn, raw_name, tour, source)

                best_id = None
                best_score = 0.0
                for p in players:
                    score = similarity(raw_name, p["full_name"])
                    if score > best_score:
                        best_score = score
                        best_id = p["player_id"]

                # 3. Route by confidence
                if best_score >= CONFIDENCE_AUTO_ACCEPT:
                    self._write_alias(conn, raw_name, best_id, source, best_score, "ACCEPTED")
                    return best_id

                elif best_score >= CONFIDENCE_QUEUE:
                    # Write BEFORE raising — raise after with block commits
                    self._write_alias(conn, raw_name, best_id, source, best_score, "PENDING")
                    self._write_queue(conn, raw_name, best_id, best_score, source, context)
                    _raise_queued = ResolutionQueued(
                        f"Name '{raw_name}' queued for review "
                        f"(best match: {best_id}, confidence: {best_score:.2f})"
                    )

                else:
                    # Could be a new player — derive ID and create
                    derived_id = name_to_player_id(tour, raw_name)
                    existing = conn.execute(
                        "SELECT player_id FROM players WHERE player_id = ?",
                        (derived_id,),
                    ).fetchone()

                    if existing:
                        # ID collision — append numeric suffix
                        i = 2
                        while True:
                            candidate = f"{derived_id}-{i}"
                            if not conn.execute(
                                "SELECT 1 FROM players WHERE player_id = ?", (candidate,)
                            ).fetchone():
                                derived_id = candidate
                                break
                            i += 1

                    return self._create_new_player(
                        conn, raw_name, tour, source, player_id=derived_id
                    )

        # Raise deferred exceptions AFTER with block has committed
        if _raise_queued:
            raise _raise_queued
        if _raise_failed:
            raise _raise_failed

    def _create_new_player(
        self,
        conn: sqlite3.Connection,
        raw_name: str,
        tour: str,
        source: str,
        player_id: Optional[str] = None,
    ) -> str:
        if player_id is None:
            player_id = name_to_player_id(tour, raw_name)

        conn.execute(
            "INSERT OR IGNORE INTO players (player_id, tour, full_name) VALUES (?, ?, ?)",
            (player_id, tour, raw_name),
        )
        self._write_alias(conn, raw_name, player_id, source, 1.0, "ACCEPTED")
        return player_id

    def _write_alias(
        self,
        conn: sqlite3.Connection,
        raw_name: str,
        player_id: str,
        source: str,
        confidence: float,
        status: str,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO player_aliases
               (raw_name, player_id, source, confidence, status)
               VALUES (?, ?, ?, ?, ?)""",
            (raw_name, player_id, source, confidence, status),
        )

    def _write_queue(
        self,
        conn: sqlite3.Connection,
        raw_name: str,
        suggested_id: Optional[str],
        confidence: float,
        source: str,
        context: Optional[str],
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO alias_review_queue
               (raw_name, suggested_id, confidence, source, context)
               VALUES (?, ?, ?, ?, ?)""",
            (raw_name, suggested_id, confidence, source, context),
        )

    def accept_queued(self, raw_name: str, source: str, player_id: str) -> None:
        """Human accepts a queued alias. Updates both queue and alias table."""
        backup("alias_accept")
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE player_aliases SET status='ACCEPTED' "
                "WHERE raw_name=? AND source=?",
                (raw_name, source),
            )
            conn.execute(
                "UPDATE alias_review_queue SET resolved_at=datetime('now'), resolution='ACCEPTED' "
                "WHERE raw_name=? AND source=? AND resolved_at IS NULL",
                (raw_name, source),
            )

    def reject_queued(self, raw_name: str, source: str) -> None:
        """Human rejects a queued alias."""
        backup("alias_reject")
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE player_aliases SET status='REJECTED' "
                "WHERE raw_name=? AND source=?",
                (raw_name, source),
            )
            conn.execute(
                "UPDATE alias_review_queue SET resolved_at=datetime('now'), resolution='REJECTED' "
                "WHERE raw_name=? AND source=? AND resolved_at IS NULL",
                (raw_name, source),
            )

    def pending_queue(self) -> list[dict]:
        """Return all unresolved items in the review queue."""
        with get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM alias_review_queue WHERE resolved_at IS NULL ORDER BY confidence DESC"
            ).fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────

class ResolutionFailed(Exception):
    """Name could not be resolved — do not insert match."""
    pass


class ResolutionQueued(Exception):
    """Name queued for human review — do not insert match yet."""
    pass
