"""
Scanning: turn API responses into ranked, stakeable opportunities.

Shared by watch.py (terminal) and serve.py (dashboard) so the two can never
disagree about what is on offer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .api import OddsAPI, is_in_play, minutes_to_start, parse_iso, staleness_seconds
from .core import Arb, allocate, apply_stake_limits, market_from_event, recommend_first_leg
from .pairs import GameAnalysis, analyse_game


@dataclass
class Opportunity:
    """One game, fully analysed and staked."""
    event_id: str
    sport_key: str
    sport_title: str
    home: str
    away: str
    commence_time: str
    minutes_to_start: Optional[float]
    ages: Dict[str, Optional[float]]      # bookmaker -> seconds since capture
    analysis: GameAnalysis
    arb: Optional[Arb] = None             # the best pairing, staked
    profit: float = 0.0

    @property
    def best_margin_pct(self) -> float:
        return self.analysis.best_margin_pct

    @property
    def depth(self) -> int:
        return self.analysis.depth

    @property
    def placeable(self) -> bool:
        return self.arb is not None and self.arb.worst_profit > 0

    @property
    def display_outcomes(self) -> List[str]:
        """Home first, then away. GameAnalysis sorts outcomes alphabetically so
        the maths is deterministic; that is the wrong order to read a match in."""
        outs = list(self.analysis.outcomes)
        ordered = [o for o in (self.home, self.away) if o in outs]
        return ordered + [o for o in outs if o not in ordered]

    @property
    def oldest_quote(self) -> float:
        vals = [a for a in self.ages.values() if a is not None]
        return max(vals) if vals else 0.0

    def to_dict(self) -> dict:
        """The shape the dashboard consumes. Only odds and ages are sent —
        the page recomputes every margin, so what is on screen can never
        disagree with the prices it came from."""
        return {
            "id": self.event_id,
            "sport": self.sport_title,
            "home": self.home,
            "away": self.away,
            "starts_in_min": round(self.minutes_to_start or 0),
            "outcomes": self.display_outcomes,
            "odds": self.analysis.book_odds,
            "ages": {b: (round(a) if a is not None else None)
                     for b, a in self.ages.items()},
        }


def quote_ages(event: dict, books: Sequence[str],
               now: Optional[datetime] = None) -> Dict[str, Optional[float]]:
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Optional[float]] = {}
    for bk in event.get("bookmakers", []) or []:
        title = bk.get("title") or bk.get("key")
        if title not in books:
            continue
        upd = parse_iso(bk.get("last_update", ""))
        out[title] = (now - upd).total_seconds() if upd else None
    return out


def assess_event(event: dict, sport_key: str, sport_title: str, cfg) -> Optional[Opportunity]:
    """Evaluate one event against the configured books and safety rules."""
    if cfg.PRE_MATCH_ONLY and is_in_play(event):
        return None                                   # illegal to place online in AU
    mins = minutes_to_start(event)
    if mins is not None and mins < cfg.MIN_MINUTES_TO_START:
        return None                                   # no time to place both legs

    age = staleness_seconds(event)
    if age is not None and age > cfg.MAX_DATA_AGE_SECONDS:
        return None                                   # too stale to trust

    all_odds = market_from_event(event, "h2h")
    book_odds = {b: all_odds[b] for b in cfg.BOOKS if b in all_odds}
    if len(book_odds) < 2:
        return None                                   # need two of your books

    ga = analyse_game(book_odds, "h2h", cfg.BOOKS, commission_pct=cfg.COMMISSION_PCT)
    if ga is None:
        return None

    opp = Opportunity(
        event_id=event.get("id", ""),
        sport_key=sport_key, sport_title=sport_title,
        home=event.get("home_team", "?"), away=event.get("away_team", "?"),
        commence_time=event.get("commence_time", ""),
        minutes_to_start=mins,
        ages=quote_ages(event, cfg.BOOKS),
        analysis=ga,
    )

    bp = ga.best_pair          # None unless genuinely arbitrageable
    if bp is not None:
        arb = bp.arb
        allocate(arb, cfg.TOTAL_STAKE, increment=cfg.STAKE_INCREMENT)
        if getattr(cfg, "STAKE_LIMITS", None):
            apply_stake_limits(arb, cfg.STAKE_LIMITS, increment=cfg.STAKE_INCREMENT)
        opp.arb = arb
        opp.profit = arb.worst_profit
    return opp


def select_sports(api: OddsAPI, cfg) -> List[dict]:
    """The sport keys this poll will spend credits on.

    Two ways in, because competitions behave differently. cfg.SPORT_KEYS names
    fixtures that sit still (NRL, NBA, NBL). cfg.SPORT_PREFIXES matches ones
    that rotate — tennis has a key per tournament and which are live changes
    weekly, so naming them would go stale in days.

    Both are filtered against /sports, which is free, because an odds request
    for an out-of-season key still costs a credit and returns nothing.

    Shared by scan.py, watch.py and books.py so the three cannot disagree about
    what a poll costs.
    """
    active = api.sports(two_way_only=False, prefixes=())
    keys = set(getattr(cfg, "SPORT_KEYS", None) or ())
    prefixes = tuple(getattr(cfg, "SPORT_PREFIXES", None) or ())
    if not keys and not prefixes:
        return []
    return [s for s in active
            if s["key"] in keys or (prefixes and s["key"].startswith(prefixes))]


def scan(api: OddsAPI, cfg, sports: Optional[List[dict]] = None) -> Dict:
    """One full poll. Returns opportunities sorted by what they pay."""
    if sports is None:
        sports = select_sports(api, cfg)

    opportunities: List[Opportunity] = []
    events_seen = 0
    for sp in sports:
        events = api.odds(sp["key"], regions=cfg.REGIONS, markets="h2h")
        events_seen += len(events)
        for ev in events:
            opp = assess_event(ev, sp["key"], sp["title"], cfg)
            if opp:
                opportunities.append(opp)

    # Rank by cash, not percentage: a 3% edge on a market where rounding eats
    # the profit is worth less than a 1.5% edge that actually pays.
    opportunities.sort(key=lambda o: (-o.profit, -o.best_margin_pct))

    playable = [o for o in opportunities
                if o.placeable
                and o.profit >= cfg.MIN_PROFIT
                and o.best_margin_pct >= cfg.MIN_MARGIN_PCT]

    return {
        "opportunities": opportunities,
        "playable": playable,
        "events_seen": events_seen,
        "sports": sports,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
