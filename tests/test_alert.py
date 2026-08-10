"""Telegram alerting: suppression, formatting, and the failure modes.

Suppression decides whether a real arbitrage reaches the phone, so the bias
throughout is to send. Silence is only correct when the same opportunity has
already gone out recently at no better a price.
"""
import json, sys, time, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, alert
from arbtool.scan import Opportunity, assess_event
from watch import demo_events


def demo_opps():
    # assess_event returns a list: one entry per market in the event, because
    # totals and spreads carry a separate market per line.
    return [o for ev in demo_events()
            for o in assess_event(ev, "rugbyleague_nrl", "NRL (demo)", config)]


def playable():
    return [o for o in demo_opps() if o.placeable and o.profit >= config.MIN_PROFIT]


class TestSignatureIdentity(unittest.TestCase):
    """One alert per market, not per fixture — and h2h keeps the old key.

    A game can now carry a head-to-head arb and a 50.5 totals arb at once.
    Keying suppression on the fixture would silence one of them and lose real
    money. Head-to-head deliberately keeps the bare event id so the suppression
    state already on the droplet survives the deploy that added the markets.
    """
    def _opp(self, market_key, label):
        o = playable()[0]
        o.market_key, o.market_label = market_key, label
        return o

    def test_h2h_signature_is_the_bare_event_id(self):
        self.assertEqual(alert._sig(self._opp("h2h", "")), self._opp("h2h", "").event_id)

    def test_two_markets_on_one_fixture_do_not_share_a_signature(self):
        sigs = {alert._sig(self._opp(k, lbl)) for k, lbl in
                [("h2h", ""), ("totals", "Total 50.5"), ("spreads", "Line 1.5")]}
        self.assertEqual(len(sigs), 3)

    def test_unlabelled_non_h2h_market_cannot_collide_with_h2h(self):
        # Defence in depth: lines.py drops a lined market with no line, so this
        # should be unreachable. If it ever is reached, it must not silence the
        # head-to-head alert for the same fixture.
        self.assertNotEqual(alert._sig(self._opp("totals", "")),
                            alert._sig(self._opp("h2h", "")))


class TestSuppression(unittest.TestCase):
    def setUp(self):
        self.opp = playable()[0]
        self.now = 1_000_000.0

    def test_unseen_is_sent(self):
        self.assertTrue(alert.is_fresh(self.opp, {}, self.now))

    def test_same_opportunity_suppressed_inside_window(self):
        seen = {alert._sig(self.opp): {"at": self.now - 60, "profit": self.opp.profit}}
        self.assertFalse(alert.is_fresh(self.opp, seen, self.now))

    def test_sent_again_after_cooldown(self):
        old = self.now - (alert.COOLDOWN_H * 3600 + 1)
        seen = {alert._sig(self.opp): {"at": old, "profit": self.opp.profit}}
        self.assertTrue(alert.is_fresh(self.opp, seen, self.now))

    def test_materially_better_price_breaks_through(self):
        """A standing arb that got much more profitable is worth a second
        interruption — it may now clear a threshold that made it not worth
        placing the first time."""
        seen = {alert._sig(self.opp): {"at": self.now - 60,
                                       "profit": self.opp.profit / (alert.IMPROVE * 2)}}
        self.assertTrue(alert.is_fresh(self.opp, seen, self.now))

    def test_marginally_better_price_does_not(self):
        seen = {alert._sig(self.opp): {"at": self.now - 60,
                                       "profit": self.opp.profit * 0.99}}
        self.assertFalse(alert.is_fresh(self.opp, seen, self.now))

    def test_corrupt_state_file_does_not_silence_alerts(self):
        """A damaged state file must fail towards sending, never towards
        silence — a missed arbitrage is worse than a duplicate message."""
        p = alert.SEEN_FILE
        backup = p.read_bytes() if p.exists() else None
        try:
            p.write_text("{not json")
            self.assertEqual(alert.load_seen(), {})
            self.assertTrue(alert.is_fresh(self.opp, alert.load_seen(), self.now))
        finally:
            p.unlink(missing_ok=True)
            if backup is not None:
                p.write_bytes(backup)

    def test_signature_ignores_price_drift(self):
        """Identity is the fixture. A cent of movement is the same opportunity,
        not a new one to re-announce."""
        a, b = playable()[0], playable()[0]
        b.arb.legs[b.arb.outcomes[0]].odds += 0.01
        self.assertEqual(alert._sig(a), alert._sig(b))

    def test_signature_ignores_which_pairing_won(self):
        """Ladbrokes and Neds are one Entain desk quoting identically, so the
        best pairing on a game can flip between them run to run. That must not
        read as a new opportunity, or the same match arrives every 20 minutes."""
        a, b = playable()[0], playable()[0]
        bp = b.analysis.best_pair
        bp.book_a, bp.book_b = bp.book_b, bp.book_a
        self.assertEqual(alert._sig(a), alert._sig(b))

    def test_same_fixture_suppressed_across_a_20_minute_gap(self):
        """The scheduled interval. A standing arb must not resend at 20 min."""
        opp = playable()[0]
        seen = {alert._sig(opp): {"at": self.now - 20 * 60, "profit": opp.profit}}
        self.assertFalse(alert.is_fresh(opp, seen, self.now))


class TestScanWindow(unittest.TestCase):
    """The window is a credit control, not a preference.

    alert.py exits before making any request outside it, so the window and the
    cron interval together are the entire monthly bill. It lives in config.py
    precisely so books.py can price a day's polling against the same numbers —
    a second copy of the hours is how that estimate quietly stops describing
    what is actually being spent.
    """
    def test_window_comes_from_config(self):
        self.assertEqual(alert.WINDOW_START, tuple(config.SCAN_WINDOW_START))
        self.assertEqual(alert.WINDOW_END, tuple(config.SCAN_WINDOW_END))

    def test_edges_are_start_inclusive_end_exclusive(self):
        from datetime import datetime
        def at(h, m):
            return datetime(2026, 8, 10, h, m, tzinfo=alert.TZ)
        sh, sm = config.SCAN_WINDOW_START
        eh, em = config.SCAN_WINDOW_END
        self.assertTrue(alert.in_window(at(sh, sm)), "start minute must scan")
        self.assertFalse(alert.in_window(at(eh, em)), "end minute must not")
        self.assertFalse(alert.in_window(at(sh, sm - 1)))

    def test_billable_runs_match_what_books_reports(self):
        """books.py prices the month off this count. If it drifts from the real
        window, the number that decides whether a market is affordable is a
        guess."""
        from books import runs_per_day
        every = config.CRON_MINUTES
        from datetime import datetime
        billable = sum(
            1 for m in range(0, 24 * 60, every)
            if alert.in_window(datetime(2026, 8, 10, m // 60, m % 60, tzinfo=alert.TZ)))
        self.assertEqual(runs_per_day(config), billable)

    # Active keys on 2026-08-10, from `books.py --sports`. The count moves
    # weekly with tennis and rises to roughly 15 in the September-October
    # finals period, which is a known and documented cliff — see config.py.
    MEASURED_KEYS = 11
    PLAN = 20_000

    def _credits_per_run(self, n_keys):
        """What one poll costs: markets x regions for every active key.

        Derived from SPORT_MARKETS rather than written down, so adding a market
        moves this test rather than leaving it asserting a stale number."""
        per_sport = dict(getattr(config, "SPORT_MARKETS", {}) or {})
        regions = len([r for r in config.REGIONS.split(",") if r])
        named = sum(len(m.split(",")) for m in per_sport.values())
        default = len(config.DEFAULT_MARKETS.split(","))
        rest = max(0, n_keys - len(per_sport)) * default
        return (named + rest) * regions

    def test_configured_schedule_fits_the_plan(self):
        """A tripwire, not a law. If someone tightens CRON_MINUTES, widens the
        window, or adds a market to SPORT_MARKETS, this fails before the month
        does. 31-day month, because that is the one that runs out."""
        from books import runs_per_day
        month = self._credits_per_run(self.MEASURED_KEYS) * runs_per_day(config) * 31
        self.assertLess(
            month, self.PLAN,
            f"the scheduled cron projects {month:,} credits against a "
            f"{self.PLAN:,} plan. Re-run `books.py --sports` and either widen "
            f"CRON_MINUTES or trim SPORT_MARKETS.")

    def test_peak_season_overspend_is_known_and_quantified(self):
        """Documents the cliff rather than pretending it is not there.

        At ~15 active keys this config exceeds the plan. That is accepted, not
        overlooked: the fix is to trim SPORT_MARKETS before September, and this
        test exists so the number is in the repo rather than in someone's head.
        If it starts failing, peak season has become affordable and the warnings
        in config.py and deploy/crontab.txt are stale."""
        from books import runs_per_day
        peak = self._credits_per_run(15) * runs_per_day(config) * 31
        self.assertGreater(
            peak, self.PLAN,
            "peak season now fits — update the warnings in config.py and "
            "deploy/crontab.txt, which say it does not.")


class TestLongPollTimeout(unittest.TestCase):
    """The socket deadline must outlive the long poll it waits on.

    getUpdates deliberately holds the connection open for its own `timeout`
    seconds. _call's default socket deadline was 20s against bot.POLL_TIMEOUT
    of 50, so every cycle expired, raised a bare TimeoutError that neither
    except clause caught, and killed the process. Under Restart=always that is
    a crash loop: 685 restarts in 24 hours, and the listener never once
    completed a poll.
    """
    def test_call_accepts_a_timeout(self):
        import inspect
        self.assertIn("timeout", inspect.signature(alert._call).parameters)

    def test_bot_asks_for_longer_than_its_long_poll(self):
        import bot, inspect, re
        src = inspect.getsource(bot.poll)
        m = re.search(r"timeout=POLL_TIMEOUT\s*\+\s*(\d+)", src)
        self.assertIsNotNone(
            m, "bot.poll must pass a socket timeout derived from POLL_TIMEOUT")
        self.assertGreaterEqual(
            int(m.group(1)), 5,
            "needs margin over the long poll for TLS setup and the response body")

    def test_read_timeout_becomes_a_retryable_error(self):
        """bot.poll already handles RuntimeError by sleeping and retrying. A
        bare TimeoutError escapes that and takes the process with it."""
        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
        try:
            with self.assertRaises(RuntimeError):
                alert._call("token", "getUpdates", {}, timeout=1)
        finally:
            urllib.request.urlopen = real


class TestCrashLoopIsVisible(unittest.TestCase):
    """A crash loop must not look healthy.

    The heartbeat is stamped before each poll so a hang is caught. But under
    Restart=always a process that dies mid-poll restarts, stamps again, and
    dies again — keeping the file perfectly fresh while serving nobody. That
    ran for 24 hours unreported.
    """
    def setUp(self):
        self.backup = (alert.ALIVE_FILE.read_bytes()
                       if alert.ALIVE_FILE.exists() else None)
        alert.BOT_WARNED_FILE.unlink(missing_ok=True)

    def tearDown(self):
        alert.ALIVE_FILE.unlink(missing_ok=True)
        alert.BOT_WARNED_FILE.unlink(missing_ok=True)
        if self.backup is not None:
            alert.ALIVE_FILE.write_bytes(self.backup)

    def test_fresh_heartbeat_with_stale_success_warns(self):
        now = time.time()
        alert.ALIVE_FILE.write_text(json.dumps({
            "at": now - 5,                                    # just restarted
            "ok_at": now - (alert.BOT_STALE_MIN + 10) * 60,   # never succeeds
        }))
        msg = alert.check_bot_alive(now)
        self.assertIsNotNone(msg, "a crash loop must be reported")
        self.assertIn("stuck", msg)

    def test_both_fresh_is_silent(self):
        now = time.time()
        alert.ALIVE_FILE.write_text(json.dumps({"at": now, "ok_at": now}))
        self.assertIsNone(alert.check_bot_alive(now))

    def test_old_heartbeat_without_ok_at_does_not_cry_wolf(self):
        """First scan after the upgrade reads a file written by the old bot."""
        now = time.time()
        alert.ALIVE_FILE.write_text(json.dumps({"at": now}))
        self.assertIsNone(alert.check_bot_alive(now))


class TestVerificationNote(unittest.TestCase):
    def test_names_each_price_to_check(self):
        """A general 'confirm before staking' is easy to skim past. The message
        must name the book, the side and the number, because the edge usually
        rests on one outlying price and that is the one most likely stale."""
        hits = playable()
        msg = alert.build_messages(hits, config)[0]
        self.assertIn("CHECK BEFORE STAKING", msg)
        arb = hits[0].arb
        for o in arb.outcomes:
            leg = arb.legs[o]
            self.assertIn(f"{leg.book} — {o} showing", msg)
            self.assertIn(f"{leg.odds:.2f}</b> or better", msg)
        self.assertIn("the arbitrage is gone", msg)


class TestBotHeartbeat(unittest.TestCase):
    """A dead reply listener is invisible from outside. You would discover it
    by sending a price from in front of a betting app and getting nothing."""

    def setUp(self):
        self.backup = (alert.ALIVE_FILE.read_bytes()
                       if alert.ALIVE_FILE.exists() else None)
        alert.BOT_WARNED_FILE.unlink(missing_ok=True)

    def tearDown(self):
        alert.ALIVE_FILE.unlink(missing_ok=True)
        alert.BOT_WARNED_FILE.unlink(missing_ok=True)
        if self.backup is not None:
            alert.ALIVE_FILE.write_bytes(self.backup)

    def test_never_run_is_not_a_fault(self):
        """Warning on a machine that has never run the bot would train the user
        to ignore the message that matters."""
        alert.ALIVE_FILE.unlink(missing_ok=True)
        self.assertIsNone(alert.check_bot_alive())

    def test_fresh_heartbeat_is_silent(self):
        alert.ALIVE_FILE.write_text(json.dumps({"at": time.time()}))
        self.assertIsNone(alert.check_bot_alive())

    def test_stale_heartbeat_warns_once_then_stays_quiet(self):
        old = time.time() - (alert.BOT_STALE_MIN + 5) * 60
        alert.ALIVE_FILE.write_text(json.dumps({"at": old}))
        first = alert.check_bot_alive()
        self.assertIsNotNone(first)
        self.assertIn("stopped", first)
        # A dead bot stays dead. Saying so every 20 minutes is noise.
        self.assertIsNone(alert.check_bot_alive())

    def test_warning_says_alerts_still_work(self):
        """The distinction matters: a stopped listener does not stop alerts,
        and implying otherwise would have the user watching for nothing."""
        old = time.time() - (alert.BOT_STALE_MIN + 5) * 60
        alert.ALIVE_FILE.write_text(json.dumps({"at": old}))
        self.assertIn("unaffected", alert.check_bot_alive())

    def test_recovery_rearms_the_warning(self):
        old = time.time() - (alert.BOT_STALE_MIN + 5) * 60
        alert.ALIVE_FILE.write_text(json.dumps({"at": old}))
        alert.check_bot_alive()
        alert.ALIVE_FILE.write_text(json.dumps({"at": time.time()}))
        self.assertIsNone(alert.check_bot_alive())     # back up, and re-armed
        alert.ALIVE_FILE.write_text(json.dumps({"at": old}))
        self.assertIsNotNone(alert.check_bot_alive())


class TestMessage(unittest.TestCase):
    def _api(self):
        return SimpleNamespace(credits=SimpleNamespace(
            spent_this_session=11, remaining_reported=415))

    def test_hit_leads_with_guaranteed_profit(self):
        hits = playable()
        msg = alert.build_messages(hits, config)[0]
        arb = hits[0].arb
        # worst_profit, never the headline margin: after rounding the legs no
        # longer pay identically and only this figure is actually guaranteed.
        self.assertIn(f"${arb.worst_profit:,.2f} guaranteed", msg)
        self.assertIn("PLACE FIRST", msg)

    def test_summary_line_shape_and_figures(self):
        """The summary line reads returns, then money in, then what is locked in.

        Asserts realised_margin rather than Arb.margin. Whole-dollar rounding
        means the legs stop paying identically, so the theoretical margin is no
        longer achievable — quoting it here would overstate every alert, and the
        two differ by enough to matter on a thin edge."""
        hits = playable()
        msg = alert.build_messages(hits, config)[0]
        arb = hits[0].arb
        self.assertIn(
            f"Returns ${arb.worst_return:,.2f}–${arb.best_return:,.2f}, "
            f"money put in ${arb.total_stake:,.0f}, ", msg)
        self.assertIn(f"guaranteed ${arb.worst_profit:,.2f} @ "
                      f"{arb.realised_margin * 100:.2f}%", msg)
        # The figure that is not guaranteed must not appear as if it were.
        if abs(arb.margin - arb.realised_margin) > 1e-9:
            self.assertNotIn(f"guaranteed ${arb.worst_profit:,.2f} @ "
                             f"{arb.margin * 100:.2f}%", msg)

    def test_one_message_per_game(self):
        """Two bet slips in one Telegram bubble means scrolling past one to
        read the other, on a phone, while both prices move."""
        hits = playable()
        self.assertEqual(len(alert.build_messages(hits * 3, config)), len(hits) * 3)


class TestLegContext(unittest.TestCase):
    """Age of each staked leg, and what the rest of the board says about it.

    Neither figure changes the maths. Both decide whether the crossing is real
    money or a quote a book forgot to update, and neither was on the alert
    before: the message named two prices with no indication that one of them
    was the only quote on the board, or four minutes old inside an event that
    passed the freshest-book staleness gate."""

    def _opp(self, odds, ages, staked_book="A", outcome="Home"):
        """An Opportunity with a known board, staked on a known book."""
        from arbtool.core import Leg
        analysis = SimpleNamespace(book_odds=odds, outcomes=sorted(odds["A"]))
        legs = {outcome: Leg(outcome=outcome, book=staked_book,
                             odds=odds[staked_book][outcome], stake=100.0)}
        return Opportunity(
            event_id="e1", sport_key="k", sport_title="S",
            home="Home", away="Away", commence_time="", minutes_to_start=60,
            ages=ages, analysis=analysis,
            arb=SimpleNamespace(legs=legs))

    def test_age_is_the_staked_book_not_the_freshest(self):
        """staleness_seconds() gates on max(updates) — the freshest book in the
        event — so a leg can be minutes old inside an event that passed. The
        context line must report the age of the price actually being staked."""
        opp = self._opp(
            odds={"A": {"Home": 3.60, "Away": 1.40}, "B": {"Home": 3.10, "Away": 1.45}},
            ages={"A": 240.0, "B": 3.0})
        ctx = opp.leg_context("Home")
        self.assertEqual(ctx.age, 240.0)
        self.assertIn("quoted 240s ago", ctx.describe())
        self.assertNotIn("3s ago", ctx.describe())

    def test_a_leg_no_other_book_quotes_says_so(self):
        """The extreme outlier: nothing to corroborate against, so the whole
        edge rests on one price being right. Must be stated, not inferred from
        an absent comparison."""
        opp = self._opp(
            odds={"A": {"Home": 3.60, "Away": 1.40}, "B": {"Away": 1.45}},
            ages={"A": 8.0, "B": 8.0})
        ctx = opp.leg_context("Home")
        self.assertTrue(ctx.alone)
        self.assertIsNone(ctx.next_best)
        self.assertIn("no other book quotes this", ctx.describe())

    def test_rivals_report_range_and_best(self):
        """The MLB case: PointsBet 3.60 against a field of 2.85-3.11, where all
        four crossing pairings ran through that one book. The range is what
        shows a lone outlier for what it is."""
        opp = self._opp(
            odds={"A": {"Home": 3.60, "Away": 1.40},
                  "B": {"Home": 3.11, "Away": 1.42},
                  "C": {"Home": 2.85, "Away": 1.44},
                  "D": {"Home": 3.02, "Away": 1.43}},
            ages={"A": 12.0, "B": 5.0, "C": 5.0, "D": 5.0})
        ctx = opp.leg_context("Home")
        self.assertEqual(ctx.next_best, ("B", 3.11))
        self.assertEqual([b for b, _ in ctx.rivals], ["B", "D", "C"])
        self.assertIn("3 others quote 2.85–3.11, best B 3.11", ctx.describe())

    def test_a_price_of_one_is_not_a_rival(self):
        """1.00 returns the stake and is not a real market. Counting it as a
        rival would make a genuinely lone price look corroborated, which is the
        error this line exists to prevent."""
        opp = self._opp(
            odds={"A": {"Home": 3.60, "Away": 1.40}, "B": {"Home": 1.00, "Away": 1.45}},
            ages={"A": 8.0, "B": 8.0})
        self.assertTrue(opp.leg_context("Home").alone)

    def test_missing_age_is_stated_not_silently_dropped(self):
        opp = self._opp(
            odds={"A": {"Home": 3.60, "Away": 1.40}, "B": {"Home": 3.10, "Away": 1.45}},
            ages={"A": None, "B": 4.0})
        self.assertIn("age unknown", opp.leg_context("Home").describe())

    def test_every_staked_leg_carries_its_context_into_the_message(self):
        """The alert is the surface actually read, so the check block must carry
        the age and the field for both legs — not just be available on the
        object."""
        hits = playable()
        self.assertTrue(hits, "demo fixtures produced no playable arbitrage")
        for opp in hits:
            msg = alert.format_hit(opp, config)
            for outcome in opp.arb.outcomes:
                self.assertIn(alert.e(opp.leg_context(outcome).describe()), msg)

    def test_stakes_are_whole_dollars_with_no_cents(self):
        """allocate() rounds to STAKE_INCREMENT, so the cents are always .00 —
        printing them just adds digits to mistype into a betting slip."""
        hits = playable()
        msg = alert.build_messages(hits, config)[0]
        arb = hits[0].arb
        for o in arb.outcomes:
            self.assertEqual(arb.legs[o].stake % config.STAKE_INCREMENT, 0)
            self.assertIn(f"stake ${arb.legs[o].stake:,.0f}</b>", msg)
        self.assertNotIn(".00</b>", msg)

    def test_noise_is_not_in_the_message(self):
        msg = alert.build_messages(playable(), config)[0]
        for banned in ("starts in", "oldest quote", "credit(s) this run",
                       "left on plan", "not an instruction"):
            self.assertNotIn(banned, msg)

    def test_team_names_are_escaped(self):
        """Team names reach Telegram inside HTML. An unescaped angle bracket
        would break parse_mode and the message would be refused outright."""
        self.assertEqual(alert.e("A & B <script>"), "A &amp; B &lt;script&gt;")

    def test_message_fits_telegram_limit(self):
        for msg in alert.build_messages(playable() * 12, config):
            self.assertLessEqual(len(msg), alert.MAX_LEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
