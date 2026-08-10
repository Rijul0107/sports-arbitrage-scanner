"""
Arbitrage mathematics.

Pure functions, no I/O, fully unit-tested. Everything else in this package
depends on this module; this module depends on nothing.

The method
----------
For a market with outcomes 1..n, take the best (highest) decimal odds
available for each outcome across all bookmakers:

    S = sum(1 / best_odds_i)

The market is arbitrageable iff S < 1. Backing every outcome in proportion
to its inverse odds guarantees the same return whichever outcome wins:

    stake fraction on i = (1 / odds_i) / S
    return on total stake T = T / S
    profit = T * (1/S - 1)

Everything below is that identity plus the corrections that make it survive
contact with a real bookmaker: commission, rounding to placeable stake
increments, and per-book stake ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# {bookmaker_name: {outcome_name: decimal_odds}}
BookOdds = Dict[str, Dict[str, float]]


@dataclass
class Leg:
    """One side of an arbitrage: what to back, where, at what price."""
    outcome: str
    book: str
    odds: float
    stake: float = 0.0
    # Commission this book charges on winnings. Per leg, not per market:
    # Betfair charges it and the corporates do not, so one number for the whole
    # position is wrong in both directions.
    commission_pct: float = 0.0

    @property
    def effective_odds(self) -> float:
        """The price after this book's commission on winnings.

        Commission is levied on the profit, not on the stake returned, so
        2.20 at 5% pays 1 + 1.20*0.95 = 2.14."""
        return 1.0 + (self.odds - 1.0) * (1.0 - self.commission_pct / 100.0)

    @property
    def gross_returns(self) -> float:
        """What the bookmaker's slip will say, before commission is taken."""
        return self.stake * self.odds

    @property
    def returns(self) -> float:
        """What actually lands in the account.

        Net, not gross. Every guaranteed figure in this module is built from
        this, so a commission-charging leg must not report the slip's number —
        that would overstate worst_profit by the commission and turn a losing
        position into an apparent arbitrage."""
        return self.stake * self.effective_odds


@dataclass
class Arb:
    """The result of evaluating one market."""
    outcomes: List[str]
    legs: Dict[str, Leg]           # outcome -> Leg
    inverse_sum: float             # S
    margin: float                  # 1/S - 1, as a fraction; negative if no arb
    n_books_in_market: int
    commission_pct: float = 0.0

    @property
    def is_arb(self) -> bool:
        return self.inverse_sum < 1.0

    @property
    def books_used(self) -> List[str]:
        return [self.legs[o].book for o in self.outcomes]

    @property
    def single_book(self) -> bool:
        """True when one book holds the best price on every outcome.

        Practically unplaceable: bookmakers do not let you back both sides of
        the same market with them. Worth flagging rather than silently
        reporting an arb nobody can take."""
        return len(set(self.books_used)) < len(self.outcomes)

    @property
    def total_stake(self) -> float:
        return sum(leg.stake for leg in self.legs.values())

    @property
    def worst_return(self) -> float:
        """Lowest return across outcomes. After rounding, legs no longer pay
        identically, so this is the number that actually matters."""
        return min(leg.returns for leg in self.legs.values())

    @property
    def best_return(self) -> float:
        return max(leg.returns for leg in self.legs.values())

    @property
    def worst_profit(self) -> float:
        """Guaranteed profit: the least you make whichever outcome lands.
        Negative means the rounding destroyed the edge."""
        return self.worst_return - self.total_stake

    @property
    def realised_margin(self) -> float:
        """Worst-case profit as a fraction of outlay, post-rounding."""
        return self.worst_profit / self.total_stake if self.total_stake else 0.0


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def commission_for(commission, book: str) -> float:
    """This book's commission percentage.

    Accepts a plain number, applied to every book, or a {book: pct} mapping
    with books absent from it charging nothing. The mapping is the honest
    shape: Betfair charges commission and the corporates do not, so a single
    global rate is wrong whichever value it takes — zero overstates a Betfair
    leg's winnings, and Betfair's rate penalises every corporate leg."""
    if isinstance(commission, dict):
        return float(commission.get(book, 0.0) or 0.0)
    return float(commission or 0.0)


def best_odds_per_outcome(book_odds: BookOdds,
                          commission=0.0) -> Dict[str, Tuple[float, str]]:
    """{outcome: (best_odds, bookmaker)}, where "best" is after commission.

    Ranking on the raw price would be wrong wherever commission differs
    between books: Betfair at 2.20 less 5% pays 2.14, so a corporate at 2.16
    is the better leg despite the shorter headline. The odds returned are the
    raw ones — that is what the betting slip will show and what the user must
    confirm in the app — but the choice between books is made on what actually
    lands in the account.

    Prices at or below 1.00 are treated as suspended/malformed and ignored:
    a decimal price of 1.00 returns the stake, which is not a real market."""
    best: Dict[str, Tuple[float, str]] = {}
    ranked: Dict[str, float] = {}
    for book, outcomes in book_odds.items():
        for outcome, odds in outcomes.items():
            if odds is None:
                continue
            try:
                o = float(odds)
            except (TypeError, ValueError):
                continue
            if not (o > 1.0) or o != o:      # also rejects NaN
                continue
            net = 1.0 + (o - 1.0) * (1.0 - commission_for(commission, book) / 100.0)
            if outcome not in best or net > ranked[outcome]:
                best[outcome] = (o, book)
                ranked[outcome] = net
    return best


def evaluate(
    book_odds: BookOdds,
    commission_pct: float = 0.0,
    expect_outcomes: Optional[int] = None,
) -> Optional[Arb]:
    """Evaluate one market. Returns None when the market cannot be assessed.

    commission_pct: commission applied to winnings only (the exchange
        convention). Either a number applied to every book, or a
        {book: pct} mapping — books absent from the mapping charge nothing.
        5.0 means 5%. The mapping is what real markets look like: Betfair
        charges commission, the corporates do not.
    expect_outcomes: require exactly this many outcomes. Pass 2 for win/loss
        sports so that a bookmaker erroneously listing a draw leg causes the
        market to be skipped rather than mis-evaluated.

    Returns None if: fewer than two outcomes; the outcome count does not match
    expect_outcomes; or any named outcome lacks a usable price anywhere
    (partial coverage cannot be arbitraged, since one result is uncovered)."""
    all_outcomes = {o for prices in book_odds.values() for o in prices}
    if len(all_outcomes) < 2:
        return None
    if expect_outcomes is not None and len(all_outcomes) != expect_outcomes:
        return None

    best = best_odds_per_outcome(book_odds, commission_pct)
    if set(best) != all_outcomes:
        return None

    outcomes = sorted(best)
    legs = {
        o: Leg(outcome=o, book=best[o][1], odds=best[o][0],
               commission_pct=commission_for(commission_pct, best[o][1]))
        for o in outcomes
    }
    # S is summed over effective odds, so a commission-charging leg raises S
    # and shrinks the margin exactly as it shrinks the money.
    inverse_sum = sum(1.0 / legs[o].effective_odds for o in outcomes)
    return Arb(
        outcomes=outcomes,
        legs=legs,
        inverse_sum=inverse_sum,
        margin=(1.0 / inverse_sum) - 1.0,
        n_books_in_market=len(book_odds),
        # Kept for reporting only. The maths reads Leg.commission_pct, because
        # a market can now carry two different rates at once.
        commission_pct=commission_pct,
    )


# ---------------------------------------------------------------------------
# Staking
# ---------------------------------------------------------------------------

def _effective_odds(arb: Arb) -> Dict[str, float]:
    """Per leg, not per market — the legs can sit at different rates."""
    return {o: leg.effective_odds for o, leg in arb.legs.items()}


def allocate(arb: Arb, bankroll: float, increment: float = 0.01) -> Arb:
    """Split `bankroll` across the legs and round to a placeable increment.

    increment: the smallest stake a bookmaker will accept, e.g. 0.01 for cents,
        1.0 for whole dollars. Rounding is the step every naive arbitrage
        calculator skips, and it can turn a thin edge negative.

    Rounding is *down* on every leg, then the remainder is given to whichever
    leg's return is currently lowest. Rounding down keeps total outlay at or
    under the bankroll, which is what a fixed budget requires; the remainder
    pass lifts the worst-case return rather than letting it sag.

    Mutates and returns `arb`."""
    if bankroll <= 0:
        for leg in arb.legs.values():
            leg.stake = 0.0
        return arb

    eff = _effective_odds(arb)
    ideal = {o: bankroll * (1.0 / eff[o]) / arb.inverse_sum for o in arb.outcomes}

    if increment <= 0:
        for o in arb.outcomes:
            arb.legs[o].stake = ideal[o]
        return arb

    # Round every leg down to the increment.
    for o in arb.outcomes:
        steps = int(ideal[o] / increment + 1e-9)
        arb.legs[o].stake = round(steps * increment, 10)

    # Hand out whatever is left, one increment at a time, always to the leg
    # with the lowest return — this maximises the guaranteed (worst-case) payout.
    leftover = bankroll - sum(arb.legs[o].stake for o in arb.outcomes)
    guard = 0
    while leftover >= increment - 1e-9 and guard < 100000:
        target = min(arb.outcomes, key=lambda o: arb.legs[o].stake * eff[o])
        arb.legs[target].stake = round(arb.legs[target].stake + increment, 10)
        leftover -= increment
        guard += 1

    return arb


def apply_stake_limits(
    arb: Arb, limits: Dict[str, float], increment: float = 0.01
) -> Arb:
    """Rescale every leg so no leg exceeds its book's maximum stake.

    limits: {bookmaker: max_stake}. Books not listed are unconstrained.

    A bookmaker capping one leg does not break the arb — it caps its size.
    The whole position scales down by the tightest binding ratio, preserving
    the hedge. Mutates and returns `arb`."""
    ratio = 1.0
    for o in arb.outcomes:
        leg = arb.legs[o]
        cap = limits.get(leg.book)
        if cap is not None and leg.stake > 0 and cap < leg.stake:
            ratio = min(ratio, cap / leg.stake)

    if ratio < 1.0:
        scaled = sum(arb.legs[o].stake for o in arb.outcomes) * ratio
        allocate(arb, scaled, increment=increment)
    return arb


def unhedged_exposure(arb: Arb, first_leg: str) -> float:
    """How much is at risk if the first leg fills and the second never does.

    This is the real downside of arbitrage betting, and it is not the total
    bankroll — it is the stake sitting on one unhedged outcome."""
    return arb.legs[first_leg].stake


def recommend_first_leg(arb: Arb) -> str:
    """Which leg to place first.

    Place the leg most likely to disappear. In practice that is the one at the
    longest price: long odds move further and faster on the same shift in
    implied probability, and the books offering an outlier long price are the
    ones about to correct it. Getting the fragile leg down first means that if
    the second has moved you are unhedged on the *shorter* price, which is both
    a smaller stake and easier to lay off.

    Ranked on the raw price, deliberately, not the commission-adjusted one.
    This is about which quote is most likely to vanish, and an exchange price
    drifts on its own displayed number — commission has no bearing on that."""
    return max(arb.outcomes, key=lambda o: arb.legs[o].odds)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def market_from_event(event: dict, market_key: str = "h2h") -> BookOdds:
    """Pull {book: {outcome: odds}} out of one Odds API event object."""
    out: BookOdds = {}
    for bk in event.get("bookmakers", []) or []:
        for mk in bk.get("markets", []) or []:
            if mk.get("key") != market_key:
                continue
            prices = {}
            for oc in mk.get("outcomes", []) or []:
                name, price = oc.get("name"), oc.get("price")
                if name is not None and isinstance(price, (int, float)):
                    prices[name] = float(price)
            if prices:
                out[bk.get("title") or bk.get("key") or "?"] = prices
    return out
