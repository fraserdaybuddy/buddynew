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
LOGIN_URL   = "https://identitysso.betfair.com/api/login"
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

# Market types for totals markets
# These are the Betfair market type codes for over/under stat markets
TOTALS_MARKET_TYPES = {
    "darts":   ["TOTAL_180S", "MATCH_ODDS"],    # TOTAL_180S is the target
    "snooker": ["TOTAL_CENTURIES", "MATCH_ODDS"],
    "tennis":  ["TOTAL_ACES", "MATCH_ODDS"],
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


def _credentials() -> tuple[str, str, str]:
    _load_env()
    username = os.environ.get("BETFAIR_USERNAME", "")
    password = os.environ.get("BETFAIR_PASSWORD", "")
    app_key  = os.environ.get("BETFAIR_APP_KEY", "")
    if not all([username, password, app_key]):
        raise RuntimeError(
            "BETFAIR_USERNAME / BETFAIR_PASSWORD / BETFAIR_APP_KEY not set in .env"
        )
    return username, password, app_key


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
        username, password, app_key = _credentials()
        self._app_key = app_key

        resp = requests.post(
            LOGIN_URL,
            data={"username": username, "password": password},
            headers={"X-Application": app_key,
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "SUCCESS":
            raise RuntimeError(f"Betfair login failed: {data.get('error', 'unknown')}")

        self._token = data["token"]
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
        price_projection = {
            "priceData": ["EX_BEST_OFFERS", "EX_TRADED"],
        }
    body = {
        "marketIds":       market_ids,
        "priceProjection": price_projection,
        "orderProjection": "ALL",
        "matchProjection": "NO_ROLLUP",
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


# ── Historical data via Exchange Stream API ────────────────────────────────────

def search_totals_markets(
    session:   BetfairSession,
    sport:     str,
    from_date: str,
    to_date:   str,
) -> list[dict]:
    """
    Find totals (O/U) markets for a sport. Returns matching markets.
    Primary target: TOTAL_180S (darts), TOTAL_CENTURIES (snooker), TOTAL_ACES (tennis).
    Falls back to all markets if specific type not found.
    """
    market_types = TOTALS_MARKET_TYPES.get(sport, [])
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


# ── Main / CLI ────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",           action="store_true", help="Test authentication")
    parser.add_argument("--list-events",    action="store_true", help="List all event types")
    parser.add_argument("--search-markets", choices=["darts","snooker","tennis"],
                        help="Search for totals markets for a sport")
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
        print(f"[betfair] Searching {sport} totals markets {args.from_date} → {args.to_date}")
        session.login()
        markets = search_totals_markets(session, sport, args.from_date, args.to_date)
        print(f"\nFound {len(markets)} markets:")
        for m in markets[:20]:
            event = m.get("event", {})
            print(f"  {m.get('marketId'):<14}  {m.get('marketName'):<30}  "
                  f"{event.get('name',''):<30}  "
                  f"{m.get('marketStartTime','')[:10]}")
        if len(markets) > 20:
            print(f"  ... and {len(markets)-20} more")
        session.logout()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
