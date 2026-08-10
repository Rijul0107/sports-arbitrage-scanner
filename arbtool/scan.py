"""
Scanning: turn API responses into ranked, stakeable opportunities.

Shared by watch.py (terminal) and serve.py (dashboard) so the two can never
disagree about what is on offer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .api import OddsAPI, is_in_play, minutes_to_start, parse_iso, staleness_seconds
from .core import Arb, allocate, apply_stake_limits
from .lines import submarkets
from .pairs import GameAnalysis, analyse_game


@dataclass(frozen=True)
class LegContext:
    """What the rest of the board says about one leg of an arbitrage.

    Two facts decide whether a crossing is real money or a quote the book
    forgot to update, and neither of them appears anywhere in the arbitrage
    maths: how old this book's price is, and whether any other book agrees
    with it. A price well clear of every rival is the signature of a stale
    line, and four pairings that all run through one outlying book are one
    price wearing four hats, not four confirmations.

    This reports and does not filter. Suppressing on age or on distance from
    the field would silently drop the exact case the owner wants to see and
    check in the app; a dropped alert is indistinguishable from a quiet
    board, which is how a whole missing bookmaker hid once before."""
    outcome: str
    book: str
    odds: float
    age: Optional[float]                     # seconds since this book's capture
    rivals: List[Tuple[str, float]]          # other books on this outcome, best first

    @property
    def next_best(self) -> Optional[Tuple[str, float]]:
        return self.rivals[0] if self.rivals else None

    @property
    def alone(self) -> bool:
        """No other configured book quotes this outcome at all.

        The extreme of the outlier case: there is nothing to corroborate
        against, so the edge rests entirely on one price being right."""
        return not self.rivals

    def describe(self) -> str:
        """One plain-text line of context, for a phone screen or a card.

        Plain text rather than HTML so the Telegram and terminal surfaces
        cannot drift apart in what they report, only in how they mark it up."""
        when = "age unknown" if self.age is None else f"quoted {self.age:.0f}s ago"
        if self.alone:
            return f"{when} · no other book quotes this"
        prices = [p for _, p in self.rivals]
        top_book, top_price = self.rivals[0]
        if len(self.rivals) == 1:
            return f"{when} · 1 other quotes {top_price:.2f} ({top_book})"
        return (f"{when} · {len(self.rivals)} others quote "
                f"{min(prices):.2f}–{max(prices):.2f}, best {top_book} {top_price:.2f}")


@dataclass
class Opportunity:
    """One market in one game, fully analysed and staked.

    Not one per game: head-to-head is a single market, but totals and spreads
    carry one market per line and each line is a separate bet with its own
    prices, its own best pairing and its own stake."""
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
    market_key: str = "h2h"
    market_label: str = ""                # "" for h2h, else "Total 50.5"
    can_push: bool = False                # whole line: an exact result refunds

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
    def uid(self) -> str:
        """Identity of this market, not of the game.

        A game now yields several opportunities, so anything keyed on the event
        id alone — the dashboard's per-game DOM ids, alert suppression — would
        collapse a spreads arbitrage and a head-to-head arbitrage on the same
        fixture into one.

        Head-to-head keeps the bare event id on purpose: the suppression state
        already on disk is keyed that way, so a standing h2h alert stays
        suppressed across the deploy that added the other markets. Keyed on
        market_key as well as the label so that a market arriving without a
        label can never collide with the head-to-head entry."""
        if self.market_key == "h2h" and not self.market_label:
            return self.event_id
        return f"{self.event_id}:{self.market_key}:{self.market_label}"

    @property
    def display_outcomes(self) -> List[str]:
        """Home first, then away. GameAnalysis sorts outcomes alphabetically so
        the maths is deterministic; that is the wrong order to read a match in.

        Matched on prefix rather than equality because a spreads outcome is the
        team name plus its handicap ("Penrith Panthers -1.5")."""
        outs = list(self.analysis.outcomes)
        ordered = []
        for team in (self.home, self.away):
            for o in outs:
                if o in ordered:
                    continue
                if o == team or o.startswith(team + " "):
                    ordered.append(o)
                    break
        return ordered + [o for o in outs if o not in ordered]

    @property
    def oldest_quote(self) -> float:
        vals = [a for a in self.ages.values() if a is not None]
        return max(vals) if vals else 0.0

    def leg_context(self, outcome: str) -> Optional[LegContext]:
        """Age and rival prices for the leg staked on `outcome`.

        Age is that leg's own book, not the event's. staleness_seconds() gates
        on the freshest book in the event, so a leg can be minutes old inside
        an event that passed the gate — this is the number that describes the
        price actually being staked."""
        if self.arb is None or outcome not in self.arb.legs:
            return None
        leg = self.arb.legs[outcome]
        rivals = []
        for book, prices in self.analysis.book_odds.items():
            if book == leg.book:
                continue
            price = prices.get(outcome)
            # Same floor as best_odds_per_outcome: 1.00 returns the stake and
            # is not a real market, so it must not read as a rival quote.
            if price and price > 1.0:
                rivals.append((book, float(price)))
        rivals.sort(key=lambda bp: -bp[1])
        return LegContext(outcome=outcome, book=leg.book, odds=leg.odds,
                          age=self.ages.get(leg.book), rivals=rivals)

    def to_dict(self) -> dict:
        """The shape the dashboard consumes. Only odds and ages are sent —
        the page recomputes every margin, so what is on screen can never
        disagree with the prices it came from."""
        return {
            "id": self.uid,
            "sport": self.sport_title,
            "home": self.home,
            "away": self.away,
            "starts_in_min": round(self.minutes_to_start or 0),
            "market": self.market_label,
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


def commission_map(cfg) -> dict:
    """{book: commission pct} for every configured book.

    Built here rather than read straight from config so the flat
    COMMISSION_PCT still works as a floor for books with no explicit rate, and
    so every caller — scan, watch, the bot — gets the same mapping instead of
    each deciding how to merge the two settings."""
    flat = float(getattr(cfg, "COMMISSION_PCT", 0.0) or 0.0)
    per_book = dict(getattr(cfg, "BOOK_COMMISSION_PCT", None) or {})
    return {b: float(per_book.get(b, flat)) for b in getattr(cfg, "BOOKS", ())}


def markets_for(cfg, sport_key: str) -> str:
    """The market keys to request for one sport, as the API's comma list.

    Per sport rather than global because a market is only worth its credit
    where AU books actually quote it: NRL spreads carry nine books, MMA spreads
    carry none, and both cost the same to ask for. Read through a helper so
    scan, watch and books.py cannot disagree about what a poll costs."""
    per_sport = dict(getattr(cfg, "SPORT_MARKETS", None) or {})
    return per_sport.get(sport_key, getattr(cfg, "DEFAULT_MARKETS", "h2h"))


def assess_event(event: dict, sport_key: str, sport_title: str, cfg) -> List[Opportunity]:
    """Every safely comparable two-outcome market in one event, staked.

    A list, not one result. Head-to-head yields at most one; totals and spreads
    yield one per line, and each line is a separate bet that has to be staked
    and reported on its own. Prices are grouped by their exact signed line in
    lines.submarkets() before anything here sees them, so two different lines
    can never be compared against each other."""
    if cfg.PRE_MATCH_ONLY and is_in_play(event):
        return []                                     # illegal to place online in AU
    mins = minutes_to_start(event)
    if mins is not None and mins < cfg.MIN_MINUTES_TO_START:
        return []                                     # no time to place both legs

    age = staleness_seconds(event)
    if age is not None and age > cfg.MAX_DATA_AGE_SECONDS:
        return []                                     # too stale to trust

    wanted = [m for m in markets_for(cfg, sport_key).split(",") if m]
    ages = quote_ages(event, cfg.BOOKS)
    allow_push = bool(getattr(cfg, "ALLOW_PUSH_LINES", False))
    commission = commission_map(cfg)

    out: List[Opportunity] = []
    for sm in submarkets(event, wanted):
        if sm.can_push and not allow_push:
            # A whole line pays back both stakes on an exact result. The
            # arbitrage is not wrong, but the guaranteed profit it advertises
            # is not what you collect, and books differ on whether a push
            # voids one leg or both.
            continue

        book_odds = {b: sm.book_odds[b] for b in cfg.BOOKS if b in sm.book_odds}
        if len(book_odds) < 2:
            continue                                  # need two of your books

        ga = analyse_game(book_odds, sm.market_key, cfg.BOOKS,
                          commission_pct=commission)
        if ga is None:
            continue

        opp = Opportunity(
            event_id=event.get("id", ""),
            sport_key=sport_key, sport_title=sport_title,
            home=event.get("home_team", "?"), away=event.get("away_team", "?"),
            commence_time=event.get("commence_time", ""),
            minutes_to_start=mins,
            ages=ages,
            analysis=ga,
            market_key=sm.market_key,
            market_label=sm.label,
            can_push=sm.can_push,
        )

        bp = ga.best_pair          # None unless genuinely arbitrageable
        if bp is not None:
            arb = bp.arb
            allocate(arb, cfg.TOTAL_STAKE, increment=cfg.STAKE_INCREMENT)
            if getattr(cfg, "STAKE_LIMITS", None):
                apply_stake_limits(arb, cfg.STAKE_LIMITS, increment=cfg.STAKE_INCREMENT)
            opp.arb = arb
            opp.profit = arb.worst_profit
        out.append(opp)

    out.sort(key=lambda o: (-o.profit, -o.best_margin_pct))
    return out


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
        events = api.odds(sp["key"], regions=cfg.REGIONS,
                          markets=markets_for(cfg, sp["key"]))
        events_seen += len(events)
        for ev in events:
            opportunities.extend(assess_event(ev, sp["key"], sp["title"], cfg))

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
