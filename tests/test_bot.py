"""Interactive restaking. Every reply here is a dollar figure someone types
into a betting slip, so the arithmetic is held to the same bar as core.py."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, bot
from arbtool.core import allocate, evaluate
from arbtool.pairs import analyse_game

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
        implementation of the split is exactly the divergence ARCHITECTURE.md §7
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

    def test_unrebuildable_market_says_so(self):
        broken = dict(ITEM, legs=[
            {"book": "SportsBet", "outcome": "Penrith Panthers", "odds": 2.12}])
        self.assertIn("Could not rebuild", bot.restake(broken, 2000.0, config))


BOARD_ITEM = {
    "sig": "e1", "home": "Penrith Panthers", "away": "Sydney Roosters",
    "sport": "NRL", "at": 0,
    "display": ["Penrith Panthers", "Sydney Roosters"],
    "board": {
        "SportsBet": {"Penrith Panthers": 1.74, "Sydney Roosters": 2.10},
        "TAB":       {"Penrith Panthers": 1.75, "Sydney Roosters": 2.05},
        "Ladbrokes": {"Penrith Panthers": 1.82, "Sydney Roosters": 2.00},
        "Unibet":    {"Penrith Panthers": 1.71, "Sydney Roosters": 2.10},
    },
    "legs": [{"book": "Ladbrokes", "outcome": "Penrith Panthers", "odds": 1.82},
             {"book": "SportsBet", "outcome": "Sydney Roosters", "odds": 2.10}],
}


class TestParseQuote(unittest.TestCase):
    def test_two_prices_are_home_then_away(self):
        """Two bare numbers are read in the order the match is read, not the
        alphabetical order the engine sorts outcomes into. Getting this
        backwards would price the wrong side."""
        book, prices = bot.parse_quote("bet365 1.72 2.15", BOARD_ITEM)
        self.assertEqual(book, "bet365")
        self.assertEqual(prices, {"Penrith Panthers": 1.72,
                                  "Sydney Roosters": 2.15})

    def test_named_side_resolves_from_a_partial_name(self):
        book, prices = bot.parse_quote("bet365 Roosters 2.15", BOARD_ITEM)
        self.assertEqual(prices, {"Sydney Roosters": 2.15})

    def test_filler_words_never_match_a_team(self):
        """"the" is inside "Pan-the-rs". A substring match tied prices to the
        wrong side, which is the worst failure this parser can have."""
        book, prices = bot.parse_quote("bet365 has 2.15 on the roosters",
                                       BOARD_ITEM)
        self.assertEqual(book, "bet365")
        self.assertEqual(prices, {"Sydney Roosters": 2.15})

    def test_phrasing_and_order_do_not_matter(self):
        want = {"Sydney Roosters": 2.15}
        for s in ("bet365 roosters 2.15", "bet365 2.15 roosters",
                  "roosters are 2.15 on bet365", "BET365 @2.15 Roosters",
                  "bet365 Sydney Roosters 2.15", "bet 365 roosters 2.15"):
            book, prices = bot.parse_quote(s, BOARD_ITEM)
            self.assertEqual(prices, want, s)
            self.assertEqual(book.lower().replace(" ", ""), "bet365", s)

    def test_two_named_sides_pair_positionally(self):
        book, prices = bot.parse_quote("bet365 panthers 1.72 roosters 2.15",
                                       BOARD_ITEM)
        self.assertEqual(prices, {"Penrith Panthers": 1.72,
                                  "Sydney Roosters": 2.15})

    def test_a_price_with_no_bookmaker_asks_for_one(self):
        r = bot.parse_quote("2.15 roosters", BOARD_ITEM)
        self.assertIsInstance(r, str)
        self.assertIn("bookmaker", r)

    def test_one_price_with_no_team_refuses_to_guess(self):
        """A lone price could belong to either side, and picking the flattering
        one would manufacture an arbitrage that does not exist."""
        r = bot.parse_quote("bet365 2.30", BOARD_ITEM)
        self.assertIsInstance(r, str)
        self.assertIn("Which side", r)

    def test_unrecognised_team_names_both_sides(self):
        r = bot.parse_quote("bet365 xyz 2.15", BOARD_ITEM)
        self.assertIsInstance(r, str)
        self.assertIn("Penrith Panthers", r)

    def test_implausible_prices_rejected(self):
        for bad in ("bet365 210", "bet365 Roosters 0.5", "bet365 1.0 2.0"):
            self.assertIsInstance(bot.parse_quote(bad, BOARD_ITEM), str, bad)

    def test_a_bare_number_is_not_a_quote(self):
        """Otherwise the quote parser would swallow every restake."""
        for s in ("4000", "$4,000", "/last", "thanks"):
            self.assertIsNone(bot.parse_quote(s, BOARD_ITEM), s)


class TestQuote(unittest.TestCase):
    def test_compares_against_the_whole_board_not_just_the_alerted_legs(self):
        """The improvement can come from pairing the quoted price with a book
        that placed third. Comparing only against the winning pair would miss
        it, and would also overstate the gain."""
        out = bot.quote(dict(BOARD_ITEM), "bet365",
                        {"Sydney Roosters": 2.15}, config)
        self.assertIn("best we have is 2.10", out)     # SportsBet/Unibet, not the leg
        self.assertIn("Ladbrokes + bet365", out)

    def test_better_but_not_crossing_says_so_and_prints_no_slip(self):
        out = bot.quote(dict(BOARD_ITEM), "bet365",
                        {"Sydney Roosters": 2.15}, config)
        self.assertIn("not an arbitrage", out)
        self.assertNotIn("PLACE FIRST", out)

    def test_worse_price_is_called_not_better(self):
        out = bot.quote(dict(BOARD_ITEM), "bet365",
                        {"Sydney Roosters": 1.90}, config)
        self.assertIn("not better", out)
        self.assertNotIn("PLACE FIRST", out)

    def test_crossing_quote_matches_the_engine_exactly(self):
        """The slip must agree with core.py to the cent. This is the number
        someone types into two betting apps."""
        out = bot.quote(dict(BOARD_ITEM), "bet365",
                        {"Sydney Roosters": 2.40}, config)
        merged = dict(BOARD_ITEM["board"], **{"bet365": {"Sydney Roosters": 2.40}})
        arb = analyse_game(merged, "h2h", sorted(merged),
                           commission_pct=config.COMMISSION_PCT).best_pair.arb
        allocate(arb, config.TOTAL_STAKE, increment=config.STAKE_INCREMENT)
        self.assertIn(f"${arb.worst_profit:,.2f} guaranteed", out)
        for o in arb.outcomes:
            self.assertIn(f"stake ${arb.legs[o].stake:,.0f}", out)

    def test_quoted_price_is_labelled_as_the_users_own_reading(self):
        """It did not come from the feed, and the leg it pairs with is still as
        old as the alert. Both facts have to survive into the message."""
        out = bot.quote(dict(BOARD_ITEM), "bet365",
                        {"Sydney Roosters": 2.40}, config)
        self.assertIn("your reading, not our feed", out)

    def test_book_name_is_html_escaped(self):
        out = bot.quote(dict(BOARD_ITEM), "<b>x", {"Sydney Roosters": 2.15}, config)
        self.assertNotIn("<b>x", out)

    def test_falls_back_to_the_alerted_pair_and_says_so(self):
        """Alerts written before the board was stored only carry two books."""
        old = {k: v for k, v in BOARD_ITEM.items() if k != "board"}
        out = bot.quote(old, "bet365", {"Sydney Roosters": 2.15}, config)
        self.assertIn("not the full board", out)


SECOND_ITEM = {
    "sig": "e2", "home": "Brisbane Broncos", "away": "Melbourne Storm",
    "sport": "NRL", "at": 0,
    "display": ["Brisbane Broncos", "Melbourne Storm"],
    "board": {"SportsBet": {"Brisbane Broncos": 1.90, "Melbourne Storm": 1.95},
              "Ladbrokes": {"Brisbane Broncos": 2.00, "Melbourne Storm": 1.88}},
    "legs": [{"book": "Ladbrokes", "outcome": "Brisbane Broncos", "odds": 2.00},
             {"book": "SportsBet", "outcome": "Melbourne Storm", "odds": 1.95}],
}


class TestPickGame(unittest.TestCase):
    """A price is useless against the wrong fixture. Naming a team has to
    select the game, or a quote meant for one match would be priced against
    another and the reply would be confidently wrong."""

    def setUp(self):
        # BOARD_ITEM, not ITEM: ITEM is Panthers v *Storm*, so "storm" would
        # legitimately match both fixtures and the ambiguity branch would fire.
        self.items = [dict(BOARD_ITEM), dict(SECOND_ITEM)]

    def test_naming_a_team_selects_its_game(self):
        idx, ask = bot._pick_item("bet365 storm 2.30", self.items)
        self.assertEqual(idx, 1)
        self.assertIsNone(ask)

    def test_naming_nothing_uses_the_newest(self):
        idx, ask = bot._pick_item("bet365 1.72 2.15", self.items)
        self.assertEqual(idx, 0)
        self.assertIsNone(ask)

    def test_a_name_in_two_games_asks_instead_of_guessing(self):
        # ITEM is Panthers v Storm, and the second is Storm v Titans, so "storm"
        # genuinely names two fixtures.
        both = [dict(ITEM), dict(SECOND_ITEM,
                                 home="Melbourne Storm", away="Gold Coast Titans",
                                 display=["Melbourne Storm", "Gold Coast Titans"],
                                 legs=[{"book": "TAB", "outcome": "Melbourne Storm",
                                        "odds": 2.0},
                                       {"book": "Neds", "outcome": "Gold Coast Titans",
                                        "odds": 2.0}])]
        idx, ask = bot._pick_item("bet365 storm 2.30", both)
        self.assertIsNone(idx)
        self.assertIn("more than one alert", ask)


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

    def test_quoting_an_older_game_then_staking_it(self):
        """Quote a game that is not the newest, then send a total. The stake
        must apply to the game just discussed, not to whichever alert happened
        to arrive last."""
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM), dict(SECOND_ITEM)]))
        out = bot.handle("bet365 storm 2.30", config)
        self.assertIn("Melbourne Storm", out)
        self.assertNotIn("Sydney Roosters", out)
        after = bot.handle("/stake 3000", config)
        self.assertIn("Melbourne Storm", after)
        self.assertIn("2.30", after)

    def test_games_lists_every_stored_fixture(self):
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM), dict(SECOND_ITEM)]))
        out = bot.handle("/games", config)
        self.assertIn("Sydney Roosters", out)
        self.assertIn("Melbourne Storm", out)

    def test_slash_commands(self):
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        self.assertIn("guaranteed", bot.handle("/stake 4000", config))
        self.assertIn("Sydney Roosters", bot.handle("/quote bet365 2.15 roosters",
                                                    config))
        self.assertIn("Arb Desk", bot.handle("/help", config))

    def test_command_suffixed_with_the_bot_name(self):
        """Telegram appends @botname when a command is tapped in a group."""
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        self.assertIn("Sydney Roosters",
                      bot.handle("/quote@arbbot bet365 2.15 roosters", config))

    def test_a_bare_command_explains_itself(self):
        """Tapping the menu entry sends the command with no arguments. Silence
        there would look broken."""
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        for c in ("/quote", "/stake"):
            self.assertIn("Arb Desk", bot.handle(c, config), c)

    def test_an_unreadable_command_argument_still_answers(self):
        """Stray chatter is ignored on purpose, but an explicit command was
        deliberate and is owed a reply."""
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        self.assertIn("could not read", bot.handle("/stake abc", config))
        self.assertIsNone(bot.handle("abc", config))

    def test_menu_entries_are_valid_for_telegram(self):
        """setMyCommands rejects uppercase or overlong entries, and a rejected
        menu fails silently — the button would simply never appear."""
        for c in bot.COMMANDS:
            self.assertRegex(c["command"], r"^[a-z0-9_]{1,32}$")
            self.assertLessEqual(len(c["description"]), 256)
            self.assertNotIn("<", c["description"])

    def test_quote_then_stake_restakes_the_improved_pairing(self):
        """Sending a price and then a total must restake what was just shown.
        Silently restaking the superseded pairing would quote a profit the user
        was never offered."""
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        bot.handle("bet365 Roosters 2.40", config)
        out = bot.handle("4000", config)
        self.assertIn("bet365", out)
        self.assertIn("2.40", out)

    def test_a_quote_that_does_not_cross_leaves_the_legs_alone(self):
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        bot.handle("bet365 Roosters 2.15", config)
        self.assertEqual(json.loads(bot.LAST_FILE.read_text())[0]["legs"],
                         BOARD_ITEM["legs"])

    def test_stake_replies_still_work_with_a_board_stored(self):
        bot.LAST_FILE.write_text(json.dumps([dict(BOARD_ITEM)]))
        self.assertIn("guaranteed", bot.handle("4000", config))


if __name__ == "__main__":
    unittest.main(verbosity=2)
