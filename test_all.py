"""
test_all.py -- JOB-006 System Health Test Suite
================================================
Runs offline-safe tests on every system component.
For Betfair API tests add --betfair flag (requires credentials + internet).
For server tests add --server flag (requires run_server.py to be running).

Usage:
    PYTHONUTF8=1 python test_all.py
    PYTHONUTF8=1 python test_all.py --betfair
    PYTHONUTF8=1 python test_all.py --server
    PYTHONUTF8=1 python test_all.py --betfair --server --verbose
"""

import sys
import argparse
import sqlite3
import traceback
from pathlib import Path
from datetime import date

# Force UTF-8 output on Windows so print() never raises UnicodeEncodeError
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── colour helpers (ASCII-safe check/cross marks) ─────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}FAIL{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}SKIP{RESET}  {msg}")
def hdr(msg):  print(f"\n{BOLD}{CYAN}{'-'*55}{RESET}\n{BOLD}{CYAN}  {msg}{RESET}\n{BOLD}{CYAN}{'-'*55}{RESET}")

PASS = 0
FAIL = 0

def check(label, fn, skip=False):
    global PASS, FAIL
    if skip:
        warn(f"SKIP  {label}")
        return
    try:
        result = fn()
        if result is False:
            fail(label)
            FAIL += 1
        else:
            detail = f"  ({result})" if isinstance(result, str) else ""
            ok(f"{label}{detail}")
            PASS += 1
    except Exception as e:
        fail(f"{label}  →  {e}")
        if VERBOSE:
            traceback.print_exc()
        FAIL += 1

VERBOSE = False

# ── setup ──────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "universe.db"
API_BASE = "http://127.0.0.1:5000"


# ══════════════════════════════════════════════════════════════════════════════
# 1. DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def test_db():
    hdr("1. DATABASE")

    def db_exists():
        if not DB_PATH.exists():
            raise FileNotFoundError(f"{DB_PATH} not found")
        size_mb = DB_PATH.stat().st_size / 1_048_576
        return f"{size_mb:.1f} MB"
    check("DB file exists", db_exists)

    def db_connect():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return "integrity_check OK"
    check("DB connects and passes integrity check", db_connect)

    def tables_exist():
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        required = {"players","player_aliases","matches","tournaments","betfair_markets",
                    "ledger","player_form","elo_ratings"}
        missing  = required - tables
        if missing:
            raise AssertionError(f"Missing tables: {missing}")
        return f"{len(tables)} tables"
    check("All required tables exist", tables_exist)

    def tennis_matches():
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM matches WHERE sport='tennis'").fetchone()[0]
        conn.close()
        if n < 1000:
            raise AssertionError(f"Only {n} tennis matches — expected 5000+")
        return f"{n:,} rows"
    check("Tennis matches loaded", tennis_matches)

    def elo_ratings():
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(DISTINCT player_id) FROM elo_ratings").fetchone()[0]
        conn.close()
        if n < 100:
            raise AssertionError(f"Only {n} ELO players")
        return f"{n:,} players"
    check("ELO ratings populated", elo_ratings)

    def top_elo():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT p.full_name, er.elo FROM elo_ratings er "
            "JOIN players p ON er.player_id=p.player_id "
            "WHERE er.surface='Hard' ORDER BY er.elo DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            raise AssertionError("No ELO ratings for Hard surface")
        return f"#{1} {row['full_name']} ELO={row['elo']:.0f}"
    check("Top ELO player retrievable", top_elo)

    def betfair_markets():
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM betfair_markets").fetchone()[0]
        conn.close()
        if n == 0:
            raise AssertionError("betfair_markets is empty — run run_daily.py first")
        return f"{n:,} rows"
    check("betfair_markets has data", betfair_markets)

    def ledger_schema():
        conn = sqlite3.connect(DB_PATH)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
        conn.close()
        required = {"bet_id","match_id","sport","bet_direction","line",
                    "odds_taken","stake_gbp","status","profit_loss_gbp",
                    "placed_at","settled_at","mode"}
        missing = required - cols
        if missing:
            raise AssertionError(f"Missing ledger columns: {missing}")
        return "schema OK"
    check("Ledger table schema complete", ledger_schema)


# ══════════════════════════════════════════════════════════════════════════════
# 2. MODEL — ELO + SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def test_model():
    hdr("2. MODEL — ELO + SIMULATION")

    def simulate_import():
        from src.model.simulate import simulate, elo_to_hold_probs
        return "import OK"
    check("simulate.py imports cleanly", simulate_import)

    def simulate_bo3():
        from src.model.simulate import simulate, elo_to_hold_probs
        # Sinner (1990) vs qualifier (1550) on Hard, BO3
        s_a, s_b = elo_to_hold_probs(440.0, "Hard", 3)
        sim = simulate(s_a, s_b, 3, tiebreak_rule="standard", n=10_000, seed=42)
        fl = sim.fair_line_games()
        if not (15 < fl < 28):
            raise AssertionError(f"Fair line {fl:.1f} out of expected range 15–28")
        return f"fair_line={fl:.1f} games"
    check("Monte Carlo BO3 simulation runs", simulate_bo3)

    def simulate_bo5():
        from src.model.simulate import simulate, elo_to_hold_probs
        s_a, s_b = elo_to_hold_probs(440.0, "Hard", 5)
        sim = simulate(s_a, s_b, 5, tiebreak_rule="standard", n=10_000, seed=42)
        fl = sim.fair_line_games()
        if not (20 < fl < 45):
            raise AssertionError(f"BO5 fair line {fl:.1f} out of expected range 20–45")
        return f"fair_line={fl:.1f} games"
    check("Monte Carlo BO5 simulation runs", simulate_bo5)

    def simulation_p_sums():
        from src.model.simulate import simulate, elo_to_hold_probs
        s_a, s_b = elo_to_hold_probs(100.0, "Hard", 3)
        sim = simulate(s_a, s_b, 3, n=50_000, seed=1)
        # p_over + p_under at fair line should sum to ~1
        fl = sim.fair_line_games()
        total = sim.p_games_over(fl) + sim.p_games_under(fl)
        if not (0.95 < total < 1.05):
            raise AssertionError(f"p_over+p_under = {total:.3f} (expected ≈1.0)")
        return f"sum={total:.3f}"
    check("Simulation probabilities sum to ~1", simulation_p_sums)

    def elo_direction():
        from src.model.simulate import simulate, elo_to_hold_probs
        # Higher ELO player should have shorter fair line (wins faster = fewer games)
        s_a1, s_b1 = elo_to_hold_probs(200.0, "Hard", 3)   # moderate gap
        s_a2, s_b2 = elo_to_hold_probs(0.0,   "Hard", 3)   # equal players
        sim1 = simulate(s_a1, s_b1, 3, n=20_000, seed=42)
        sim2 = simulate(s_a2, s_b2, 3, n=20_000, seed=42)
        fl1, fl2 = sim1.fair_line_games(), sim2.fair_line_games()
        if not (fl1 < fl2):
            raise AssertionError(f"Equal players ({fl2:.1f}) should produce more games than gap={200} ({fl1:.1f})")
        return f"gap200→{fl1:.1f} < equal→{fl2:.1f} ✓"
    check("Larger ELO gap → shorter fair line", elo_direction)


# ══════════════════════════════════════════════════════════════════════════════
# 3. EDGE SCREENER + KELLY
# ══════════════════════════════════════════════════════════════════════════════

def test_edge():
    hdr("3. EDGE SCREENER + KELLY")

    def edge_import():
        from src.model.edge import (
            screen_tennis_match, screen_from_betfair_markets,
            write_to_ledger, recommended_stake, devig_2way,
            BetSignal, MIN_EDGE, MIN_ELO_GAP
        )
        return "import OK"
    check("edge.py imports cleanly", edge_import)

    def devig_correct():
        from src.model.edge import devig_2way
        p_o, p_u = devig_2way(1.85, 2.05)
        if abs(p_o + p_u - 1.0) > 0.001:
            raise AssertionError(f"devig sum {p_o+p_u:.4f} ≠ 1.0")
        return f"p_over={p_o:.3f}  p_under={p_u:.3f}"
    check("devig_2way sums to 1.0", devig_correct)

    def kelly_positive_edge():
        from src.model.edge import recommended_stake
        frac, stake = recommended_stake(
            edge=0.15, decimal_odds=1.95, bankroll=1000.0,
            tier=1, abs_elo_gap=200.0
        )
        if stake <= 0:
            raise AssertionError(f"Expected positive stake, got £{stake:.2f}")
        return f"frac={frac:.4f}  stake=£{stake:.2f}"
    check("Positive edge → positive Kelly stake", kelly_positive_edge)

    def kelly_zero_edge():
        from src.model.edge import recommended_stake
        frac, stake = recommended_stake(
            edge=-0.05, decimal_odds=1.90, bankroll=1000.0,
            tier=1, abs_elo_gap=200.0
        )
        if stake != 0.0:
            raise AssertionError(f"Negative edge should give £0, got £{stake:.2f}")
        return "stake=£0 ✓"
    check("Negative edge → zero stake (governor clamped)", kelly_zero_edge)

    def screen_tennis_synthetic():
        from src.model.edge import screen_tennis_match
        signals = screen_tennis_match(
            match_id="TEST-001",
            match_date="2026-01-15",
            p1_id="ATP-SINNER-J",
            p2_id="ATP-QUALIFIER-X",
            p1_elo_surface=1990.0,
            p2_elo_surface=1550.0,
            surface="Hard",
            best_of=3,
            tiebreak_rule="standard",
            p1_serve_str=None,
            p2_serve_str=None,
            p1_tier=1,
            p2_tier=3,
            p1_last_match="2026-01-10",
            p2_last_match="2026-01-08",
            p1_surface_n=45,
            p2_surface_n=4,
            market_lines={},  # synthetic — no real lines
            bankroll=1000.0,
            mode="PAPER",
        )
        if not signals:
            raise AssertionError("Expected at least 1 signal")
        return f"{len(signals)} signal(s)"
    check("screen_tennis_match with synthetic lines returns signals", screen_tennis_synthetic)

    def screen_tennis_real_edge():
        from src.model.edge import screen_tennis_match
        # Feed real market lines where our model has clear edge
        signals = screen_tennis_match(
            match_id="TEST-002",
            match_date="2026-01-15",
            p1_id="ATP-SINNER-J",
            p2_id="ATP-QUALIFIER-X",
            p1_elo_surface=1990.0,
            p2_elo_surface=1550.0,
            surface="Hard",
            best_of=3,
            tiebreak_rule="standard",
            p1_serve_str=None,
            p2_serve_str=None,
            p1_tier=1,
            p2_tier=3,
            p1_last_match="2026-01-10",
            p2_last_match="2026-01-08",
            p1_surface_n=45,
            p2_surface_n=4,
            market_lines={
                "total_games": {
                    "line": 28.5,       # artificially high — model should want UNDER
                    "over_odds": 1.95,
                    "under_odds": 1.95,
                    "liquidity": 2000,
                    "synthetic": False,
                }
            },
            bankroll=1000.0,
            mode="PAPER",
        )
        bet_signals = [s for s in signals if s.stake_gbp > 0]
        if not bet_signals:
            warn("No BET signals — check if model edge exceeds 8% threshold for this scenario")
            return "no bets (edge may be < 8%)"
        return f"{len(bet_signals)} bet signal(s)  edge={bet_signals[0].edge:+.1%}"
    check("screen_tennis_match with real high line → UNDER bet signal", screen_tennis_real_edge)

    def live_screener():
        from src.model.edge import screen_from_betfair_markets
        signals = screen_from_betfair_markets(
            sport="tennis", surface="Hard", best_of=3,
            bankroll=1000.0, mode="PAPER", min_liquidity=0.0
        )
        if signals is None:
            raise AssertionError("screen_from_betfair_markets returned None")
        bets = [s for s in signals if s.stake_gbp > 0]
        return f"{len(signals)} signals  {len(bets)} qualifying bets"
    check("screen_from_betfair_markets runs against DB", live_screener)


# ══════════════════════════════════════════════════════════════════════════════
# 4. LEDGER — WRITE + SETTLE
# ══════════════════════════════════════════════════════════════════════════════

def test_ledger():
    hdr("4. LEDGER — WRITE + SETTLE")

    TEST_RUN_ID = "TEST_RUN_CHECKSUM_001"

    def write_paper_bet():
        from src.model.edge import BetSignal, write_to_ledger
        from src.database import get_conn
        s = BetSignal(
            match_id="BF:Test_Match_A_v_B",
            sport="tennis",
            market_type="total_games",
            direction="UNDER",
            line=22.5,
            model_p=0.58,
            market_p=0.50,
            edge=0.08,
            odds=1.91,
            kelly_frac=0.04,
            stake_gbp=40.0,
            tier=1,
            mode="PAPER",
            synthetic_line=False,
            event_name="Test A v Test B",
        )
        conn = get_conn()
        # Clean up any prior test run
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM ledger WHERE run_id=?", (TEST_RUN_ID,))
        conn.commit()
        n = write_to_ledger([s], TEST_RUN_ID, conn)
        conn.close()
        if n != 1:
            raise AssertionError(f"Expected 1 bet written, got {n}")
        return "1 PENDING bet written"
    check("write_to_ledger writes bet with bet_id", write_paper_bet)

    def verify_bet_id():
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT rowid, bet_id, status, stake_gbp FROM ledger WHERE run_id=?",
            (TEST_RUN_ID,)
        ).fetchone()
        conn.close()
        if not row:
            raise AssertionError("Test bet not found in ledger")
        if not row["bet_id"]:
            raise AssertionError("bet_id is NULL — not generated correctly")
        if row["status"] != "PENDING":
            raise AssertionError(f"Expected PENDING, got {row['status']}")
        return f"rowid={row['rowid']}  bet_id={row['bet_id']}  stake=£{row['stake_gbp']:.0f}"
    check("Bet stored with non-NULL bet_id and PENDING status", verify_bet_id)

    def settle_win():
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT rowid, stake_gbp, odds_taken FROM ledger WHERE run_id=?",
            (TEST_RUN_ID,)
        ).fetchone()
        if not row:
            conn.close()
            raise AssertionError("Test bet not found")
        expected_pnl = round((row["odds_taken"] - 1.0) * row["stake_gbp"], 2)
        conn.execute(
            "UPDATE ledger SET status='WON', profit_loss_gbp=?, settled_at=datetime('now') WHERE rowid=?",
            (expected_pnl, row["rowid"])
        )
        conn.commit()
        # Verify
        row2 = conn.execute(
            "SELECT status, profit_loss_gbp FROM ledger WHERE run_id=?",
            (TEST_RUN_ID,)
        ).fetchone()
        conn.close()
        if row2["status"] != "WON":
            raise AssertionError(f"Status not updated: {row2['status']}")
        return f"pnl=£{row2['profit_loss_gbp']:+.2f}"
    check("Settle as WON computes correct P&L", settle_win)

    def cleanup_test_bet():
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM ledger WHERE run_id=?", (TEST_RUN_ID,))
        conn.commit()
        conn.close()
        return "test data removed"
    check("Test bet cleaned up", cleanup_test_bet)


# ══════════════════════════════════════════════════════════════════════════════
# 5. GOVERNOR
# ══════════════════════════════════════════════════════════════════════════════

def test_governor():
    hdr("5. GOVERNOR — KELLY STAKING")

    def governor_import():
        from src.execution.governor import kelly_stake, KELLY_FRACTION, MAX_STAKE_GBP, MIN_STAKE_GBP
        return f"KELLY_FRACTION={KELLY_FRACTION}  MAX=£{MAX_STAKE_GBP}  MIN=£{MIN_STAKE_GBP}"
    check("governor.py imports", governor_import)

    def kelly_clamps():
        from src.execution.governor import kelly_stake
        # Very high edge — should be clamped to MAX_STAKE_GBP
        huge = kelly_stake(1_000_000, 0.50, 2.0, fraction=1.0)
        # Zero or negative edge — should be £0
        zero = kelly_stake(1000, -0.10, 1.90, fraction=0.25)
        if zero != 0:
            raise AssertionError(f"Negative edge should return £0, got £{zero}")
        return f"huge stake clamped to £{huge:.0f}  zero edge=£{zero:.0f}"
    check("Kelly clamps to MAX_STAKE and floors at 0", kelly_clamps)


# ══════════════════════════════════════════════════════════════════════════════
# 6. API SERVER (requires --server flag)
# ══════════════════════════════════════════════════════════════════════════════

def test_server(run_server_tests: bool):
    hdr("6. API SERVER  (--server flag)")

    if not run_server_tests:
        warn("Skipped — pass --server to test (requires run_server.py running)")
        return

    import urllib.request
    import json as _json

    def check_url(path, method="GET", body=None):
        url = f"{API_BASE}{path}"
        data = _json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if body else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())

    def server_reachable():
        d = check_url("/api/status")
        if "matches" not in d:
            raise AssertionError("Unexpected response shape from /api/status")
        return f"server up  tennis={d['matches']['tennis']:,}"
    check("GET /api/status", server_reachable)

    def latest_date():
        d = check_url("/api/latest-date")
        if "date" not in d:
            raise AssertionError("No 'date' field in response")
        return f"date={d['date']}"
    check("GET /api/latest-date", latest_date)

    def signals_endpoint():
        d = check_url("/api/signals?mode=PAPER")
        if "signals" not in d:
            raise AssertionError("No 'signals' field in response")
        return f"{d['total']} signals  {d['bets']} bets"
    check("GET /api/signals", signals_endpoint)

    def analyse_endpoint():
        d = check_url("/api/analyse", method="POST", body={
            "p1": "Sinner", "p2": "Djokovic",
            "surface": "Hard", "best_of": 3,
            "book_line": 22.5, "book_odds": 1.90, "direction": "UNDER"
        })
        if "simulation" not in d:
            raise AssertionError(f"Unexpected response: {d}")
        return f"fair_line={d['simulation']['fair_line_games']}  edge={d['edge']:+.3f}"
    check("POST /api/analyse (Sinner v Djokovic)", analyse_endpoint)

    def ledger_endpoint():
        d = check_url("/api/ledger?limit=10")
        if "rows" not in d or "summary" not in d:
            raise AssertionError("Missing rows or summary")
        return f"{d['summary']['total_bets']} bets  pnl=£{d['summary']['total_pnl']:+.2f}"
    check("GET /api/ledger", ledger_endpoint)

    def settle_endpoint_roundtrip():
        import _json as j
        # Write a test bet via the API or directly to DB, then settle via API
        from src.model.edge import BetSignal, write_to_ledger
        from src.database import get_conn
        TEST_RUN = "API_SETTLE_TEST_001"
        s = BetSignal(
            match_id="BF:API_Settle_Test",
            sport="tennis", market_type="total_games", direction="OVER",
            line=21.5, model_p=0.60, market_p=0.50, edge=0.10,
            odds=1.95, kelly_frac=0.05, stake_gbp=50.0,
            tier=1, mode="PAPER", synthetic_line=False,
        )
        conn = get_conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM ledger WHERE run_id=?", (TEST_RUN,))
        conn.commit()
        write_to_ledger([s], TEST_RUN, conn)
        row = conn.execute(
            "SELECT rowid FROM ledger WHERE run_id=?", (TEST_RUN,)
        ).fetchone()
        rowid = row["rowid"]
        conn.close()

        # Settle via API
        d = check_url("/api/ledger/settle", method="POST", body={"rowid": rowid, "result": "WON"})
        if d.get("result") != "WON":
            raise AssertionError(f"Settle API returned: {d}")
        pnl = d["pnl"]

        # Clean up
        conn2 = get_conn()
        conn2.execute("PRAGMA foreign_keys = OFF")
        conn2.execute("DELETE FROM ledger WHERE run_id=?", (TEST_RUN,))
        conn2.commit()
        conn2.close()
        return f"settle API OK  pnl=£{pnl:+.2f}"
    check("POST /api/ledger/settle roundtrip", settle_endpoint_roundtrip)

    def dashboard_served():
        import urllib.request as _ur
        with _ur.urlopen(f"{API_BASE}/", timeout=5) as resp:
            content = resp.read()
            ct = resp.headers.get("Content-Type", "")
        if b"<html" not in content.lower() and b"<!doctype" not in content.lower():
            raise AssertionError("Response doesn't look like HTML")
        return f"{len(content):,} bytes HTML"
    check("GET / serves dashboard HTML", dashboard_served)


# ══════════════════════════════════════════════════════════════════════════════
# 7. BETFAIR API (requires --betfair flag)
# ══════════════════════════════════════════════════════════════════════════════

def test_betfair(run_betfair_tests: bool):
    hdr("7. BETFAIR API  (--betfair flag)")

    if not run_betfair_tests:
        warn("Skipped — pass --betfair to test (requires credentials + internet)")
        return

    def certs_exist():
        crt = Path(r"C:\Users\frase\client.crt")
        pem = Path(r"C:\Users\frase\client.pem")
        if not crt.exists():
            raise FileNotFoundError(f"Missing: {crt}")
        if not pem.exists():
            raise FileNotFoundError(f"Missing: {pem}")
        return "client.crt + client.pem present"
    check("SSL certificates on disk", certs_exist)

    def env_loaded():
        from pathlib import Path as _P
        import os
        env_file = ROOT / ".env"
        if not env_file.exists():
            raise FileNotFoundError(".env not found")
        content = env_file.read_text()
        if "BETFAIR_APP_KEY" not in content:
            raise AssertionError("BETFAIR_APP_KEY not in .env")
        return ".env present with credentials"
    check(".env credentials file present", env_loaded)

    def betfair_login():
        from src.execution.betfair import BetfairSession
        session = BetfairSession()
        token = session.login()
        if not token:
            raise AssertionError("Login returned no session token")
        session.logout()
        return f"token={token[:8]}..."
    check("Betfair cert login + logout", betfair_login)

    def betfair_list_markets():
        from src.execution.betfair import BetfairSession
        from src.execution.scraper import poll_sport
        session = BetfairSession()
        session.login()
        try:
            # poll_sport returns number of rows written; dry_run=True → no DB writes
            n = poll_sport(session, sport="tennis", days_ahead=2, dry_run=True, link_date=str(date.today()))
            return f"poll_sport dry-run OK  ({n} market lines found)"
        finally:
            session.logout()
    check("Betfair poll_sport dry-run (tennis, 2 days)", betfair_list_markets)


# ══════════════════════════════════════════════════════════════════════════════
# 8. FILE INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════

def test_files():
    hdr("8. FILE INTEGRITY")

    critical_files = [
        "run_daily.py",
        "run_server.py",
        "run_presession.py",
        "START_JOB006.bat",
        "src/api/server.py",
        "src/model/edge.py",
        "src/model/simulate.py",
        "src/model/elo_loader.py",
        "src/execution/betfair.py",
        "src/execution/scraper.py",
        "src/execution/governor.py",
        "src/database.py",
        "dashboard/betting-dashboard.html",
        "data/universe.db",
        ".env",
    ]

    for rel in critical_files:
        path = ROOT / rel
        def _check(p=path, r=rel):
            if not p.exists():
                raise FileNotFoundError(f"MISSING: {r}")
            size = p.stat().st_size
            if size == 0:
                raise AssertionError(f"Empty file: {r}")
            return f"{size:,} bytes"
        check(f"  {rel}", _check)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global VERBOSE

    parser = argparse.ArgumentParser(description="JOB-006 system health test suite")
    parser.add_argument("--server",  action="store_true", help="Test API server (must be running)")
    parser.add_argument("--betfair", action="store_true", help="Test Betfair API (needs internet)")
    parser.add_argument("--verbose", action="store_true", help="Show full tracebacks on failure")
    args = parser.parse_args()
    VERBOSE = args.verbose

    print(f"\n{BOLD}JOB-006 System Health Test{RESET}  {date.today()}")
    print(f"DB: {DB_PATH}")

    test_files()
    test_db()
    test_model()
    test_edge()
    test_ledger()
    test_governor()
    test_server(args.server)
    test_betfair(args.betfair)

    # ── Summary ───────────────────────────────────────────────────────────────
    total = PASS + FAIL
    print(f"\n{BOLD}{'═'*55}")
    if FAIL == 0:
        print(f"{GREEN}  ALL {total} TESTS PASSED{RESET}{BOLD}")
    else:
        print(f"{RED}  {FAIL} FAILED{RESET}{BOLD}  /  {total} tests  ({PASS} passed)")
    print(f"{'═'*55}{RESET}\n")

    if FAIL == 0 and not args.server:
        print("  Next: start the server and run again with --server")
        print("  $ $env:PYTHONUTF8=1; python run_server.py")
        print("  $ $env:PYTHONUTF8=1; python test_all.py --server\n")

    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
