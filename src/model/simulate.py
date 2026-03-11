"""
simulate.py — Monte Carlo tennis match simulation
JOB-006 Sports Betting Model

Input: p_hold_A, p_hold_B (derived from serve/return data + ELO adjustment),
       best_of (3 or 5), tiebreak_rule ('standard' | 'advantage')

Output: full PMF over sets played and total games played (10k simulations)

Architecture:
  1. sim_game(p_serve_wins) → bool (server wins game)
  2. sim_set(s_a, s_b, ...) → (ga, gb, a_won)
  3. sim_match(s_a, s_b, best_of, tiebreak_rule) → (sets_played, total_games, a_won)
  4. simulate(s_a, s_b, best_of, tiebreak_rule, n=10000) → SimResult

ELO to hold probability conversion:
  elo_to_p_match(elo_gap) → p_match using standard ELO formula
  p_match_to_p_hold(p_match, s_baseline) → (s_a, s_b) via scaling

Hold probability baseline from data:
  Hard:  s_baseline ≈ 0.63 (ATP), 0.57 (WTA)
  Clay:  s_baseline ≈ 0.60 (ATP), 0.55 (WTA)
  Grass: s_baseline ≈ 0.68 (ATP), 0.62 (WTA)

Tiebreak rules:
  'standard'   — tiebreak at 6-6 in all sets (US Open, AO, ATP regular)
  'advantage'  — no tiebreak in deciding set (Roland Garros 3rd/5th set)
  'tb10'       — 10-point match tiebreak in deciding set (AO 3rd set WTA, etc.)

Roland Garros advantage set:
  Deciding set played as advantage set (no tiebreak) — fat OVER tail.
  Implemented as: keep playing at 6-6 in deciding set until 2-game gap.
"""

import random
import numpy as np
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

TiebreaRule = Literal["standard", "advantage", "tb10"]


# ---------------------------------------------------------------------------
# Baseline hold probabilities by surface / tour
# ---------------------------------------------------------------------------

# (ATP/WTA combined baseline from 2023-2024 ATP data)
HOLD_BASELINE = {
    "Hard":  0.625,
    "Clay":  0.600,
    "Grass": 0.675,
}


# ---------------------------------------------------------------------------
# ELO helpers
# ---------------------------------------------------------------------------

def elo_to_p_match(elo_gap: float) -> float:
    """
    Convert ELO gap (p1_elo - p2_elo) to P(player1 wins match).
    Standard ELO formula.
    """
    return 1.0 / (1.0 + 10.0 ** (-elo_gap / 400.0))


def _p_set_from_p_match_bo3(p_set: float) -> float:
    """P(match win in BO3) given p(set win)."""
    return p_set ** 2 * (3.0 - 2.0 * p_set)


def _p_set_from_p_match_bo5(p_set: float) -> float:
    """P(match win in BO5) given p(set win).
    P(win 3-0) + P(win 3-1) + P(win 3-2)
    = p^3 + C(3,1)*p^3*q + C(4,2)*p^3*q^2
    = p^3*(1 + 3q + 6q^2)
    """
    q = 1.0 - p_set
    return p_set ** 3 * (1.0 + 3.0 * q + 6.0 * q ** 2)


def p_match_to_p_set(p_match: float, best_of: int = 3) -> float:
    """
    Numerically invert P(match) → P(set win) using bisection.
    """
    if p_match <= 0.0:
        return 0.0
    if p_match >= 1.0:
        return 1.0

    fn = _p_set_from_p_match_bo3 if best_of == 3 else _p_set_from_p_match_bo5
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if fn(mid) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def elo_to_hold_probs(
    elo_gap: float,
    surface: str = "Hard",
    best_of: int = 3,
) -> tuple:
    """
    Convert ELO gap to (s_A, s_B): hold probabilities for each player.

    Method:
    1. ELO gap → p_match_A
    2. p_match_A → p_set_A (via inversion)
    3. p_set_A encodes the serve/return balance. Allocate symmetrically
       around the surface baseline:
         s_A = baseline + delta, s_B = baseline - delta
       where delta is chosen such that p_set_A matches target.
    4. Approximate: use p_set as a scaling factor on hold baseline.

    This is an approximation — for precise calibration, supply
    actual s_A / s_B from serve/return stats.
    """
    baseline = HOLD_BASELINE.get(surface, 0.625)
    p_match = elo_to_p_match(elo_gap)
    p_set = p_match_to_p_set(p_match, best_of)

    # Scale: p_set ≈ 0.5 → both equal, p_set > 0.5 → A stronger
    # Shift hold probs symmetrically
    # Calibrated from data: each 0.01 difference in hold → ~0.02 difference in p_set
    target_delta = (p_set - 0.5) * 0.5  # rough linear approximation
    s_a = np.clip(baseline + target_delta, 0.3, 0.95)
    s_b = np.clip(baseline - target_delta, 0.3, 0.95)
    return float(s_a), float(s_b)


# ---------------------------------------------------------------------------
# Core simulation: game → set → match
# ---------------------------------------------------------------------------

def sim_game(p_serve_wins: float) -> bool:
    """Simulate a single tennis game. Returns True if server wins."""
    return random.random() < p_serve_wins


def sim_set(
    s_a: float,
    s_b: float,
    advantage_set: bool = False,
    server_a_first: bool = True,
) -> tuple:
    """
    Simulate a single tennis set.

    Args:
        s_a           — P(A wins game when A serves)
        s_b           — P(B wins game when B serves)
        advantage_set — True = play out set with 2-game gap beyond 6-6
                        False = tiebreak at 6-6 (7-6 result)
        server_a_first — which player serves first in the set

    Returns:
        (games_a, games_b, a_won_set)
    """
    ga = 0
    gb = 0
    a_serves = server_a_first

    while True:
        if a_serves:
            a_wins = random.random() < s_a
        else:
            a_wins = random.random() > s_b  # A wins game on B's serve if B doesn't hold

        if a_wins:
            ga += 1
        else:
            gb += 1

        # Check set over
        lead = ga - gb
        max_g = max(ga, gb)

        if max_g >= 6 and abs(lead) >= 2:
            return (ga, gb, ga > gb)

        if ga == 6 and gb == 6:
            if advantage_set:
                # Keep playing — 2 game gap required
                pass
            else:
                # Tiebreak: use composite prob
                p_tb = (s_a + (1.0 - s_b)) / 2.0
                if random.random() < p_tb:
                    return (7, 6, True)
                else:
                    return (6, 7, False)

        # Switch server after each game
        a_serves = not a_serves


def sim_match(
    s_a: float,
    s_b: float,
    best_of: int = 3,
    tiebreak_rule: TiebreaRule = "standard",
) -> tuple:
    """
    Simulate a full match.

    Args:
        s_a, s_b     — hold probabilities
        best_of      — 3 or 5
        tiebreak_rule — 'standard' | 'advantage' | 'tb10'
                       'advantage' means deciding set is advantage set
                       'tb10' means deciding set is a 10-point match tiebreak
                       (treated as 1 game in accounting, rarely used in model)

    Returns:
        (sets_a, sets_b, total_games, a_won)
    """
    sets_to_win = (best_of + 1) // 2  # 2 for BO3, 3 for BO5
    sets_a = 0
    sets_b = 0
    total_games = 0
    server_a_first = True  # first set A serves first (simplified)

    while sets_a < sets_to_win and sets_b < sets_to_win:
        # Determine if this is the deciding set
        is_deciding = (sets_a == sets_to_win - 1 and sets_b == sets_to_win - 1)

        if tiebreak_rule == "advantage" and is_deciding:
            adv_set = True
        elif tiebreak_rule == "tb10" and is_deciding:
            # Treat deciding set as a regular set with tiebreak for simplicity
            adv_set = False
        else:
            adv_set = False

        ga, gb, a_won = sim_set(s_a, s_b, advantage_set=adv_set,
                                server_a_first=server_a_first)
        total_games += ga + gb

        if a_won:
            sets_a += 1
        else:
            sets_b += 1

        # Alternate who serves first each set (simplified)
        server_a_first = not server_a_first

    sets_played = sets_a + sets_b
    return (sets_a, sets_b, sets_played, total_games, sets_a > sets_b)


# ---------------------------------------------------------------------------
# Simulation result container
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    n: int
    best_of: int
    # Sets played distribution
    sets_pmf: dict = field(default_factory=dict)     # {n_sets: probability}
    # Total games distribution
    games_mean: float = 0.0
    games_std: float = 0.0
    games_median: float = 0.0
    games_p10: float = 0.0
    games_p25: float = 0.0
    games_p75: float = 0.0
    games_p90: float = 0.0
    # P(A wins match)
    p_a_wins: float = 0.0
    # Raw arrays (for custom quantile queries)
    _games_array: list = field(default_factory=list, repr=False)

    def p_games_over(self, line: float) -> float:
        """P(total games > line)."""
        if not self._games_array:
            return None
        arr = np.array(self._games_array)
        return float(np.mean(arr > line))

    def p_games_under(self, line: float) -> float:
        """P(total games < line)."""
        if not self._games_array:
            return None
        arr = np.array(self._games_array)
        return float(np.mean(arr < line))

    def p_sets_over(self, line: float) -> float:
        """P(sets played > line) from PMF."""
        return sum(p for k, p in self.sets_pmf.items() if k > line)

    def p_sets_under(self, line: float) -> float:
        """P(sets played < line) from PMF."""
        return sum(p for k, p in self.sets_pmf.items() if k < line)

    def fair_line_games(self) -> float:
        """Median total games (use as synthetic fair line)."""
        return self.games_median

    def fair_line_sets(self) -> float:
        """Median sets played."""
        arr = np.array(list(self.sets_pmf.keys()))
        probs = np.array(list(self.sets_pmf.values()))
        cumsum = np.cumsum(probs[np.argsort(arr)])
        sorted_sets = np.sort(arr)
        idx = np.searchsorted(cumsum, 0.5)
        return float(sorted_sets[min(idx, len(sorted_sets) - 1)])

    def summary(self) -> str:
        lines = [
            f"SimResult(n={self.n}, BO{self.best_of})",
            f"  P(A wins): {self.p_a_wins:.3f}",
            f"  Sets PMF: { {k: f'{v:.3f}' for k,v in sorted(self.sets_pmf.items())} }",
            f"  Games: mean={self.games_mean:.1f} median={self.games_median:.1f} "
            f"std={self.games_std:.1f}",
            f"  P10/P25/P75/P90: {self.games_p10:.0f}/{self.games_p25:.0f}/"
            f"{self.games_p75:.0f}/{self.games_p90:.0f}",
        ]
        return "\n".join(lines)


def simulate(
    s_a: float,
    s_b: float,
    best_of: int = 3,
    tiebreak_rule: TiebreaRule = "standard",
    n: int = 10_000,
    seed: int = None,
) -> SimResult:
    """
    Run n Monte Carlo simulations and return SimResult.

    Args:
        s_a           — P(A wins game when serving)
        s_b           — P(B wins game when serving)
        best_of       — 3 or 5
        tiebreak_rule — 'standard' | 'advantage' | 'tb10'
        n             — number of simulations
        seed          — random seed for reproducibility

    Returns:
        SimResult with full PMF over sets and games distributions
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    sets_counts = {}
    games_list = []
    a_wins_count = 0

    for _ in range(n):
        sets_a, sets_b, sets_played, total_games, a_won = sim_match(
            s_a, s_b, best_of, tiebreak_rule
        )
        sets_counts[sets_played] = sets_counts.get(sets_played, 0) + 1
        games_list.append(total_games)
        if a_won:
            a_wins_count += 1

    games_arr = np.array(games_list)
    sets_pmf = {k: v / n for k, v in sets_counts.items()}

    return SimResult(
        n=n,
        best_of=best_of,
        sets_pmf=sets_pmf,
        games_mean=float(np.mean(games_arr)),
        games_std=float(np.std(games_arr)),
        games_median=float(np.median(games_arr)),
        games_p10=float(np.percentile(games_arr, 10)),
        games_p25=float(np.percentile(games_arr, 25)),
        games_p75=float(np.percentile(games_arr, 75)),
        games_p90=float(np.percentile(games_arr, 90)),
        p_a_wins=a_wins_count / n,
        _games_array=games_list,
    )


# ---------------------------------------------------------------------------
# Convenience: simulate from ELO gap
# ---------------------------------------------------------------------------

def simulate_from_elo(
    elo_gap: float,
    surface: str = "Hard",
    best_of: int = 3,
    tiebreak_rule: TiebreaRule = "standard",
    n: int = 10_000,
    seed: int = None,
) -> SimResult:
    """
    Run simulation from ELO gap (p1_elo - p2_elo).

    Positive elo_gap means player1 is stronger.
    """
    s_a, s_b = elo_to_hold_probs(elo_gap, surface, best_of)
    return simulate(s_a, s_b, best_of, tiebreak_rule, n, seed)


# ---------------------------------------------------------------------------
# Quick sanity check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Sanity checks ===\n")

    # Equal players, BO3, hard court
    r = simulate_from_elo(0, "Hard", best_of=3, n=50_000, seed=42)
    print("ELO gap=0, BO3, Hard:")
    print(r.summary())
    print(f"  P(over 20.5 games): {r.p_games_over(20.5):.3f}")
    print(f"  P(over 22.5 games): {r.p_games_over(22.5):.3f}")
    print()

    # Large mismatch, BO3, clay
    r2 = simulate_from_elo(200, "Clay", best_of=3, n=50_000, seed=42)
    print("ELO gap=200, BO3, Clay:")
    print(r2.summary())
    print()

    # Small mismatch, BO5, Roland Garros advantage set
    r3 = simulate_from_elo(50, "Clay", best_of=5, tiebreak_rule="advantage", n=50_000, seed=42)
    print("ELO gap=50, BO5, Clay, advantage set (Roland Garros):")
    print(r3.summary())
    print(f"  P(over 38.5 games): {r3.p_games_over(38.5):.3f}")
    print(f"  P(over 45.5 games): {r3.p_games_over(45.5):.3f}")
    print()

    # Verify ELO inversion
    for gap in [0, 50, 100, 200, 300]:
        p_m = elo_to_p_match(gap)
        p_s3 = p_match_to_p_set(p_m, 3)
        p_s5 = p_match_to_p_set(p_m, 5)
        s_a, s_b = elo_to_hold_probs(gap, "Hard", 3)
        print(f"ELO gap {gap:4d}: p_match={p_m:.3f}  p_set_BO3={p_s3:.3f}  "
              f"p_set_BO5={p_s5:.3f}  s_A={s_a:.3f}  s_B={s_b:.3f}")
