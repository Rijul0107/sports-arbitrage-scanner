"""
The Odds API client, with credit accounting.

The free tier grants 500 credits a month. A credit is spent per
(market x region) per odds request, so one h2h/au call for one sport costs 1.
That budget disappears fast if you poll carelessly, so this client tracks
spend, exposes the balance the API reports back, and refuses to keep going
once a floor is reached.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

API_BASE = "https://api.the-odds-api.com/v4"

# Sports with no draw. A two-outcome market is the clean arbitrage case:
# cover both sides and every result is accounted for.
TWO_WAY_PREFIXES = ("rugbyleague_", "basketball_", "tennis_", "mma_", "baseball_")


class OddsAPIError(RuntimeError):
    """Base for API failures, with a `kind` for programmatic handling."""
    def __init__(self, message: str, kind: str = "http", status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class QuotaExhausted(OddsAPIError):
    pass


class BudgetExceeded(RuntimeError):
    """Raised by the client's own guard, before a request is made."""


@dataclass
class Credits:
    """Running account of API spend."""
    spent_this_session: int = 0
    remaining_reported: Optional[int] = None   # from x-requests-remaining
    used_reported: Optional[int] = None        # from x-requests-used
    budget: Optional[int] = None               # self-imposed session ceiling

    @property
    def budget_left(self) -> Optional[int]:
        if self.budget is None:
            return None
        return max(0, self.budget - self.spent_this_session)

    def summary(self) -> str:
        bits = [f"{self.spent_this_session} spent this session"]
        if self.remaining_reported is not None:
            bits.append(f"{self.remaining_reported} left on plan")
        if self.budget is not None:
            bits.append(f"{self.budget_left} left in session budget")
        return " · ".join(bits)


class OddsAPI:
    def __init__(self, api_key: str, session_budget: Optional[int] = None,
                 timeout: int = 20, min_plan_credits: int = 5):
        """
        session_budget: hard ceiling on credits this process may spend. The
            single most useful guardrail on a 500-credit monthly allowance.
        min_plan_credits: stop when the API reports fewer than this remaining.
        """
        if not api_key:
            raise ValueError("An API key is required. Get a free one at the-odds-api.com")
        self.api_key = api_key
        self.timeout = timeout
        self.min_plan_credits = min_plan_credits
        self.credits = Credits(budget=session_budget)

    # -- plumbing ---------------------------------------------------------

    def _get(self, path: str, params: Dict[str, str], cost: int) -> Tuple[object, Dict]:
        if self.credits.budget is not None and self.credits.budget_left is not None:
            if cost > self.credits.budget_left:
                raise BudgetExceeded(
                    f"This call costs {cost} credits but only "
                    f"{self.credits.budget_left} remain in the session budget."
                )
        if (self.credits.remaining_reported is not None
                and self.credits.remaining_reported < self.min_plan_credits):
            raise QuotaExhausted(
                f"Only {self.credits.remaining_reported} credits left on the plan; "
                f"stopping at the {self.min_plan_credits}-credit floor.",
                kind="quota")

        qs = urllib.parse.urlencode({**params, "apiKey": self.api_key})
        req = urllib.request.Request(
            f"{API_BASE}{path}?{qs}", headers={"User-Agent": "arb-desk/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                hdrs = resp.headers
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 401:
                raise OddsAPIError(f"API key rejected: {detail}", "auth", 401) from e
            if e.code == 429:
                raise QuotaExhausted(f"Monthly quota exhausted: {detail}", "quota", 429) from e
            if e.code == 422:
                raise OddsAPIError(f"Bad request parameters: {detail}", "params", 422) from e
            raise OddsAPIError(f"HTTP {e.code}: {detail}", "http", e.code) from e
        except urllib.error.URLError as e:
            raise OddsAPIError(f"Network unreachable: {e.reason}", "network") from e

        # Charge what the API says it charged, not what we predicted. The two
        # differ: the docs state "Empty responses don't count against quota", so
        # a sport key that is in season but has no games right now is free. Using
        # the estimate made the session budget cut polling short of the ceiling
        # the user actually set. Fall back to the estimate only if the header is
        # missing or unparseable, since under-counting spend is the dangerous
        # direction on a metered plan.
        charged = hdrs.get("x-requests-last")
        try:
            self.credits.spent_this_session += int(float(charged))
        except (TypeError, ValueError):
            self.credits.spent_this_session += cost

        rem, used = hdrs.get("x-requests-remaining"), hdrs.get("x-requests-used")
        if rem is not None:
            try:
                self.credits.remaining_reported = int(float(rem))
            except ValueError:
                pass
        if used is not None:
            try:
                self.credits.used_reported = int(float(used))
            except ValueError:
                pass
        return body, dict(hdrs)

    # -- endpoints --------------------------------------------------------

    def sports(self, two_way_only: bool = True,
               prefixes: Tuple[str, ...] = TWO_WAY_PREFIXES) -> List[Dict]:
        """In-season sports. This endpoint is free — it costs no credits."""
        body, _ = self._get("/sports", {}, cost=0)
        out = [s for s in body if s.get("active") and not s.get("has_outrights")]
        if two_way_only:
            out = [s for s in out if s["key"].startswith(prefixes)]
        return out

    def odds(self, sport_key: str, regions: str = "au", markets: str = "h2h",
             odds_format: str = "decimal") -> List[Dict]:
        """Current odds. Costs len(markets) x len(regions) credits."""
        cost = len(markets.split(",")) * len(regions.split(","))
        body, _ = self._get(
            f"/sports/{sport_key}/odds",
            {"regions": regions, "markets": markets, "oddsFormat": odds_format},
            cost=cost)
        return body

    def events(self, sport_key: str) -> List[Dict]:
        """Scheduled events without prices. Free — useful for finding a fixture
        to watch before you spend anything on odds."""
        body, _ = self._get(f"/sports/{sport_key}/events", {}, cost=0)
        return body


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def staleness_seconds(event: dict) -> Optional[float]:
    """How old the freshest bookmaker quote in this event is, in seconds.

    This matters more than anything else on the screen. An arbitrage computed
    from prices updated four minutes ago is a statement about the past. The
    API's own refresh cadence, plus your polling interval, sets a floor on how
    stale your view can be."""
    updates = [parse_iso(bk.get("last_update", "")) for bk in event.get("bookmakers", [])]
    updates = [u for u in updates if u]
    if not updates:
        return None
    return (datetime.now(timezone.utc) - max(updates)).total_seconds()


def is_in_play(event: dict) -> bool:
    """Has the event already started?

    Online in-play betting is prohibited in Australia under the Interactive
    Gambling Act — licensed operators may only accept in-play bets by
    telephone or in a retail venue. Anything this returns True for cannot be
    executed online from Australia, whatever the margin says."""
    start = parse_iso(event.get("commence_time", ""))
    if start is None:
        return False
    return start <= datetime.now(timezone.utc)


def minutes_to_start(event: dict) -> Optional[float]:
    start = parse_iso(event.get("commence_time", ""))
    if start is None:
        return None
    return (start - datetime.now(timezone.utc)).total_seconds() / 60.0
