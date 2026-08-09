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
from arbtool.scan import assess_event
from watch import demo_events


def demo_opps():
    return [o for o in (assess_event(ev, "rugbyleague_nrl", "NRL (demo)", config)
                        for ev in demo_events()) if o]


def playable():
    return [o for o in demo_opps() if o.placeable and o.profit >= config.MIN_PROFIT]


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
        self.assertIn("unhedged", msg)          # the one-leg risk must stay stated

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
