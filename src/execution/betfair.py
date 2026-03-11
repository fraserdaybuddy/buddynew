"""
betfair.py — Betfair Exchange API client
JOB-006 Sports Betting Model

Handles: authentication, market search, closing price retrieval.
Uses delayed app key for data queries (sufficient for historical line sourcing).

Credentials loaded from .env — never hardcoded.

Usage:
    PYTHONUTF8=1 python -m src.execution.betfair --test
    PYTHONUTF8=1 python -m src.execution.betfair --search-markets darts
"""

import os
import json
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("betfair")

# ── Endpoints ────────────────────────────────────────────────────────────────
LOGIN_URL   = "https://identitysso-cert.betfair.com/api/certlogin"
API_URL     = "https://api.betfair.com/exchange/betting/rest/v1.0"
KEEPALIVE   = "https://identitysso.betfair.com/api/keepAlive"
LOGOUT_URL  = "https://identitysso.betfair.com/api/logout"

# ── Betfair event type IDs ────────────────────────────────────────────────────
# Confirmed event type IDs for our sports
EVENT_TYPES = {
    "darts":   "3503",   # Darts
    "snooker": "6423",   # Snooker
    "tennis":  "2",      # Tennis
}

# ── Market type codes for all target markets ──────────────────────────────────
# See BETFAIR_MARKET_SPEC.md for full verification status and runner format.
#
# Confidence key:  HIGH = confirmed on Betfair
#                  MEDIUM = strongly expected but not yet verified via API
#                  LOW = unverified, may not exist
#
TARGET_MARKET_TYPES = {
    "tennis": [
        "TOTAL_GAMES",          # HIGH — Total games O/U (most liquid tennis stat market)
        "TOTAL_SETS",           # HIGH — Total sets O/U (also "NUMBER_OF_SETS")
        "NUMBER_OF_SETS",       # alias — try both in queries
        "FIRST_SET_WINNER",     # HIGH — First set winner (moneyline)
        "SET_WINNER",           # alias — may appear as SET_WINNER with "1st Set" in name
        "SET_1_TOTAL_GAMES",    # LOW  — First set total games (verify existence)
        "MATCH_ODDS",           # confirmed — match winner (not our primary target)
    ],
    "darts": [
        "TOTAL_180S",           # HIGH — Total 180s O/U (major PDC events only)
        "TOTAL_LEGS",           # LOW  — Total legs O/U (UNVERIFIED — may not exist)
        "MATCH_ODDS",           # confirmed — match winner
    ],
    "snooker": [
        "TOTAL_FRAMES",         # HIGH — Total frames O/U
        "TOTAL_CENTURIES",      # LOW  — Total centuries O/U (UNVERIFIED)
        "MATCH_ODDS",           # confirmed — match winner
    ],
}

# Primary stat markets per sport (the ones we actually bet)
STAT_MARKET_TYPES = {
    "tennis":  ["TOTAL_GAMES", "TOTAL_SETS", "NUMBER_OF_SETS",
                "FIRST_SET_WINNER", "SET_WINNER", "SET_1_TOTAL_GAMES"],
    "darts":   ["TOTAL_180S", "TOTAL_LEGS"],
    "snooker": ["TOTAL_FRAMES", "TOTAL_CENTURIES"],
}

# Legacy alias kept for backwards compat
TOTALS_MARKET_TYPES = {
    "darts":   STAT_MARKET_TYPES["darts"],
    "snooker": STAT_MARKET_TYPES["snooker"],
    "tennis":  STAT_MARKET_TYPES["tennis"],
}


# ── Credentials ──────────────────────────────────────────────────────────────

def _load_env():
    """Load .env file into environment if not already set."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key.strip() and val.strip() and key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip()


def _credentials() -> tuple[str, str, str, str, str]:
    _load_env()
    username = os.environ.get("BETFAIR_USERNAME", "")
    password = os.environ.get("BETFAIR_PASSWORD", "")
    app_key  = os.environ.get("BETFAIR_APP_KEY", "")
    cert_crt = os.environ.get("BETFAIR_CERT_CRT", "")
    cert_key = os.environ.get("BETFAIR_CERT_KEY", "")
    if not all([username, password, app_key, cert_crt, cert_key]):
        raise RuntimeError(
            "BETFAIR_USERNAME / BETFAIR_PASSWORD / BETFAIR_APP_KEY / "
            "BETFAIR_CERT_CRT / BETFAIR_CERT_KEY not set in .env"
        )
    return username, password, app_key, cert_crt, cert_key


# ── Session management ────────────────────────────────────────────────────────

class BetfairSession:
    """
    Manages a Betfair API session.
    Authenticates on first use, keeps alive during the session.
    """

    def __init__(self):
        self._token:   Optional[str] = None
        self._app_key: Optional[str] = None

    def login(self) -> str:
        username, password, app_key, cert_crt, cert_key = _credentials()
        self._app_key = app_key

        resp = requests.post(
            LOGIN_URL,
            data={"username": username, "password": password},
            headers={"X-Application": app_key,
                     "Content-Type": "application/x-www-form-urlencoded"},
            cert=(cert_crt, cert_key),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("loginStatus") != "SUCCESS":
            raise RuntimeError(f"Betfair login failed: {data.get('loginStatus', 'unknown')}")

        self._token = data["sessionToken"]
        log.info("[betfair] Logged in successfully")
        return self._token

    def logout(self):
        if not self._token:
            return
        requests.post(LOGOUT_URL,
                      headers=self._headers(),
                      timeout=10)
        self._token = None
        log.info("[betfair] Logged out")

    def _headers(self) -> dict:
        if not self._token:
            self.login()
        return {
            "X-Authentication": self._token,
            "X-Application":    self._app_key,
            "Content-Type":     "application/json",
            "Accept":           "application/json",
        }

    def post(self, endpoint: str, body: dict) -> dict:
        url  = f"{API_URL}/{endpoint}/"
        resp = requests.post(url, json=body, headers=self._headers(), timeout=30)
        if not resp.ok:
            log.error(f"[betfair] {endpoint} → {resp.status_code}: {resp.text[:400]}")
        resp.raise_for_status()
        return resp.json()


# ── Market search ─────────────────────────────────────────────────────────────

def list_event_types(session: BetfairSession) -> list[dict]:
    """Return all available event types (sports) on the exchange."""
    return session.post("listEventTypes", {"filter": {}})


def list_markets(
    session:    BetfairSession,
    sport:      str,
    from_date:  str,  # "2024-01-01T00:00:00Z"
    to_date:    str,  # "2025-12-31T23:59:59Z"
    market_types: Optional[list[str]] = None,
    max_results: int = 200,
) -> list[dict]:
    """
    Search for markets by sport and date range.
    Returns list of market summaries with marketId, marketName, event.
    """
    event_type_id = EVENT_TYPES.get(sport)
    if not event_type_id:
        raise ValueError(f"Unknown sport: {sport}")

    mkt_filter = {
        "eventTypeIds": [event_type_id],
        "marketStartTime": {
            "from": from_date,
            "to":   to_date,
        },
    }
    if market_types:
        mkt_filter["marketTypeCodes"] = market_types

    body = {
        "filter":        mkt_filter,
        "marketProjection": ["MARKET_START_TIME", "EVENT", "RUNNER_DESCRIPTION"],
        "maxResults":    str(max_results),
        "sort":          "FIRST_TO_START",
    }

    return session.post("listMarketCatalogue", body)


def get_market_book(
    session:    BetfairSession,
    market_ids: list[str],
    price_projection: Optional[dict] = None,
) -> list[dict]:
    """
    Get current/last traded prices for a list of market IDs.
    For settled markets returns last traded price.
    """
    if not price_projection:
        # EX_BEST_OFFERS only — EX_TRADED triggers TOO_MUCH_DATA for large markets
        # (Total Games markets have 110+ runners; 19 markets × 110 × 2 > API limit)
        # Market-level totalMatched is used for liquidity instead of per-runner volume.
        price_projection = {
            "priceData": ["EX_BEST_OFFERS"],
        }
    # orderProjection / matchProjection are NOT valid for delayed/data app keys
    body = {
        "marketIds":       market_ids,
        "priceProjection": price_projection,
    }
    return session.post("listMarketBook", body)


def get_settled_markets(
    session:   BetfairSession,
    sport:     str,
    from_date: str,
    to_date:   str,
) -> list[dict]:
    """
    Retrieve settled (closed) markets for a sport and date range.
    Uses listClearedOrders to find markets that have been settled.
    Note: Only returns markets where we placed bets.
    For historical price data, use list_markets + get_market_book.
    """
    body = {
        "betStatus":   "SETTLED",
        "eventTypeIds": [EVENT_TYPES[sport]],
        "settledDateRange": {
            "from": from_date,
            "to":   to_date,
        },
        "groupBy": "MARKET",
        "includeItemDescription": True,
        "maxResults": 200,
    }
    return session.post("listClearedOrders", body)


# ── Bet-level settlement ───────────────────────────────────────────────────────

ACCOUNTS_URL = "https://api.betfair.com/exchange/account/rest/v1.0"


def list_cleared_orders(
    session:      BetfairSession,
    bet_status:   str = "SETTLED",
    settled_from: Optional[str] = None,
    settled_to:   Optional[str] = None,
    max_results:  int = 1000,
) -> list[dict]:
    """
    Pull settled orders at BET level (not market level).
    Returns list of ClearedOrderSummary dicts with betId, profit, settledDate.
    """
    body: dict = {
        "betStatus":            bet_status,
        "groupBy":              "BET",
        "includeItemDescription": True,
        "maxResults":           max_results,
    }
    if settled_from or settled_to:
        body["settledDateRange"] = {}
        if settled_from:
            body["settledDateRange"]["from"] = settled_from
        if settled_to:
            body["settledDateRange"]["to"] = settled_to

    result = session.post("listClearedOrders", body)
    # Response is {"clearedOrders": [...], "moreAvailable": bool}
    if isinstance(result, dict):
        return result.get("clearedOrders", [])
    return result


def get_account_funds(session: BetfairSession) -> dict:
    """
    Pull current account balance from Betfair Accounts API.
    Returns dict with availableToBetBalance, exposure, retainedCommission.
    """
    url = f"{ACCOUNTS_URL}/getAccountFunds/"
    resp = requests.post(
        url,
        json={"wallet": "UK wallet"},
        headers=session._headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# Alias so new data modules can import BetfairClient
BetfairClient = BetfairSession


# ── Historical data via Exchange Stream API ────────────────────────────────────

def search_totals_markets(
    session:   BetfairSession,
    sport:     str,
    from_date: str,
    to_date:   str,
) -> list[dict]:
    """
    Find stat O/U markets for a sport. Returns matching markets.
    Uses STAT_MARKET_TYPES[sport] — see BETFAIR_MARKET_SPEC.md for verification status.
    """
    market_types = STAT_MARKET_TYPES.get(sport, [])
    results = []

    for mtype in market_types:
        markets = list_markets(
            session, sport, from_date, to_date,
            market_types=[mtype],
            max_results=200,
        )
        for m in markets:
            m["_query_market_type"] = mtype
        results.extend(markets)
        log.info(f"[betfair] {sport} {mtype}: {len(markets)} markets found")

    return results


def list_all_markets(
    session:   BetfairSession,
    sport:     str,
    from_date: str,
    to_date:   str,
) -> list[dict]:
    """
    Fetch ALL market types for a sport (no marketTypeCodes filter).
    Use this to discover available market type codes — run once after login
    to verify BETFAIR_MARKET_SPEC.md assumptions.

    Returns markets with unique marketType values printed to console.
    """
    event_type_id = EVENT_TYPES.get(sport)
    if not event_type_id:
        raise ValueError(f"Unknown sport: {sport}")

    body = {
        "filter": {
            "eventTypeIds": [event_type_id],
            "marketStartTime": {"from": from_date, "to": to_date},
        },
        "marketProjection": ["MARKET_START_TIME", "EVENT", "RUNNER_DESCRIPTION",
                             "MARKET_DESCRIPTION"],
        "maxResults": 200,
        "sort": "FIRST_TO_START",
    }
    return session.post("listMarketCatalogue", body)


# ── Main / CLI ────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",             action="store_true", help="Test authentication")
    parser.add_argument("--list-events",      action="store_true", help="List all event types")
    parser.add_argument("--search-markets",   choices=["darts","snooker","tennis"],
                        help="Search for stat O/U markets for a sport")
    parser.add_argument("--list-all-markets", choices=["darts","snooker","tennis"],
                        help="List ALL market types for a sport (for verification)")
    parser.add_argument("--from-date", default="2024-01-01T00:00:00Z")
    parser.add_argument("--to-date",   default="2025-12-31T23:59:59Z")
    args = parser.parse_args()

    session = BetfairSession()

    if args.test:
        print("[betfair] Testing authentication...")
        token = session.login()
        print(f"[betfair] SUCCESS — session token: {token[:20]}...")
        session.logout()
        return

    if args.list_events:
        session.login()
        events = list_event_types(session)
        print(f"[betfair] {len(events)} event types available:")
        for e in sorted(events, key=lambda x: x.get("eventType",{}).get("name","")):
            et = e.get("eventType", {})
            print(f"  {et.get('id'):>6}  {et.get('name')}")
        session.logout()
        return

    if args.search_markets:
        sport = args.search_markets
        print(f"[betfair] Searching {sport} stat markets {args.from_date} → {args.to_date}")
        session.login()
        markets = search_totals_markets(session, sport, args.from_date, args.to_date)
        print(f"\nFound {len(markets)} markets:")
        for m in markets[:20]:
            event = m.get("event", {})
            mtype = m.get("_query_market_type", "?")
            print(f"  {m.get('marketId'):<14}  {mtype:<25}  {m.get('marketName'):<30}  "
                  f"{event.get('name',''):<30}  {m.get('marketStartTime','')[:10]}")
        if len(markets) > 20:
            print(f"  ... and {len(markets)-20} more")
        session.logout()
        return

    if args.list_all_markets:
        sport = args.list_all_markets
        print(f"[betfair] Fetching ALL {sport} market types {args.from_date} → {args.to_date}")
        print("[betfair] Use this to verify market type codes for BETFAIR_MARKET_SPEC.md\n")
        session.login()
        markets = list_all_markets(session, sport, args.from_date, args.to_date)

        # Collect unique market types and counts
        type_counts = {}
        for m in markets:
            mt = m.get("description", {}).get("marketType", "UNKNOWN")
            type_counts[mt] = type_counts.get(mt, 0) + 1

        print(f"Found {len(markets)} markets, {len(type_counts)} unique market types:\n")
        print(f"  {'Market Type Code':<35}  {'Count':>6}")
        print(f"  {'-'*35}  {'-'*6}")
        for mtype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            target = " <-- TARGET" if mtype in TARGET_MARKET_TYPES.get(sport, []) else ""
            print(f"  {mtype:<35}  {count:>6}{target}")

        print(f"\nSample markets (first 15):")
        for m in markets[:15]:
            event = m.get("event", {})
            mtype = m.get("description", {}).get("marketType", "?")
            print(f"  {m.get('marketId'):<14}  {mtype:<30}  {m.get('marketName'):<35}  "
                  f"{event.get('name','')}")
        session.logout()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
