"""Unit tests for arb.core — the arbitrage mathematics."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbtool.core import (  # noqa: E402
    Arb, allocate, apply_stake_limits, best_odds_per_outcome, evaluate,
    market_from_event, recommend_first_leg, unhedged_exposure,
)


class TestBestOdds(unittest.TestCase):
    def test_picks_highest_per_outcome(self):
        best = best_odds_per_outcome({
            "SportsBet": {"A": 2.10, "B": 1.80},
            "Ladbrokes": {"A": 1.85, "B": 2.05},
        })
        self.assertEqual(best, {"A": (2.10, "SportsBet"), "B": (2.05, "Ladbrokes")})

    def test_rejects_suspended_and_malformed(self):
        best = best_odds_per_outcome({
            "B1": {"A": None, "B": 0.5, "C": 1.0},
            "B2": {"A": 2.2, "B": 1.9, "C": float("nan")},
        })
        self.assertEqual(best, {"A": (2.2, "B2"), "B": (1.9, "B2")})

    def test_ignores_non_numeric(self):
        best = best_odds_per_outcome({"B1": {"A": "abc", "B": 2.0}})
        self.assertEqual(best, {"B": (2.0, "B1")})


class TestEvaluate(unittest.TestCase):
    def test_two_way_arb(self):
        arb = evaluate({
            "SportsBet": {"A": 2.10, "B": 1.80},
            "Ladbrokes": {"A": 1.85, "B": 2.05},
        })
        self.assertIsNotNone(arb)
        self.assertTrue(arb.is_arb)
        expected = 1/2.10 + 1/2.05
        self.assertAlmostEqual(arb.inverse_sum, expected, places=12)
        self.assertAlmostEqual(arb.margin, 1/expected - 1, places=12)
        self.assertEqual(arb.legs["A"].book, "SportsBet")
        self.assertEqual(arb.legs["B"].book, "Ladbrokes")
        self.assertFalse(arb.single_book)

    def test_efficient_market_is_not_arb(self):
        arb = evaluate({"B1": {"A": 1.90, "B": 1.90}, "B2": {"A": 1.87, "B": 1.93}})
        self.assertIsNotNone(arb)
        self.assertFalse(arb.is_arb)
        self.assertLess(arb.margin, 0)

    def test_exactly_fair_market_is_not_arb(self):
        arb = evaluate({"B1": {"A": 2.0, "B": 1.5}, "B2": {"A": 1.5, "B": 2.0}})
        self.assertAlmostEqual(arb.inverse_sum, 1.0, places=12)
        self.assertFalse(arb.is_arb)   # strict inequality: S == 1 makes nothing

    def test_three_way_arb(self):
        arb = evaluate({
            "B1": {"Home": 3.10, "Draw": 3.20, "Away": 2.90},
            "B2": {"Home": 2.80, "Draw": 3.60, "Away": 3.00},
            "B3": {"Home": 2.95, "Draw": 3.30, "Away": 3.50},
        })
        self.assertTrue(arb.is_arb)
        self.assertAlmostEqual(arb.inverse_sum, 1/3.10 + 1/3.60 + 1/3.50, places=12)
        self.assertEqual(arb.legs["Draw"].odds, 3.60)

    def test_partial_coverage_rejected(self):
        # 'B' is named but has no usable price -> that result is uncovered
        self.assertIsNone(evaluate({"B1": {"A": 2.5}, "B2": {"A": 2.4, "B": 1.0}}))

    def test_expect_outcomes_rejects_stray_draw(self):
        odds = {
            "SportsBet": {"Kings": 2.05, "Wildcats": 1.85},
            "TAB": {"Kings": 1.90, "Wildcats": 2.02, "Draw": 15.0},
        }
        self.assertIsNone(evaluate(odds, expect_outcomes=2))
        self.assertIsNotNone(evaluate(odds))          # unconstrained still evaluates

    def test_single_outcome_rejected(self):
        self.assertIsNone(evaluate({"B1": {"A": 5.0}, "B2": {"A": 4.0}}))

    def test_single_book_flagged(self):
        arb = evaluate({"Solo": {"A": 2.10, "B": 2.10}, "Other": {"A": 1.5, "B": 1.5}})
        self.assertTrue(arb.single_book)   # both best prices at one book

    def test_commission_erodes_margin(self):
        odds = {"B1": {"A": 2.10, "B": 1.80}, "B2": {"A": 1.85, "B": 2.05}}
        m0 = evaluate(odds, commission_pct=0).margin
        m5 = evaluate(odds, commission_pct=5).margin
        self.assertLess(m5, m0)

    def test_commission_can_kill_a_thin_arb(self):
        odds = {"B1": {"A": 2.02, "B": 1.90}, "B2": {"A": 1.90, "B": 2.02}}
        self.assertTrue(evaluate(odds).is_arb)
        self.assertFalse(evaluate(odds, commission_pct=5).is_arb)


class TestPerBookCommission(unittest.TestCase):
    """Betfair charges commission on winnings; the corporates do not. One
    global rate is wrong whichever value it takes, so the rate is per book —
    and it has to change which book wins an outcome, not only what it pays."""

    MARKET = {"Betfair":   {"A": 2.20, "B": 1.90},
              "SportsBet": {"A": 2.16, "B": 1.95}}
    COMM = {"Betfair": 5.0}

    def test_commission_decides_which_book_wins_the_outcome(self):
        """2.20 less 5% pays 2.14, so 2.16 with no commission is the better
        leg. Ranking on the headline price would send money to Betfair and
        report a profit that never arrives."""
        free = evaluate(self.MARKET, expect_outcomes=2)
        self.assertEqual(free.legs["A"].book, "Betfair")
        charged = evaluate(self.MARKET, commission_pct=self.COMM, expect_outcomes=2)
        self.assertEqual(charged.legs["A"].book, "SportsBet")

    def test_only_the_charging_book_is_penalised(self):
        m = {"Betfair": {"A": 3.00}, "SportsBet": {"B": 3.00}}
        arb = evaluate(m, commission_pct=self.COMM, expect_outcomes=2)
        self.assertAlmostEqual(arb.legs["A"].effective_odds, 1 + 2.0 * 0.95)
        self.assertAlmostEqual(arb.legs["B"].effective_odds, 3.00)

    def test_returns_are_net_not_the_slip_figure(self):
        """The bookmaker's slip shows gross. Every guaranteed number in this
        module must be built from what actually lands in the account, or
        worst_profit is overstated by exactly the commission."""
        m = {"Betfair": {"A": 3.00}, "SportsBet": {"B": 3.00}}
        arb = evaluate(m, commission_pct=self.COMM, expect_outcomes=2)
        allocate(arb, 1000.0, increment=1.0)
        leg = arb.legs["A"]
        self.assertAlmostEqual(leg.gross_returns, leg.stake * 3.00)
        self.assertAlmostEqual(leg.returns, leg.stake * (1 + 2.0 * 0.95))
        self.assertLess(leg.returns, leg.gross_returns)

    def test_commission_can_turn_an_arb_into_a_loss(self):
        """The failure this change exists to prevent: an edge that looks real
        at zero commission and settles negative once Betfair takes its cut."""
        m = {"Betfair": {"A": 2.02}, "SportsBet": {"B": 2.02}}
        self.assertTrue(evaluate(m, expect_outcomes=2).is_arb)
        charged = evaluate(m, commission_pct={"Betfair": 5.0}, expect_outcomes=2)
        self.assertFalse(charged.is_arb)
        allocate(charged, 2000.0, increment=1.0)
        self.assertLess(charged.worst_profit, 0)

    def test_a_flat_number_still_applies_to_every_book(self):
        """Back-compatible: the old single-rate call must behave as before."""
        flat = evaluate(self.MARKET, commission_pct=5.0, expect_outcomes=2)
        for leg in flat.legs.values():
            self.assertAlmostEqual(leg.commission_pct, 5.0)

    def test_books_absent_from_the_mapping_charge_nothing(self):
        arb = evaluate(self.MARKET, commission_pct={"Betfair": 5.0},
                       expect_outcomes=2)
        self.assertAlmostEqual(arb.legs["B"].commission_pct, 0.0)

    def test_stakes_are_split_on_net_odds(self):
        """A leg staked on its gross price is staked wrong: the hedge would not
        pay equally, and the shortfall lands on the commission-charging side."""
        m = {"Betfair": {"A": 2.60}, "SportsBet": {"B": 2.60}}
        arb = evaluate(m, commission_pct=self.COMM, expect_outcomes=2)
        allocate(arb, 5000.0, increment=1.0)
        # Net returns must be close to equal; gross must not be.
        self.assertLess(abs(arb.legs["A"].returns - arb.legs["B"].returns), 3.0)
        self.assertGreater(
            abs(arb.legs["A"].gross_returns - arb.legs["B"].gross_returns), 3.0)


class TestAllocate(unittest.TestCase):
    def setUp(self):
        self.arb = evaluate({
            "SportsBet": {"A": 2.10, "B": 1.80},
            "Ladbrokes": {"A": 1.85, "B": 2.05},
        })

    def test_unrounded_equalises_returns(self):
        allocate(self.arb, 1000.0, increment=0)
        rets = [leg.returns for leg in self.arb.legs.values()]
        self.assertAlmostEqual(min(rets), max(rets), places=8)
        self.assertAlmostEqual(self.arb.total_stake, 1000.0, places=8)
        self.assertGreater(self.arb.worst_profit, 0)

    def test_never_exceeds_bankroll(self):
        for bankroll in (150, 137.77, 1000, 33.33):
            for inc in (0.01, 0.5, 1.0, 5.0):
                a = evaluate({"B1": {"A": 2.10, "B": 1.80},
                              "B2": {"A": 1.85, "B": 2.05}})
                allocate(a, bankroll, increment=inc)
                self.assertLessEqual(a.total_stake, bankroll + 1e-9,
                                     f"bankroll={bankroll} inc={inc}")

    def test_whole_dollar_rounding_keeps_profit_positive(self):
        allocate(self.arb, 150.0, increment=1.0)
        self.assertEqual(self.arb.total_stake, round(self.arb.total_stake))
        self.assertGreater(self.arb.worst_profit, 0)
        # every leg is a whole number of dollars
        for leg in self.arb.legs.values():
            self.assertAlmostEqual(leg.stake, round(leg.stake), places=9)

    def test_remainder_goes_to_weakest_leg(self):
        # With a coarse increment the leftover must lift the lowest return,
        # not simply pile onto leg one.
        allocate(self.arb, 150.0, increment=5.0)
        rets = sorted(leg.returns for leg in self.arb.legs.values())
        # worst return should still beat outlay
        self.assertGreater(rets[0], self.arb.total_stake)

    def test_coarse_increment_can_destroy_a_thin_edge(self):
        thin = evaluate({"B1": {"A": 2.01, "B": 1.99}, "B2": {"A": 1.99, "B": 2.01}})
        self.assertTrue(thin.is_arb)
        allocate(thin, 20.0, increment=5.0)
        # The point of the test: realised margin is what matters, and coarse
        # rounding on a small bankroll can push it below the theoretical margin.
        self.assertLessEqual(thin.realised_margin, thin.margin + 1e-9)

    def test_zero_bankroll(self):
        allocate(self.arb, 0)
        self.assertEqual(self.arb.total_stake, 0)


class TestStakeLimits(unittest.TestCase):
    def test_cap_scales_whole_position_preserving_hedge(self):
        arb = evaluate({"SportsBet": {"A": 2.10, "B": 1.80},
                        "Ladbrokes": {"A": 1.85, "B": 2.05}})
        allocate(arb, 1000.0, increment=0.01)
        apply_stake_limits(arb, {"SportsBet": 50.0}, increment=0.01)
        self.assertLessEqual(arb.legs["A"].stake, 50.0 + 1e-9)
        self.assertGreater(arb.worst_profit, 0)     # still an arb, just smaller
        self.assertLess(arb.total_stake, 1000.0)

    def test_no_limits_is_noop(self):
        arb = evaluate({"B1": {"A": 2.10, "B": 1.80}, "B2": {"A": 1.85, "B": 2.05}})
        allocate(arb, 150.0)
        before = arb.total_stake
        apply_stake_limits(arb, {})
        self.assertAlmostEqual(arb.total_stake, before, places=9)


class TestExecutionGuidance(unittest.TestCase):
    def test_first_leg_is_the_longest_price(self):
        arb = evaluate({"SportsBet": {"A": 2.10, "B": 1.80},
                        "Ladbrokes": {"A": 1.85, "B": 2.05}})
        self.assertEqual(recommend_first_leg(arb), "A")   # 2.10 > 2.05

    def test_unhedged_exposure_is_one_leg_not_bankroll(self):
        arb = evaluate({"SportsBet": {"A": 2.10, "B": 1.80},
                        "Ladbrokes": {"A": 1.85, "B": 2.05}})
        allocate(arb, 150.0, increment=1.0)
        first = recommend_first_leg(arb)
        exposure = unhedged_exposure(arb, first)
        self.assertLess(exposure, 150.0)
        self.assertGreater(exposure, 0)
        self.assertAlmostEqual(exposure, arb.legs[first].stake, places=9)


class TestMarketFromEvent(unittest.TestCase):
    def test_extracts_h2h_only(self):
        event = {
            "bookmakers": [
                {"key": "sportsbet", "title": "SportsBet", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Panthers", "price": 2.12},
                        {"name": "Storm", "price": 1.78}]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 1.9, "point": 40.5}]}]},
                {"key": "ladbrokes_au", "title": "Ladbrokes", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Panthers", "price": 1.80},
                        {"name": "Storm", "price": 2.08}]}]},
            ]}
        odds = market_from_event(event)
        self.assertEqual(set(odds), {"SportsBet", "Ladbrokes"})
        self.assertNotIn("Over", odds["SportsBet"])
        arb = evaluate(odds, expect_outcomes=2)
        self.assertTrue(arb.is_arb)
        self.assertAlmostEqual(arb.margin, 1/(1/2.12 + 1/2.08) - 1, places=12)

    def test_handles_empty_and_missing_keys(self):
        self.assertEqual(market_from_event({}), {})
        self.assertEqual(market_from_event({"bookmakers": None}), {})
        self.assertEqual(market_from_event({"bookmakers": [{"title": "X"}]}), {})


class TestEndToEndRealistic(unittest.TestCase):
    def test_nrl_arb_at_150_bankroll(self):
        """The scenario the user actually cares about."""
        arb = evaluate({
            "SportsBet": {"Penrith Panthers": 2.12, "Melbourne Storm": 1.78},
            "Ladbrokes": {"Penrith Panthers": 1.80, "Melbourne Storm": 2.08},
            "TAB":       {"Penrith Panthers": 1.85, "Melbourne Storm": 1.95},
        }, expect_outcomes=2)
        self.assertTrue(arb.is_arb)
        self.assertAlmostEqual(arb.margin * 100, 4.99, places=1)

        allocate(arb, 150.0, increment=1.0)
        self.assertLessEqual(arb.total_stake, 150.0)
        self.assertGreater(arb.worst_profit, 0)
        self.assertEqual(arb.legs["Penrith Panthers"].book, "SportsBet")
        self.assertEqual(arb.legs["Melbourne Storm"].book, "Ladbrokes")
        # Two different books -> actually placeable
        self.assertFalse(arb.single_book)


if __name__ == "__main__":
    unittest.main(verbosity=2)
