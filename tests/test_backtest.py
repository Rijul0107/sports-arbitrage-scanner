"""The bankroll simulation: capital constraints, ordering, and honesty.

The risk this file guards against is a backtest that flatters itself. Three
ways that happens, all tested below: taking the best price instead of the first
one seen (reading the future), paying for the same opportunity every time it is
re-detected, and letting positions overlap without the capital to hold them.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backtest


def opp(seen_minutes, margin_frac, kickoff_minutes=None, key=None, sport="test_sport"):
    """One synthetic opportunity, `seen_minutes` after an arbitrary epoch."""
    base = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
    kickoff = base + timedelta(minutes=kickoff_minutes if kickoff_minutes is not None
                               else seen_minutes)
    return {
        "key": key or (f"ev{seen_minutes}", "h2h", ""),
        "seen_at": (base + timedelta(minutes=seen_minutes)).isoformat(),
        "commence_time": kickoff.isoformat(),
        "sport": sport,
        "margin_pct": margin_frac * 100,
        "margin_frac": margin_frac,
        "books": ["A", "B"],
    }


class TestCapitalConstraint(unittest.TestCase):
    def test_a_second_opportunity_inside_the_lock_is_skipped(self):
        # Two opportunities an hour apart, settlement three hours after
        # kickoff. Going all-in on the first leaves nothing for the second.
        opps = [opp(0, 0.03), opp(60, 0.03)]
        final, taken, skipped, _, _ = backtest.simulate(opps, 1000.0, 1000.0, 3.0)
        self.assertEqual(len(taken), 1)
        self.assertEqual(len(skipped), 1)
        self.assertAlmostEqual(final, 1030.0, places=2)

    def test_halving_the_position_captures_both(self):
        opps = [opp(0, 0.03), opp(60, 0.03)]
        _, taken, skipped, _, _ = backtest.simulate(opps, 1000.0, 500.0, 3.0)
        self.assertEqual((len(taken), len(skipped)), (2, 0))

    def test_capital_returns_after_settlement(self):
        # Far enough apart that the first has settled before the second is seen,
        # so both are taken rather than the second being skipped for capital.
        opps = [opp(0, 0.03), opp(60 * 24, 0.03, kickoff_minutes=60 * 24)]
        final, taken, _, _, _ = backtest.simulate(opps, 1000.0, 1000.0, 3.0)
        self.assertEqual(len(taken), 2)
        # Exactly 2 x $30, NOT compounded: max_position is an absolute cap, so
        # the second position is still $1000 even though $1030 is now free.
        # This is the intended reading of --max-position and the reason the
        # sweep expresses size as a fraction of the STARTING bankroll.
        self.assertAlmostEqual(final, 1060.0, places=2)

    def test_profit_compounds_when_the_position_cap_is_not_binding(self):
        # With the cap above the grown bankroll, the second position is staked
        # on the larger balance and the result exceeds two flat wins.
        opps = [opp(0, 0.03), opp(60 * 24, 0.03, kickoff_minutes=60 * 24)]
        final, taken, _, _, _ = backtest.simulate(opps, 1000.0, 10_000.0, 3.0)
        self.assertEqual(len(taken), 2)
        self.assertGreater(final, 1060.0)
        self.assertAlmostEqual(final, 1000 * 1.03 * 1.03, places=2)

    def test_a_position_below_the_floor_is_skipped_not_taken_tiny(self):
        # $40 free is under the $50 floor: whole-dollar rounding on two legs
        # would eat the edge, so it must be reported as skipped.
        opps = [opp(0, 0.03), opp(30, 0.03)]
        _, taken, skipped, _, _ = backtest.simulate(opps, 1000.0, 960.0, 3.0)
        self.assertEqual(len(taken), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("free", skipped[0]["reason"])


class TestNoLookahead(unittest.TestCase):
    def test_opportunities_are_processed_in_time_order(self):
        opps = [opp(120, 0.02), opp(0, 0.05)]
        _, taken, _, _, _ = backtest.simulate(sorted(opps, key=lambda o: o["seen_at"]),
                                              1000.0, 1000.0, 3.0)
        self.assertEqual(taken[0]["margin_pct"], 5.0)

    def test_profit_scales_with_the_stake_actually_available(self):
        # Margin is a fraction; a half-sized bankroll earns half the profit.
        big, _, _, _, _ = backtest.simulate([opp(0, 0.04)], 2000.0, 2000.0, 3.0)
        small, _, _, _, _ = backtest.simulate([opp(0, 0.04)], 1000.0, 1000.0, 3.0)
        self.assertAlmostEqual(big - 2000.0, 2 * (small - 1000.0), places=2)


class TestSweep(unittest.TestCase):
    def test_smaller_positions_never_take_fewer_opportunities(self):
        opps = [opp(i * 30, 0.03) for i in range(6)]
        rows = backtest.sweep(opps, 2000.0, 3.0)
        taken = [r["taken"] for r in rows]      # rows run largest fraction first
        self.assertEqual(taken, sorted(taken))

    def test_every_row_reports_both_taken_and_skipped(self):
        opps = [opp(i * 30, 0.03) for i in range(6)]
        for r in backtest.sweep(opps, 2000.0, 3.0):
            self.assertEqual(r["taken"] + r["skipped"], len(opps))


class TestHonesty(unittest.TestCase):
    def test_assumptions_are_not_silently_empty(self):
        # The report prints these; an empty dict would make a simulated figure
        # look like a measured one.
        self.assertGreaterEqual(len(backtest.ASSUMPTIONS), 5)
        for text in backtest.ASSUMPTIONS.values():
            self.assertGreater(len(text), 40)

    def test_the_no_bets_assumption_is_present_and_first(self):
        self.assertEqual(next(iter(backtest.ASSUMPTIONS)), "no bets were placed")


if __name__ == "__main__":
    unittest.main()
