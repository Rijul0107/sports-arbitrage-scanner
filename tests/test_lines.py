"""Line matching: the sandwich guard and the grouping that makes it structural.

Every test here is about one question — can this code ever put two prices in
front of the staking maths that are not opposite sides of the same bet? A
sandwich that reaches allocate() is not a failing test, it is a position where
both legs lose.
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbtool.lines import (
    gap, is_middle, is_sandwich, opposes, submarkets,
)


def bk(title, markets, upd="2026-08-10T04:00:00Z"):
    return {"key": title.lower(), "title": title, "last_update": upd,
            "markets": markets}


def mkt(key, outcomes):
    return {"key": key, "outcomes": outcomes}


def oc(name, price, point=None):
    o = {"name": name, "price": price}
    if point is not None:
        o["point"] = point
    return o


class TestSandwichGuard(unittest.TestCase):
    """Trap 1, stated directly as sides rather than through the grouping."""

    def test_same_total_line_is_a_hedge(self):
        self.assertTrue(opposes(("Over", 40.5), ("Under", 40.5)))
        self.assertFalse(is_sandwich(("Over", 40.5), ("Under", 40.5)))
        self.assertFalse(is_middle(("Over", 40.5), ("Under", 40.5)))

    def test_over_above_under_is_a_sandwich(self):
        # A total of exactly 41 loses both legs.
        self.assertTrue(is_sandwich(("Over", 41.5), ("Under", 40.5)))
        self.assertFalse(opposes(("Over", 41.5), ("Under", 40.5)))

    def test_under_above_over_is_a_middle_not_a_hedge(self):
        # A total of exactly 41 wins both. Pleasant, still not a hedge, and
        # staking it as one misprices every leg.
        self.assertTrue(is_middle(("Over", 40.5), ("Under", 41.5)))
        self.assertFalse(opposes(("Over", 40.5), ("Under", 41.5)))
        self.assertFalse(is_sandwich(("Over", 40.5), ("Under", 41.5)))

    def test_mirrored_spread_is_a_hedge(self):
        self.assertTrue(opposes(("Penrith", -1.5), ("Roosters", 1.5)))

    def test_same_direction_spread_is_a_sandwich(self):
        # Both teams giving 1.5 start: they cannot both win by two.
        self.assertTrue(is_sandwich(("Penrith", -1.5), ("Roosters", -1.5)))

    def test_asymmetric_spread_is_not_a_hedge(self):
        self.assertTrue(is_middle(("Penrith", -1.5), ("Roosters", 2.5)))
        self.assertTrue(is_sandwich(("Penrith", -2.5), ("Roosters", 1.5)))

    def test_h2h_sides_oppose_by_name(self):
        self.assertTrue(opposes(("Penrith", None), ("Roosters", None)))

    def test_lined_against_unlined_is_never_safe(self):
        self.assertTrue(is_sandwich(("Over", 40.5), ("Penrith", None)))
        self.assertIsNone(gap(("Over", 40.5), ("Penrith", None)))

    def test_same_side_twice_is_never_safe(self):
        self.assertTrue(is_sandwich(("Over", 40.5), ("Over", 40.5)))


class TestGrouping(unittest.TestCase):
    """The structural half: two different lines must never meet."""

    def test_totals_split_by_line(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
            bk("B", [mkt("totals", [oc("Over", 1.95, 41.5), oc("Under", 1.85, 41.5)])]),
        ]}
        got = submarkets(ev, ["totals"])
        self.assertEqual(len(got), 2)
        for sm in got:
            self.assertEqual(len(sm.book_odds), 1)   # never merged

    def test_matched_totals_line_merges(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
            bk("B", [mkt("totals", [oc("Over", 2.10, 40.5), oc("Under", 1.75, 40.5)])]),
        ]}
        got = submarkets(ev, ["totals"])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0].book_odds), ["A", "B"])
        self.assertEqual(sorted(got[0].book_odds["A"]), ["Over 40.5", "Under 40.5"])

    def test_spreads_at_same_abs_but_opposite_direction_do_not_merge(self):
        """The trap abs(point) grouping walks into.

        Both books sit at 1.5. Backing Penrith -1.5 at A and Roosters -1.5 at B
        needs both teams to win by two, which cannot happen — so these must
        land in different markets and never be paired."""
        ev = {"bookmakers": [
            bk("A", [mkt("spreads", [oc("Penrith", 1.90, -1.5), oc("Roosters", 1.90, 1.5)])]),
            bk("B", [mkt("spreads", [oc("Penrith", 1.90, 1.5), oc("Roosters", 1.90, -1.5)])]),
        ]}
        got = submarkets(ev, ["spreads"])
        self.assertEqual(len(got), 2)
        for sm in got:
            self.assertEqual(len(sm.book_odds), 1)

    def test_mirrored_spreads_merge_and_carry_the_sign(self):
        ev = {"bookmakers": [
            bk("A", [mkt("spreads", [oc("Penrith", 1.90, -1.5), oc("Roosters", 1.90, 1.5)])]),
            bk("B", [mkt("spreads", [oc("Penrith", 2.05, -1.5), oc("Roosters", 1.80, 1.5)])]),
        ]}
        got = submarkets(ev, ["spreads"])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0].book_odds["A"]),
                         ["Penrith -1.5", "Roosters +1.5"])
        self.assertEqual(got[0].label, "Line 1.5")

    def test_book_quoting_its_own_sandwich_is_dropped(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 41.5), oc("Under", 1.90, 40.5)])]),
        ]}
        self.assertEqual(submarkets(ev, ["totals"]), [])

    def test_three_way_quote_poisons_the_market_for_the_event(self):
        """Trap 1 in outcome space, which is how ice hockey and boxing fail.

        One book pricing regulation time (with a Draw) and another pricing
        including overtime are different bets under one market key. Backing one
        team at the two-way book and the other at the three-way book loses both
        when the game is drawn in regulation and the first team wins in
        overtime."""
        ev = {"bookmakers": [
            bk("A", [mkt("h2h", [oc("Oilers", 2.10), oc("Canucks", 1.80)])]),
            bk("B", [mkt("h2h", [oc("Oilers", 2.60), oc("Canucks", 2.40),
                                 oc("Draw", 4.20)])]),
        ]}
        self.assertEqual(submarkets(ev, ["h2h"]), [])

    def test_one_sided_quote_joins_its_own_line_only(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
            bk("B", [mkt("totals", [oc("Over", 2.30, 40.5)])]),
            bk("C", [mkt("totals", [oc("Under", 2.30, 44.5)])]),
        ]}
        got = {sm.point: sm for sm in submarkets(ev, ["totals"])}
        self.assertEqual(sorted(got[40.5].book_odds), ["A", "B"])
        self.assertEqual(got[40.5].book_odds["B"], {"Over 40.5": 2.30})
        self.assertNotIn(44.5, got)      # no market to join, so dropped

    def test_whole_number_line_is_flagged_for_push(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 41), oc("Under", 1.90, 41)])]),
            bk("B", [mkt("totals", [oc("Over", 1.90, 41.5), oc("Under", 1.90, 41.5)])]),
        ]}
        flags = {sm.point: sm.can_push for sm in submarkets(ev, ["totals"])}
        self.assertTrue(flags[41.0])
        self.assertFalse(flags[41.5])

    def test_h2h_is_one_market_with_plain_names(self):
        ev = {"bookmakers": [
            bk("A", [mkt("h2h", [oc("Penrith", 2.10), oc("Roosters", 1.80)])]),
            bk("B", [mkt("h2h", [oc("Penrith", 1.95), oc("Roosters", 1.95)])]),
        ]}
        got = submarkets(ev, ["h2h"])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].label, "")
        self.assertFalse(got[0].can_push)
        self.assertEqual(sorted(got[0].book_odds["A"]), ["Penrith", "Roosters"])

    def test_unrequested_markets_are_ignored(self):
        ev = {"bookmakers": [
            bk("A", [mkt("h2h", [oc("Penrith", 2.10), oc("Roosters", 1.80)]),
                     mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
        ]}
        self.assertEqual([s.market_key for s in submarkets(ev, ["h2h"])], ["h2h"])

    def test_malformed_prices_are_ignored_without_poisoning(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", None, 40.5), oc("Under", 1.90, 40.5)])]),
            bk("B", [mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
        ]}
        got = submarkets(ev, ["totals"])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0].book_odds), ["B"])

    def test_lined_market_without_a_line_is_dropped(self):
        """A totals quote with no `point` must not be treated as head-to-head.

        gap() reads two absent points as complementary-by-name, which is right
        for h2h and catastrophic for totals: every line a book quotes would
        merge into one market, and the merge could not be spotted afterwards
        because the line is exactly what is missing. The feed has always sent
        `point`; if it stops, the market gets dropped rather than guessed."""
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90), oc("Under", 1.90)])]),
            bk("B", [mkt("totals", [oc("Over", 2.40), oc("Under", 1.60)])]),
        ]}
        self.assertEqual(submarkets(ev, ["totals"]), [])

    def test_h2h_still_works_without_points(self):
        """The same absence is normal and correct on head-to-head."""
        ev = {"bookmakers": [
            bk("A", [mkt("h2h", [oc("Penrith", 2.10), oc("Roosters", 1.80)])]),
            bk("B", [mkt("h2h", [oc("Penrith", 1.95), oc("Roosters", 1.95)])]),
        ]}
        self.assertEqual(len(submarkets(ev, ["h2h"])), 1)

    def test_float_noise_on_a_line_still_matches(self):
        ev = {"bookmakers": [
            bk("A", [mkt("totals", [oc("Over", 1.90, 40.5), oc("Under", 1.90, 40.5)])]),
            bk("B", [mkt("totals", [oc("Over", 2.10, 40.50000000001),
                                    oc("Under", 1.75, 40.50000000001)])]),
        ]}
        self.assertEqual(len(submarkets(ev, ["totals"])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
