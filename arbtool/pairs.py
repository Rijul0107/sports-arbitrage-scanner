"""
Pairwise bookmaker analysis.

The question this module answers: given a game and a set of bookmakers you hold
accounts with, *which pairing* gives you the biggest edge — and how many
pairings work at all?

That second number matters more than it looks. An arbitrage available through
exactly one pairing is fragile: one price moves and it is gone. The same margin
available through four pairings is robust, and it also gives you somewhere to
go when your first choice limits your stake or the price shortens while you are
typing.

Method
------
For each unordered pair of books, restrict the market to just those two and
take the best price per outcome within that restriction. That automatically
picks the optimal assignment of outcomes to books — no need to enumerate
orientations. A pair only counts if both books are actually used; if one book
holds the best price on every outcome, that is not a cross-book arbitrage and
is not placeable anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Sequence

from .core import Arb, BookOdds, evaluate


@dataclass
class PairResult:
    """One bookmaker pairing evaluated against one market."""
    book_a: str
    book_b: str
    arb: Optional[Arb]          # None when the pair cannot cover every outcome
    uses_both: bool             # False when one book holds every best price

    @property
    def margin_pct(self) -> float:
        """Positive is an arbitrage; negative is the overround you'd pay."""
        if self.arb is None:
            return float("-inf")
        return self.arb.margin * 100

    @property
    def is_arb(self) -> bool:
        return self.arb is not None and self.arb.is_arb and self.uses_both

    @property
    def label(self) -> str:
        return f"{self.book_a} + {self.book_b}"


def pair_matrix(book_odds: BookOdds,
                books: Optional[Sequence[str]] = None,
                commission_pct: float = 0.0,
                expect_outcomes: Optional[int] = None) -> List[PairResult]:
    """Every unordered pairing, best first.

    books: restrict to these bookmakers, in this order. Names not present in
        this market are skipped. Pass None to use whatever the market carries.
    """
    present = [b for b in (books or sorted(book_odds)) if b in book_odds]

    results: List[PairResult] = []
    for a, b in combinations(present, 2):
        restricted = {a: book_odds[a], b: book_odds[b]}
        arb = evaluate(restricted, commission_pct=commission_pct,
                       expect_outcomes=expect_outcomes)
        uses_both = bool(arb) and len({leg.book for leg in arb.legs.values()}) == 2
        results.append(PairResult(a, b, arb, uses_both))

    results.sort(key=lambda r: -r.margin_pct)
    return results


@dataclass
class GameAnalysis:
    """Everything worth knowing about one game across the tracked books."""
    market_key: str
    outcomes: List[str]
    book_odds: BookOdds
    pairs: List[PairResult]
    best_overall: Optional[Arb]     # best price per outcome across ALL books

    @property
    def best_pair(self) -> Optional[PairResult]:
        """The best pairing, but only when it is genuinely arbitrageable.

        Note the trap this guards: PairResult.arb is the Arb *object* and is
        truthy whenever the market could be evaluated at all, including when
        the margin is negative. The boolean is is_arb. Testing the wrong one
        marks the least-bad losing pairing as 'best', which is worse than
        useless — it looks like a recommendation."""
        if not self.pairs:
            return None
        return self.pairs[0] if self.pairs[0].is_arb else None

    @property
    def top_pair(self) -> Optional[PairResult]:
        """The least-bad pairing whether or not it crosses. For showing how
        close the market is getting."""
        return self.pairs[0] if self.pairs else None

    @property
    def arb_pairs(self) -> List[PairResult]:
        return [p for p in self.pairs if p.is_arb]

    @property
    def depth(self) -> int:
        """How many distinct pairings are arbitrageable.

        1 means fragile. 3 or more means you have fallbacks if a price moves
        or a book limits you."""
        return len(self.arb_pairs)

    @property
    def has_arb(self) -> bool:
        return self.depth > 0

    @property
    def best_margin_pct(self) -> float:
        return self.pairs[0].margin_pct if self.pairs else float("-inf")

    def books_in_best(self) -> List[str]:
        bp = self.best_pair
        return [bp.book_a, bp.book_b] if bp else []


def analyse_game(book_odds: BookOdds, market_key: str = "h2h",
                 books: Optional[Sequence[str]] = None,
                 commission_pct: float = 0.0) -> Optional[GameAnalysis]:
    """Full pairwise analysis of one market in one game."""
    present = [b for b in (books or sorted(book_odds)) if b in book_odds]
    if len(present) < 2:
        return None

    outcomes = sorted({o for b in present for o in book_odds[b]})
    if len(outcomes) < 2:
        return None
    n = len(outcomes)

    restricted = {b: book_odds[b] for b in present}
    pairs = pair_matrix(restricted, present, commission_pct, expect_outcomes=n)
    best_overall = evaluate(restricted, commission_pct=commission_pct,
                            expect_outcomes=n)

    return GameAnalysis(market_key=market_key, outcomes=outcomes,
                        book_odds=restricted, pairs=pairs,
                        best_overall=best_overall)


def best_market(analyses: Sequence[GameAnalysis]) -> Optional[GameAnalysis]:
    """Across every market in a game (h2h, totals, spreads...), the one with
    the biggest edge. This is 'the maximum arbitrage in this game'."""
    scored = [a for a in analyses if a and a.pairs]
    if not scored:
        return None
    return max(scored, key=lambda a: a.best_margin_pct)


def format_matrix(ga: GameAnalysis, width: int = 22) -> str:
    """The pairwise table, as text. Best pairing first."""
    lines = []
    head = f"{'PAIRING':<{width*2+3}} {'MARGIN':>9}  {'BEST PRICES':<34} DEPTH"
    lines.append(head)
    lines.append("-" * len(head))
    for p in ga.pairs:
        if p.arb is None:
            lines.append(f"{p.label:<{width*2+3}} {'no coverage':>9}")
            continue
        prices = "  ".join(
            f"{o[:14]} {p.arb.legs[o].odds:.2f} ({p.arb.legs[o].book[:9]})"
            for o in p.arb.outcomes)
        mark = "ARB" if p.is_arb else ""
        if not p.uses_both and p.arb.is_arb:
            mark = "1 book"
        lines.append(f"{p.label:<{width*2+3}} {p.margin_pct:>8.2f}%  {prices:<34} {mark}")
    return "\n".join(lines)
