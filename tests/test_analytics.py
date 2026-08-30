"""Board analytics: the statistics must describe the population they claim to.

The bug this file exists to prevent shipped once. `margins()` originally read
`realised_margin_pct`, which the recorder stores only for boards that crossed
and were therefore staked — it is -inf everywhere else by design. The function
duly reported that 99% of boards contained an arbitrage, over a population that
was 1.3%. It was not wrong about any number it computed; it was wrong about
which rows it computed them over, which no assertion on a single value would
have caught.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics


class Row(dict):
    """A board row. dict access mirrors sqlite3.Row's mapping interface."""
    def __getitem__(self, k):
        return self.get(k)


def board(best, realised=None, sport="test_sport", playable=0, odds=None):
    return (Row(best_margin_pct=best, realised_margin_pct=realised,
                sport_key=sport, playable=playable, market_key="h2h",
                market_label="", event_id="ev1", scanned_at="2026-08-12T06:00:00+00:00"),
            odds or {"A": {"home": 2.0}, "B": {"home": 2.1}})


class TestMarginsPopulation(unittest.TestCase):
    def test_crossing_rate_is_over_all_boards_not_only_staked_ones(self):
        # 2 crossed out of 100. The 98 losers carry realised=None, exactly as
        # the recorder writes them.
        boards = [board(-3.0) for _ in range(98)] + [board(1.0, 0.9), board(2.0, 1.8)]
        m = analytics.margins(boards)
        self.assertEqual(m["boards"], 100)
        self.assertEqual(m["crossed"], 2)
        self.assertAlmostEqual(m["crossed_pct"], 2.0, places=3)

    def test_median_reflects_the_uncrossed_majority(self):
        boards = [board(-3.0) for _ in range(98)] + [board(1.0, 0.9), board(2.0, 1.8)]
        m = analytics.margins(boards)
        self.assertLess(m["median"], 0,
                        "median must be negative: most boards never cross")

    def test_realised_stats_are_reported_separately_and_scoped_to_staked(self):
        boards = [board(-3.0) for _ in range(98)] + [board(1.0, 0.9), board(2.0, 1.8)]
        m = analytics.margins(boards)
        self.assertEqual(m["realised_boards"], 2)
        self.assertAlmostEqual(m["realised_median"], 1.35, places=2)

    def test_implausible_count_uses_realised_not_theoretical(self):
        boards = [board(-3.0), board(30.0, 29.5), board(1.0, 0.9)]
        self.assertEqual(analytics.margins(boards)["implausible_over_10pct"], 1)

    def test_empty_input_returns_empty_rather_than_dividing_by_zero(self):
        self.assertEqual(analytics.margins([]), {})


class TestCorrelation(unittest.TestCase):
    def test_identical_books_score_100(self):
        same = {"A": {"home": 2.0, "away": 2.0}, "B": {"home": 2.0, "away": 2.0}}
        rows = analytics.correlation([board(-1.0, odds=same) for _ in range(300)],
                                     min_shared=10)
        self.assertEqual(rows[0]["identical_pct"], 100.0)

    def test_a_single_differing_tick_breaks_identity(self):
        odds = {"A": {"home": 2.0, "away": 2.0}, "B": {"home": 2.0, "away": 2.01}}
        rows = analytics.correlation([board(-1.0, odds=odds) for _ in range(300)],
                                     min_shared=10)
        self.assertEqual(rows[0]["identical_pct"], 0.0)

    def test_thin_pairs_are_dropped_not_reported_at_100(self):
        # Five shared markets at 100% agreement is noise and must not appear
        # beside a rate computed over thousands.
        same = {"A": {"home": 2.0}, "B": {"home": 2.0}}
        rows = analytics.correlation([board(-1.0, odds=same) for _ in range(5)],
                                     min_shared=200)
        self.assertEqual(rows, [])


class TestBySport(unittest.TestCase):
    def test_crossed_and_playable_are_counted_separately(self):
        # The ice hockey case: crosses constantly, never actionable.
        boards = ([board(0.2, 0.2, sport="icehockey_nhl", playable=0) for _ in range(50)]
                  + [board(-3.0, sport="icehockey_nhl") for _ in range(50)])
        row = next(r for r in analytics.by_sport(boards) if r["sport"] == "icehockey_nhl")
        self.assertEqual(row["crossed"], 50)
        self.assertEqual(row["playable"], 0)
        self.assertEqual(row["crossed_pct"], 50.0)
        self.assertEqual(row["playable_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
