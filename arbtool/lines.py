"""
Line matching: splitting one API market into safely comparable two-outcome
markets.

Head-to-head is one market per game, so prices can be compared directly.
Totals and spreads cannot: a game carries several lines at once, and two prices
are opposing sides of the same bet only when they sit on the *same* line.

    Over 40.5  against  Under 40.5   is a hedge — exactly one wins.
    Over 41.5  against  Under 40.5   is a SANDWICH — a total of exactly 41
                                     loses BOTH legs.

Spreads have a nastier version of the same trap, because the two sides carry
mirrored points rather than equal ones. Grouping on abs(point) looks right and
is not: one book quoting Penrith -1.5 / Roosters +1.5 and another quoting
Penrith +1.5 / Roosters -1.5 both sit at abs 1.5, but backing Penrith -1.5 and
Roosters -1.5 needs *both* teams to win by two, which cannot happen.

The defence is structural rather than a check bolted on afterwards. Prices are
grouped by their exact signed line before anything downstream sees them, so a
sandwich cannot be assembled in the first place — there is no code path that
compares two different lines. is_sandwich() is the named guard: it is asserted
against every group that gets built, and it is the function to call if you ever
compare two sides by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# (outcome name, line). The line is None for markets that have no line at all,
# which in practice means head-to-head.
Side = Tuple[str, Optional[float]]

BookPrices = Dict[str, Dict[str, float]]


def _round(point) -> Optional[float]:
    """Lines arrive as JSON numbers and are compared for exact equality, so
    they are pinned to three decimals first. Real lines are quarters at worst;
    without this, 40.5 read from two responses could fail to match on a float
    representation difference and silently split one market into two."""
    if point is None:
        return None
    try:
        return round(float(point), 3)
    except (TypeError, ValueError):
        return None


def gap(a: Side, b: Side) -> Optional[float]:
    """Room between two sides, in points.

    Zero is a clean hedge: exactly one side wins. Negative is a sandwich, where
    a result landing in the gap loses both. Positive is a middle, where such a
    result wins both — pleasant, but it is not a hedge and must not be staked
    as one, because the odds no longer describe complementary events.

    None means the two sides are not opposable at all and nothing may be
    inferred from them.
    """
    (na, pa), (nb, pb) = a, b
    if na == nb:
        return None                       # the same side twice is not a hedge
    if pa is None and pb is None:
        return 0.0                        # h2h: complementary by name alone
    if pa is None or pb is None:
        return None                       # one lined, one not — different bets

    lowered = {na.lower(), nb.lower()}
    if lowered == {"over", "under"}:
        over, under = (pa, pb) if na.lower() == "over" else (pb, pa)
        # Under sits above Over by this much. Under 41.5 v Over 40.5 leaves a
        # 1-point window that wins both; the reverse leaves one that loses both.
        return round(under - over, 3)

    # Spreads. The sides are two different teams carrying mirrored handicaps,
    # so a genuine hedge sums to zero: -1.5 and +1.5 partition every result.
    return round(pa + pb, 3)


def is_sandwich(a: Side, b: Side) -> bool:
    """Can backing both of these lose both?

    True for a genuine sandwich (Over 41.5 with Under 40.5, or two handicaps
    summing negative) and also True whenever the pair is not opposable at all,
    because "cannot be shown safe" and "unsafe" have to be handled the same way
    when money is on it."""
    g = gap(a, b)
    return g is None or g < 0


def is_middle(a: Side, b: Side) -> bool:
    """Can backing both of these win both? Not a hedge either — the staking
    maths assumes exactly one leg pays."""
    g = gap(a, b)
    return g is not None and g > 0


def opposes(a: Side, b: Side) -> bool:
    """The only safe relationship: exactly one of these two wins, always."""
    return gap(a, b) == 0.0


@dataclass
class SubMarket:
    """One market in one game at one exact line, as quoted by several books.

    Every book in `book_odds` is quoting the same two sides at the same line,
    which is what makes the prices comparable at all."""
    market_key: str                       # "h2h", "totals", "spreads"
    sides: Tuple[Side, Side]              # the two sides, sorted by name
    point: Optional[float]                # the line, None for h2h
    label: str                            # for display: "", "Total 50.5"
    can_push: bool                        # whole line: an exact result refunds
    book_odds: BookPrices

    @property
    def key(self) -> tuple:
        return (self.market_key, self.sides)


def _outcome_name(market_key: str, name: str, point: Optional[float]) -> str:
    """The name this side is known by downstream.

    Lines are folded into the outcome name rather than carried alongside it, so
    that every surface — terminal card, dashboard, Telegram message, the bot's
    restake — shows which line it is talking about without any of them needing
    to know that lines exist. A leg reading "Penrith Panthers -1.5" cannot be
    confirmed against the wrong market in the app."""
    if point is None or market_key == "h2h":
        return name
    if name.lower() in ("over", "under"):
        return f"{name} {point:g}"
    return f"{name} {point:+g}"           # spreads: the sign is the whole point


def _label(market_key: str, point: Optional[float]) -> str:
    if market_key == "h2h" or point is None:
        return ""
    if market_key == "totals":
        return f"Total {point:g}"
    if market_key == "spreads":
        return f"Line {abs(point):g}"
    return f"{market_key} {point:g}"


def _quote(mk: dict) -> Optional[Tuple[Dict[str, Optional[float]], Dict[str, float]]]:
    """({name: line}, {name: price}) for one book's quote of one market.

    None when a price is missing or malformed, which is a suspended side rather
    than a market we cannot understand. Outcome *count* is checked by the
    caller, because three outcomes means something different and is handled
    differently."""
    points: Dict[str, Optional[float]] = {}
    prices: Dict[str, float] = {}
    for oc in mk.get("outcomes") or []:
        name, price = oc.get("name"), oc.get("price")
        if name is None or isinstance(price, bool) or not isinstance(price, (int, float)):
            return None
        if name in prices:
            return None                   # the same side twice: unreadable
        points[name] = _round(oc.get("point"))
        prices[name] = float(price)
    if not prices:
        return None
    return points, prices


def submarkets(event: dict, market_keys: Sequence[str]) -> List[SubMarket]:
    """Every safely comparable two-outcome market in one event.

    Books quoting both sides define the markets, because only a two-sided quote
    establishes what the line actually is. A book quoting one side then joins an
    existing market if its side matches exactly — that price is perfectly
    backable, and dropping it would throw away half the board on books that
    suspend one side.

    A book listing three or more outcomes poisons that market for the whole
    event. This mirrors what evaluate(expect_outcomes=2) already does and is
    deliberately blunt: under the single `h2h` key some books price ice hockey
    including overtime (two-way) and others price regulation time (three-way,
    with a Draw). Backing one team at a two-way book and the other at a
    three-way book loses both legs when the game is drawn in regulation and the
    first team wins in overtime. Rather than reason about which books mean
    which, the whole market is dropped for that event.
    """
    wanted = set(market_keys)
    poisoned: set = set()
    groups: Dict[tuple, Dict[str, Dict[str, float]]] = {}
    singles: List[Tuple[str, str, Side, float]] = []

    for bk in event.get("bookmakers") or []:
        title = bk.get("title") or bk.get("key") or "?"
        for mk in bk.get("markets") or []:
            key = mk.get("key")
            if key not in wanted:
                continue
            n = len(mk.get("outcomes") or [])
            if n > 2:
                poisoned.add(key)         # a Draw leg, or a market we misread
                continue
            q = _quote(mk)
            if q is None:
                continue
            points, prices = q
            if n == 2:
                sides = tuple(sorted((name, points[name]) for name in prices))
                if key != "h2h" and any(p is None for _, p in sides):
                    # A lined market that arrived without its line. gap() reads
                    # two absent points as the head-to-head case and calls them
                    # complementary, which would merge every line a book quotes
                    # into one market — the exact sandwich this module exists to
                    # prevent, and with no line left to notice it by. The feed
                    # has always sent `point` for totals and spreads; if that
                    # ever stops, the market has to be dropped rather than
                    # guessed at.
                    continue
                if not opposes(sides[0], sides[1]):
                    # The book's own two sides do not partition the result:
                    # nothing here is hedgeable, and admitting it would let a
                    # one-sided book join a market that does not exist.
                    continue
                renamed = {_outcome_name(key, name, points[name]): price
                           for name, price in prices.items()}
                groups.setdefault((key, sides), {})[title] = renamed
            else:
                name = next(iter(prices))
                singles.append((key, title, (name, points[name]), prices[name]))

    for key, title, side, price in singles:
        # A lone price joins the market that already contains its exact side.
        # Ambiguity is impossible in practice — Over 50.5 belongs to the 50.5
        # market and nowhere else — but if it ever arose, guessing would be
        # guessing about which bet the money is on, so the price is dropped.
        hits = [k for k in groups if k[0] == key and side in k[1]]
        if len(hits) != 1:
            continue
        if title in groups[hits[0]]:
            continue                      # both sides already taken from here
        groups[hits[0]][title] = {_outcome_name(key, side[0], side[1]): price}

    out: List[SubMarket] = []
    for (key, sides), book_odds in groups.items():
        if key in poisoned:
            continue
        # Belt and braces: nothing that reaches the staking maths may be a
        # sandwich. Grouping on the exact signed sides already guarantees it —
        # this re-checks rather than trusting it. Deliberately not an assert:
        # `python -O` strips those, and a guard that money depends on must not
        # be removable by an interpreter flag.
        if is_sandwich(sides[0], sides[1]):        # pragma: no cover
            continue
        point = sides[0][1]
        pts = [p for _, p in sides if p is not None]
        out.append(SubMarket(
            market_key=key,
            sides=sides,
            point=point,
            label=_label(key, point),
            # A whole line refunds both stakes when the result lands exactly on
            # it. Not a loss, but not the profit promised either — and if two
            # books settle a push differently, one leg voids and the other
            # stands, which leaves a naked bet.
            can_push=any(float(p) == int(float(p)) for p in pts),
            book_odds=book_odds,
        ))
    out.sort(key=lambda s: (s.market_key, s.point if s.point is not None else 0))
    return out
