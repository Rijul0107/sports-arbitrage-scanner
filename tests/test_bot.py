"""Interactive restaking. Every reply here is a dollar figure someone types
into a betting slip, so the arithmetic is held to the same bar as core.py."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, bot
from arbtool.core import allocate, evaluate

ITEM = {
    "sig": "e1", "home": "Penrith Panthers", "away": "Melbourne Storm",
    "sport": "NRL", "at": 0,
    "legs": [{"book": "SportsBet", "outcome": "Penrith Panthers", "odds": 2.12},
             {"book": "Neds", "outcome": "Melbourne Storm", "odds": 2.08}],
}


class TestParseAmount(unittest.TestCase):
    def test_plain_and_decorated_numbers(self):
        for s in ("4000", " 4000 ", "$4000", "4,000", "/stake 4000", "$4,000"):
            self.assertEqual(bot.parse_amount(s), 4000.0, s)

    def test_non_numbers_are_not_stakes(self):
        """Anything unparseable must be ignored, never guessed at — a wrong
        stake is money on the table."""
        for s in ("hello", "", "/help", "yes please", "4000 dollars on penrith"):
            self.assertIsNone(bot.parse_amount(s))

    def test_absurd_amounts_rejected(self):
        self.assertIsNone(bot.parse_amount("1"))          # under the floor
        self.assertIsNone(bot.parse_amount("999999999"))  # fat finger
        self.assertIsNone(bot.parse_amount("-500"))


class TestRestake(unittest.TestCase):
    def test_matches_the_engine_exactly(self):
        """The reply must agree with core.py, not approximate it. A second
        implementation of the split is exactly the divergence CLAUDE.md §7
        warns about."""
        out = bot.restake(ITEM, 4000.0, config)
        arb = evaluate({"SportsBet": {"Penrith Panthers": 2.12},
                        "Neds": {"Melbourne Storm": 2.08}}, expect_outcomes=2)
        allocate(arb, 4000.0, increment=config.STAKE_INCREMENT)
        self.assertIn(f"${arb.worst_profit:,.2f} guaranteed", out)
        for o in arb.outcomes:
            self.assertIn(f"stake ${arb.legs[o].stake:,.0f}", out)

    def test_outlay_never_exceeds_the_requested_total(self):
        for total in (500, 1000, 2000, 4000, 7500):
            out = bot.restake(ITEM, float(total), config)
            arb = evaluate({"SportsBet": {"Penrith Panthers": 2.12},
                            "Neds": {"Melbourne Storm": 2.08}}, expect_outcomes=2)
            allocate(arb, float(total), increment=config.STAKE_INCREMENT)
            self.assertLessEqual(arb.total_stake, total)
            self.assertIn(f"${arb.worst_profit:,.2f} guaranteed", out)

    def test_stake_too_small_refuses_and_says_the_minimum(self):
        """A thin edge can be real and still lose money after rounding. Both
        UIs refuse to print a slip in that case; so must this."""
        thin = dict(ITEM, legs=[
            {"book": "SportsBet", "outcome": "Penrith Panthers", "odds": 2.02},
            {"book": "Neds", "outcome": "Melbourne Storm", "odds": 1.99}])
        out = bot.restake(thin, 12.0, config)
        self.assertIn("does not work", out)
        self.assertNotIn("PLACE FIRST", out)

    def test_reply_repeats_the_price_check(self):
        """These are the alert's prices, possibly hours old. The reply must not
        read as a fresh quote."""
        out = bot.restake(ITEM, 4000.0, config)
        self.assertIn("CHECK BEFORE STAKING", out)
        self.assertIn("not fresh ones", out)
        self.assertIn("unhedged", out)

    def test_unrebuildable_market_says_so(self):
        broken = dict(ITEM, legs=[
            {"book": "SportsBet", "outcome": "Penrith Panthers", "odds": 2.12}])
        self.assertIn("Could not rebuild", bot.restake(broken, 2000.0, config))


class TestHandle(unittest.TestCase):
    def setUp(self):
        self.backup = bot.LAST_FILE.read_bytes() if bot.LAST_FILE.exists() else None
        bot.LAST_FILE.write_text(json.dumps([ITEM]))

    def tearDown(self):
        bot.LAST_FILE.unlink(missing_ok=True)
        if self.backup is not None:
            bot.LAST_FILE.write_bytes(self.backup)

    def test_number_restakes_latest(self):
        self.assertIn("guaranteed", bot.handle("4000", config))

    def test_help_and_last(self):
        self.assertIn("Arb Desk", bot.handle("/help", config))
        self.assertIn("guaranteed", bot.handle("/last", config))

    def test_chatter_is_ignored_silently(self):
        """Replying to every stray message would make the bot noisy in exactly
        the way the alerting design avoids."""
        self.assertIsNone(bot.handle("thanks", config))
        self.assertIsNone(bot.handle("", config))

    def test_no_alerts_yet_is_explained(self):
        bot.LAST_FILE.write_text("[]")
        self.assertIn("No alert", bot.handle("4000", config))


if __name__ == "__main__":
    unittest.main(verbosity=2)
