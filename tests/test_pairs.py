"""Tests for the pairwise bookmaker analysis."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbtool.pairs import analyse_game, pair_matrix  # noqa: E402

BOOKS = ["Sportsbet", "Ladbrokes", "TAB", "Neds"]

# The worked example: Sportsbet long on Penrith, Neds long on Storm.
GAME = {
    "Sportsbet": {"Penrith Panthers": 2.12, "Melbourne Storm": 1.78},
    "Ladbrokes": {"Penrith Panthers": 1.95, "Melbourne Storm": 1.92},
    "TAB":       {"Penrith Panthers": 1.85, "Melbourne Storm": 1.98},
    "Neds":      {"Penrith Panthers": 1.80, "Melbourne Storm": 2.08},
}

# Nothing crosses here.
TIGHT = {
    "Sportsbet": {"A": 1.90, "B": 1.90},
    "Ladbrokes": {"A": 1.87, "B": 1.93},
    "TAB":       {"A": 1.92, "B": 1.88},
    "Neds":      {"A": 1.88, "B": 1.94},
}


class TestPairMatrix(unittest.TestCase):
    def test_four_books_gives_six_pairings(self):
        self.assertEqual(len(pair_matrix(GAME, BOOKS)), 6)

    def test_five_books_gives_ten(self):
        g = dict(GAME, PointsBet={"Penrith Panthers": 1.9, "Melbourne Storm": 1.9})
        self.assertEqual(len(pair_matrix(g, BOOKS + ["PointsBet"])), 10)

    def test_sorted_best_first(self):
        ps = pair_matrix(GAME, BOOKS)
        self.assertEqual(ps, sorted(ps, key=lambda p: -p.margin_pct))

    def test_each_margin_matches_hand_calculation(self):
        for p in pair_matrix(GAME, BOOKS):
            o1 = max(GAME[p.book_a]["Penrith Panthers"], GAME[p.book_b]["Penrith Panthers"])
            o2 = max(GAME[p.book_a]["Melbourne Storm"], GAME[p.book_b]["Melbourne Storm"])
            expected = (1/(1/o1 + 1/o2) - 1) * 100
            self.assertAlmostEqual(p.margin_pct, expected, places=9, msg=p.label)

    def test_books_not_in_market_are_skipped(self):
        ps = pair_matrix(GAME, BOOKS + ["Nonexistent Book"])
        self.assertEqual(len(ps), 6)
        self.assertNotIn("Nonexistent", " ".join(p.label for p in ps))

    def test_restricting_books_restricts_pairings(self):
        self.assertEqual(len(pair_matrix(GAME, ["Sportsbet", "Neds"])), 1)


class TestGameAnalysis(unittest.TestCase):
    def test_best_pair_is_sportsbet_neds(self):
        ga = analyse_game(GAME, "h2h", BOOKS)
        self.assertEqual({ga.best_pair.book_a, ga.best_pair.book_b}, {"Sportsbet", "Neds"})
        self.assertAlmostEqual(ga.best_margin_pct, 4.9905, places=3)

    def test_depth_counts_working_pairings(self):
        ga = analyse_game(GAME, "h2h", BOOKS)
        self.assertEqual(ga.depth, 4)
        self.assertTrue(ga.has_arb)

    def test_best_pair_equals_all_books_best_price(self):
        ga = analyse_game(GAME, "h2h", BOOKS)
        self.assertAlmostEqual(ga.pairs[0].margin_pct, ga.best_overall.margin * 100, places=9)

    def test_no_arb_game(self):
        ga = analyse_game(TIGHT, "h2h", BOOKS)
        self.assertFalse(ga.has_arb)
        self.assertEqual(ga.depth, 0)
        self.assertEqual(ga.arb_pairs, [])

    def test_best_pair_is_none_when_nothing_crosses(self):
        """The trap: PairResult.arb is an object and is truthy even for a
        losing pairing. best_pair must test is_arb, or the least-bad loser
        gets presented as a recommendation."""
        ga = analyse_game(TIGHT, "h2h", BOOKS)
        self.assertIsNone(ga.best_pair)
        self.assertIsNotNone(ga.top_pair)          # still available for display
        self.assertLess(ga.top_pair.margin_pct, 0)
        self.assertIsNotNone(ga.top_pair.arb)      # the object exists...
        self.assertFalse(ga.top_pair.is_arb)       # ...but it is not an arb

    def test_single_book_dominance_not_counted(self):
        """One book best on both sides is not a cross-book arb: bookmakers do
        not let you back both sides of a market with them."""
        g = {"Solo": {"A": 2.20, "B": 2.20}, "Other": {"A": 1.50, "B": 1.50}}
        ga = analyse_game(g, "h2h", ["Solo", "Other"])
        self.assertLess(ga.pairs[0].arb.inverse_sum, 1.0)   # maths says yes
        self.assertFalse(ga.pairs[0].is_arb)                # judgement says no
        self.assertFalse(ga.has_arb)
        self.assertIsNone(ga.best_pair)

    def test_fewer_than_two_books_returns_none(self):
        self.assertIsNone(analyse_game({"Sportsbet": {"A": 2.0, "B": 2.0}}, "h2h", BOOKS))

    def test_commission_erodes_every_pairing(self):
        plain = analyse_game(GAME, "h2h", BOOKS)
        comm = analyse_game(GAME, "h2h", BOOKS, commission_pct=5)
        self.assertLess(comm.best_margin_pct, plain.best_margin_pct)
        self.assertLessEqual(comm.depth, plain.depth)

    def test_matrix_is_symmetric(self):
        """Order of the two books must not change the answer."""
        ga = analyse_game(GAME, "h2h", BOOKS)
        fwd = pair_matrix(GAME, ["Sportsbet", "Neds"])[0]
        rev = pair_matrix(GAME, ["Neds", "Sportsbet"])[0]
        self.assertAlmostEqual(fwd.margin_pct, rev.margin_pct, places=12)


class TestDepthIsMeaningful(unittest.TestCase):
    def test_fragile_versus_robust(self):
        """Depth is the point of the matrix: one working pairing is fragile,
        several give you somewhere to go when a price moves."""
        fragile = {"A": {"X": 2.05, "Y": 1.80}, "B": {"X": 1.80, "Y": 2.05},
                   "C": {"X": 1.85, "Y": 1.85}, "D": {"X": 1.83, "Y": 1.86}}
        ga = analyse_game(fragile, "h2h", ["A", "B", "C", "D"])
        self.assertEqual(ga.depth, 1)
        ga2 = analyse_game(GAME, "h2h", BOOKS)
        self.assertGreater(ga2.depth, ga.depth)


if __name__ == "__main__":
    unittest.main(verbosity=2)
