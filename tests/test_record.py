"""The board log and the subset replay that reads it.

The log runs unattended for a fortnight and then a real decision is made off
it — accounts closed, float withdrawn. Two failure modes matter more than the
rest: silently recording less than the whole board, which makes the subset
question unanswerable after the fact, and breaking the alert path, which costs
money while the data is being gathered.
"""
import json, sqlite3, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, study
from arbtool.record import connect, record_scan, summary
from arbtool.scan import assess_event, is_playable
from watch import demo_events


def demo_result():
    opps = [o for e in demo_events()
            for o in assess_event(e, "rugbyleague_nrl", "NRL (demo)", config)]
    return {"opportunities": opps,
            "playable": [o for o in opps if is_playable(o, config)],
            "events_seen": len(demo_events()),
            "sports": [{"key": "rugbyleague_nrl"}],
            "scanned_at": "2026-08-12T05:30:00+00:00"}


class RecordCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.dir.name) / "boards.db")
        self.addCleanup(self.dir.cleanup)

    def rows(self):
        with connect(self.db) as conn:
            return conn.execute("SELECT * FROM boards").fetchall()


class TestWhatGetsWritten(RecordCase):
    def test_non_crossing_boards_are_kept(self):
        # The whole point. A board that did not cross under twelve books may
        # cross under six, and one that crossed may stop. Storing only the
        # winners would make every later subset question unanswerable.
        result = demo_result()
        record_scan(result, config, db_path=self.db)
        rows = self.rows()
        self.assertEqual(len(rows), len(result["opportunities"]))
        self.assertTrue(any(r["placeable"] == 0 for r in rows),
                        "demo set should include a board that does not cross")

    def test_every_book_on_the_board_is_stored_not_just_the_two_used(self):
        record_scan(demo_result(), config, db_path=self.db)
        for r in self.rows():
            odds = json.loads(r["odds_json"])
            self.assertGreaterEqual(len(odds), 2)
            for book, prices in odds.items():
                self.assertTrue(prices, f"{book} stored with no prices")

    def test_settings_are_copied_onto_the_scan(self):
        # Stake and thresholds changed mid-collection once already. A profit
        # figure whose stake is unknown cannot be compared with another's.
        record_scan(demo_result(), config, db_path=self.db)
        with connect(self.db) as conn:
            s = conn.execute("SELECT * FROM scans").fetchone()
        self.assertEqual(s["stake"], config.TOTAL_STAKE)
        self.assertEqual(s["min_margin_pct"], config.MIN_MARGIN_PCT)
        self.assertEqual(json.loads(s["books_json"]), list(config.BOOKS))

    def test_infinite_margin_becomes_null(self):
        # realised_margin_pct is -inf on an unstaked board by design. Stored
        # raw, SQLite keeps it as a float that loses every later comparison
        # without saying so.
        record_scan(demo_result(), config, db_path=self.db)
        with connect(self.db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM boards WHERE placeable=0"
                             " AND realised_margin_pct IS NOT NULL").fetchone()[0]
        self.assertEqual(n, 0)

    def test_scans_accumulate_rather_than_replace(self):
        for _ in range(3):
            record_scan(demo_result(), config, db_path=self.db)
        self.assertEqual(summary(self.db)["scans"], 3)


class TestLoggingNeverBreaksTheAlert(RecordCase):
    def test_unwritable_path_returns_none_and_does_not_raise(self):
        # The money is in the alert. A full disk or a permissions mistake on
        # the droplet must cost data, never a message.
        bad = str(Path(self.dir.name) / "nope" / "\0" / "boards.db")
        self.assertIsNone(record_scan(demo_result(), config, db_path=bad))

    def test_malformed_result_returns_none_and_does_not_raise(self):
        self.assertIsNone(record_scan({"opportunities": [object()]}, config,
                                      db_path=self.db))

    def test_missing_keys_are_tolerated(self):
        self.assertIsNotNone(record_scan({"opportunities": [], "playable": []},
                                         config, db_path=self.db))


class TestSubsetReplay(RecordCase):
    def setUp(self):
        super().setUp()
        record_scan(demo_result(), config, db_path=self.db)
        with connect(self.db) as conn:
            self.rows_ = study.load_boards(conn)

    def books_in_winning_pair(self):
        for r in self.rows_:
            if r["placeable"]:
                odds = json.loads(r["odds_json"])
                ga = study.analyse_game(odds, r["market_key"], list(odds),
                                        commission_pct=config.COMMISSION_PCT)
                if ga and ga.best_pair:
                    return r, set(ga.best_pair.arb.books_used), set(odds)
        self.fail("demo set has no crossing board")

    def test_dropping_a_book_that_holds_a_winning_price_costs_something(self):
        _, used, _ = self.books_in_winning_pair()
        full, _ = study.score(self.rows_, list(config.BOOKS), config)
        cut = [b for b in config.BOOKS if b not in used]
        after, _ = study.score(self.rows_, cut, config)
        self.assertLess(after, full)

    def test_dropping_a_book_absent_from_every_board_costs_nothing(self):
        on_board = set()
        for r in self.rows_:
            on_board |= set(json.loads(r["odds_json"]))
        spare = [b for b in config.BOOKS if b not in on_board]
        self.assertTrue(spare, "demo set uses every configured book")
        full, n_full = study.score(self.rows_, list(config.BOOKS), config)
        after, n_after = study.score(self.rows_, [b for b in config.BOOKS
                                                  if b != spare[0]], config)
        self.assertEqual((after, n_after), (full, n_full))

    def test_a_standing_arb_counts_once_however_often_it_is_logged(self):
        # Re-log the same boards ten times, as a market crossing for five hours
        # would. The count and the total must not move: it is one bet.
        before = study.score(self.rows_, list(config.BOOKS), config)
        for _ in range(10):
            record_scan(demo_result(), config, db_path=self.db)
        with connect(self.db) as conn:
            rows = study.load_boards(conn)
        self.assertGreater(len(rows), len(self.rows_))
        self.assertEqual(study.score(rows, list(config.BOOKS), config), before)

    def test_a_single_book_can_never_arbitrage(self):
        # Two different bookmakers or it is not placeable (CLAUDE.md §5).
        for b in config.BOOKS:
            self.assertEqual(study.score(self.rows_, [b], config), (0.0, 0))

    def test_subset_scores_never_exceed_the_full_set(self):
        full, _ = study.score(self.rows_, list(config.BOOKS), config)
        for b in config.BOOKS:
            after, _ = study.score(self.rows_, [x for x in config.BOOKS if x != b],
                                   config)
            self.assertLessEqual(after, full + 1e-9, f"dropping {b} gained money")


if __name__ == "__main__":
    unittest.main(verbosity=2)
