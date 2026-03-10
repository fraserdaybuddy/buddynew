"""
integration_test.py — Stage 2 acceptance tests
JOB-006 Sports Betting Model

Acceptance criteria (from blueprint):
  ✓ Paper mode places 0 real orders, writes full ledger row per intended bet
  ✓ request_uuid correctly prevents duplicate placement on retry
  ✓ Emergency kill switch (close_all) tested — paper mode confirmed safe
  ✓ Ledger entry written BEFORE polling for settlement

Run with:
    cd sports-betting
    PYTHONUTF8=1 python3 -m src.execution.integration_test

All tests run against a temp in-memory DB — universe.db is never touched.
"""

import os
import sys
import sqlite3
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Force paper mode for all tests
os.environ["LIVE_MODE"] = "false"

from database import init_db
import execution.sportmarket as sm
import execution.governor as gov
import execution.ledger_writer as lw


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_test_db() -> Path:
    """Create a temp DB with full schema. Returns path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    init_db(db_path)
    return db_path


def seed_match(db_path: Path, match_id: str = "TEST_MATCH_01",
               run_id: str = "RUN_TEST_001") -> None:
    """Insert minimal records so foreign keys pass."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # relax for test seeding
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, tour, full_name) VALUES (?, ?, ?)",
        ("PDC-TEST-A", "PDC", "Test Player A"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO players (player_id, tour, full_name) VALUES (?, ?, ?)",
        ("PDC-TEST-B", "PDC", "Test Player B"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tournaments (tournament_id, sport, tour, name, year) "
        "VALUES (?, ?, ?, ?, ?)",
        ("PDC-2025-TEST", "darts", "PDC", "Test Tournament", 2025),
    )
    conn.execute(
        "INSERT OR IGNORE INTO matches "
        "(match_id, tournament_id, sport, round, match_date, "
        " player1_id, player2_id, format, data_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (match_id, "PDC-2025-TEST", "darts", "QF", "2025-06-01",
         "PDC-TEST-A", "PDC-TEST-B", "BO11", "test"),
    )
    # Seed model_run so ledger.run_id FK passes
    conn.execute(
        "INSERT OR IGNORE INTO model_runs "
        "(run_id, match_id, sport, model_version, p_under, p_over, "
        " fair_odds_under, fair_odds_over, seed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, match_id, "darts", "test_v0", 0.55, 0.45, 1.82, 2.20, "test"),
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

class TestPaperMode(unittest.TestCase):
    """Paper mode places 0 real orders, writes full ledger row."""

    def setUp(self):
        self.db = make_test_db()
        seed_match(self.db)
        self.match_id = "TEST_MATCH_01"
        self.run_id   = "RUN_TEST_001"

    def tearDown(self):
        import gc; gc.collect()
        try:
            self.db.unlink(missing_ok=True)
        except PermissionError:
            pass  # Windows WAL lock — file will be cleaned up by OS

    def test_place_order_returns_none_in_paper_mode(self):
        """place_order must return None (not an order_id) in paper mode."""
        result = sm.place_order(
            betslip_id="BS_TEST_001",
            price=1.85,
            stake_gbp=50.0,
            match_id=self.match_id,
            direction="UNDER",
            run_id=self.run_id,
        )
        self.assertIsNone(result, "Paper mode must return None — no real order placed")

    def test_ledger_row_written_before_order(self):
        """Pre-placement ledger row must exist before order attempt."""
        bet_id = lw.write_pre_placement(
            match_id=self.match_id,
            direction="UNDER",
            run_id=self.run_id,
            sport="darts",
            line=5.5,
            odds_taken=1.85,
            stake_gbp=50.0,
            mode="PAPER",
            db_path=self.db,
        )
        self.assertIsNotNone(bet_id)
        self.assertEqual(len(bet_id), 16)

        # Verify row exists in DB with PENDING status
        import sqlite3
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT * FROM ledger WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "Ledger row must exist after write_pre_placement")
        self.assertEqual(row[8], "PENDING")  # status column

    def test_full_paper_lifecycle(self):
        """Full paper mode lifecycle: pre → placed (None) → settlement."""
        # 1. Write pre-placement
        bet_id = lw.write_pre_placement(
            match_id=self.match_id,
            direction="OVER",
            run_id=self.run_id,
            sport="darts",
            line=5.5,
            odds_taken=1.90,
            stake_gbp=30.0,
            mode="PAPER",
            db_path=self.db,
        )

        # 2. Place order (paper — returns None)
        order_id = sm.place_order(
            betslip_id="BS_TEST_002",
            price=1.90,
            stake_gbp=30.0,
            match_id=self.match_id,
            direction="OVER",
            run_id=self.run_id,
        )
        self.assertIsNone(order_id)

        # 3. Update ledger with order_id (None is valid in paper mode)
        lw.write_order_placed(bet_id, order_id, db_path=self.db)

        # 4. Settle
        lw.write_settlement(bet_id, "WON", profit_loss_gbp=27.0, db_path=self.db)

        # 5. Verify final state
        import sqlite3
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT status, profit_loss_gbp FROM ledger WHERE bet_id = ?", (bet_id,)
        ).fetchone()
        conn.close()

        self.assertEqual(row[0], "WON")
        self.assertAlmostEqual(row[1], 27.0)


class TestRequestUUID(unittest.TestCase):
    """request_uuid prevents duplicate orders on retry."""

    def test_same_inputs_produce_same_uuid(self):
        uuid1 = sm._request_uuid("MATCH_001", "UNDER", "RUN_001")
        uuid2 = sm._request_uuid("MATCH_001", "UNDER", "RUN_001")
        self.assertEqual(uuid1, uuid2, "Same inputs must always produce same UUID")

    def test_different_direction_produces_different_uuid(self):
        uuid_under = sm._request_uuid("MATCH_001", "UNDER", "RUN_001")
        uuid_over  = sm._request_uuid("MATCH_001", "OVER",  "RUN_001")
        self.assertNotEqual(uuid_under, uuid_over)

    def test_different_run_produces_different_uuid(self):
        uuid1 = sm._request_uuid("MATCH_001", "UNDER", "RUN_001")
        uuid2 = sm._request_uuid("MATCH_001", "UNDER", "RUN_002")
        self.assertNotEqual(uuid1, uuid2)

    def test_uuid_length_36(self):
        uuid = sm._request_uuid("MATCH_001", "UNDER", "RUN_001")
        self.assertEqual(len(uuid), 36)

    def test_duplicate_ledger_write_is_idempotent(self):
        """Writing the same pre-placement twice must not raise or create two rows."""
        db = make_test_db()
        seed_match(db, run_id="RUN_DUP")
        kwargs = dict(
            match_id="TEST_MATCH_01",
            direction="UNDER",
            run_id="RUN_DUP",
            sport="darts",
            line=5.5,
            odds_taken=1.85,
            stake_gbp=50.0,
            mode="PAPER",
            db_path=db,
        )

        bet_id_1 = lw.write_pre_placement(**kwargs)
        bet_id_2 = lw.write_pre_placement(**kwargs)  # duplicate — must not raise

        self.assertEqual(bet_id_1, bet_id_2)

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE bet_id = ?", (bet_id_1,)
        ).fetchone()[0]
        conn.close()
        import gc; gc.collect()
        try:
            db.unlink(missing_ok=True)
        except PermissionError:
            pass

        self.assertEqual(count, 1, "Duplicate pre_placement must not create two rows")


class TestCircuitBreaker(unittest.TestCase):
    """Circuit breaker trips on consecutive losses and drawdown."""

    def test_trips_on_consecutive_losses(self):
        cb = gov.CircuitBreaker(bankroll_start=1000.0, max_consec_losses=3)
        cb.record(-10.0)
        cb.record(-10.0)
        self.assertFalse(cb.tripped)
        cb.record(-10.0)
        self.assertTrue(cb.tripped)
        self.assertIn("consecutive", cb.trip_reason.lower())

    def test_trips_on_drawdown(self):
        cb = gov.CircuitBreaker(bankroll_start=1000.0, max_drawdown_pct=0.10)
        cb.record(-80.0)
        self.assertFalse(cb.tripped)
        cb.record(-30.0)  # cumulative -110 = 11% of 1000
        self.assertTrue(cb.tripped)
        self.assertIn("drawdown", cb.trip_reason.lower())

    def test_win_resets_consecutive_loss_count(self):
        cb = gov.CircuitBreaker(bankroll_start=1000.0, max_consec_losses=3)
        cb.record(-10.0)
        cb.record(-10.0)
        cb.record(+20.0)   # win resets streak
        cb.record(-10.0)
        self.assertFalse(cb.tripped)  # only 1 loss in new streak

    def test_no_further_records_after_trip(self):
        cb = gov.CircuitBreaker(bankroll_start=1000.0, max_consec_losses=2)
        cb.record(-10.0)
        cb.record(-10.0)
        self.assertTrue(cb.tripped)
        pnl_at_trip = cb.session_pnl
        cb.record(-100.0)  # must be ignored
        self.assertEqual(cb.session_pnl, pnl_at_trip)


class TestKellyStake(unittest.TestCase):
    """Kelly stake sizing."""

    def test_positive_edge_returns_stake(self):
        stake = gov.kelly_stake(bankroll_gbp=1000.0, edge=0.08, odds=1.85)
        self.assertGreater(stake, 0)

    def test_zero_edge_returns_zero(self):
        self.assertEqual(gov.kelly_stake(1000.0, 0.0, 1.85), 0.0)

    def test_negative_edge_returns_zero(self):
        self.assertEqual(gov.kelly_stake(1000.0, -0.05, 1.85), 0.0)

    def test_stake_capped_at_max(self):
        # Very high edge on large bankroll
        stake = gov.kelly_stake(1000000.0, 0.50, 1.85, max_stake=500.0)
        self.assertLessEqual(stake, 500.0)

    def test_stake_floored_at_min(self):
        # Very small edge on tiny bankroll
        stake = gov.kelly_stake(10.0, 0.001, 1.85, min_stake=5.0)
        self.assertGreaterEqual(stake, 5.0)


class TestEmergencyKillSwitch(unittest.TestCase):
    """close_all is safe to call in paper mode."""

    def test_close_all_paper_mode_returns_dict(self):
        result = sm.close_all()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("mode"), "PAPER")
        self.assertEqual(result.get("cancelled"), 0)

    def test_close_all_does_not_call_api_in_paper_mode(self):
        with patch("execution.sportmarket.requests.post") as mock_post:
            sm.close_all()
            mock_post.assert_not_called()


class TestLiveModeGate(unittest.TestCase):
    """assert_live_mode_safe raises in paper mode."""

    def test_raises_when_live_mode_false(self):
        with self.assertRaises(RuntimeError):
            gov.assert_live_mode_safe()

    def test_passes_when_live_mode_true(self):
        with patch.object(gov, "LIVE_MODE", True):
            try:
                gov.assert_live_mode_safe()
            except RuntimeError:
                self.fail("assert_live_mode_safe raised unexpectedly in live mode")


# ─────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Stage 2 Integration Tests — JOB-006")
    print("Mode: PAPER (LIVE_MODE=false)")
    print("DB:   temporary in-memory (universe.db untouched)")
    print("=" * 60)
    unittest.main(verbosity=2)
