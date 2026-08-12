"""End-to-end test of serve.py against a stubbed API — no network, no credits."""
import json, sys, threading, time, unittest, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, serve
from arbtool.scan import scan, select_sports, assess_event, is_playable
from watch import demo_events

def mk_events():
    soon = (datetime.now(timezone.utc) + timedelta(minutes=47)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    started = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    def bk(t,a,b,h,w):
        return {"key":t.lower(),"title":t,"last_update":fresh,
                "markets":[{"key":"h2h","outcomes":[{"name":h,"price":a},{"name":w,"price":b}]}]}
    h,a="Penrith Panthers","Melbourne Storm"
    return [
      {"id":"e1","commence_time":soon,"home_team":h,"away_team":a,
       "bookmakers":[bk("SportsBet",2.12,1.78,h,a),bk("Ladbrokes",1.95,1.92,h,a),
                     bk("TAB",1.85,1.98,h,a),bk("Neds",1.80,2.08,h,a)]},
      # already started: must be excluded (online in-play is illegal in AU)
      {"id":"e2","commence_time":started,"home_team":"X","away_team":"Y",
       "bookmakers":[bk("SportsBet",3.0,1.4,"X","Y"),bk("Neds",1.4,3.0,"X","Y")]},
    ]

class StubAPI:
    def __init__(self): self.credits = SimpleNamespace(
        remaining_reported=451, spent_this_session=3, summary=lambda: "stub")
    def sports(self, **kw): return [{"key":"rugbyleague_nrl","title":"NRL"}]
    def odds(self, *a, **kw): return mk_events()

class SportsStub:
    """Stands in for the free /sports listing. Records how it was called so a
    test can prove the allowlist is applied here and not by spending credits on
    odds requests for keys we do not want."""
    CATALOGUE = [
        {"key": "rugbyleague_nrl", "title": "NRL"},
        {"key": "rugbyunion_six_nations", "title": "Six Nations"},
        {"key": "basketball_nba", "title": "NBA"},
        {"key": "basketball_ncaab", "title": "NCAAB"},
        {"key": "basketball_wncaab", "title": "WNCAAB"},
        {"key": "tennis_atp_canadian_open", "title": "ATP Canadian Open"},
        {"key": "tennis_wta_canadian_open", "title": "WTA Canadian Open"},
    ]
    def __init__(self): self.calls = []
    def sports(self, **kw):
        self.calls.append(kw)
        return list(self.CATALOGUE)


class FakeResp:
    """Minimal stand-in for urlopen's context manager."""
    def __init__(self, body, headers): self._b = body; self.headers = headers
    def read(self): return json.dumps(self._b).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestCreditAccounting(unittest.TestCase):
    """The session budget is the guard against burning a month in an afternoon,
    so what it counts has to match what the plan was actually charged."""

    def _api(self, headers, body=None):
        import urllib.request
        from arbtool.api import OddsAPI
        api = OddsAPI("k", session_budget=100)
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: FakeResp(body or [], headers)
        try:
            api.odds("rugbyleague_nrl", regions="au", markets="h2h")
        finally:
            urllib.request.urlopen = real
        return api

    def test_charges_what_the_header_reports(self):
        api = self._api({"x-requests-last": "1", "x-requests-remaining": "499"})
        self.assertEqual(api.credits.spent_this_session, 1)
        self.assertEqual(api.credits.remaining_reported, 499)

    def test_empty_response_is_free(self):
        """Documented: 'Empty responses don't count against quota'. Estimating
        the cost instead billed the user for a sport with no games on."""
        api = self._api({"x-requests-last": "0", "x-requests-remaining": "500"})
        self.assertEqual(api.credits.spent_this_session, 0)

    def test_falls_back_to_estimate_when_header_missing(self):
        """Under-counting spend is the dangerous direction — assume it cost."""
        api = self._api({"x-requests-remaining": "499"})
        self.assertEqual(api.credits.spent_this_session, 1)

    def test_falls_back_when_header_is_junk(self):
        api = self._api({"x-requests-last": "", "x-requests-remaining": "499"})
        self.assertEqual(api.credits.spent_this_session, 1)


class TestSelectSports(unittest.TestCase):
    """Every key selected here costs one credit per poll, so what this filter
    lets through is the whole monthly bill."""

    def sel(self, keys, prefixes):
        api = SportsStub()
        cfg = SimpleNamespace(SPORT_KEYS=keys, SPORT_PREFIXES=prefixes)
        return [s["key"] for s in select_sports(api, cfg)], api

    def test_named_keys_selected(self):
        got, _ = self.sel(["rugbyleague_nrl", "basketball_nba"], ())
        self.assertEqual(sorted(got), ["basketball_nba", "rugbyleague_nrl"])

    def test_prefix_catches_rotating_tournaments(self):
        got, _ = self.sel([], ("tennis_",))
        self.assertEqual(sorted(got),
                         ["tennis_atp_canadian_open", "tennis_wta_canadian_open"])

    def test_named_and_prefix_are_unioned(self):
        got, _ = self.sel(["rugbyleague_nrl"], ("tennis_",))
        self.assertEqual(len(got), 3)
        self.assertIn("rugbyleague_nrl", got)

    def test_college_basketball_not_dragged_in(self):
        """The reason SPORT_KEYS exists. A "basketball_" prefix would pull in
        NCAAB and WNCAAB — hundreds of US college games AU books barely price,
        at a credit each per poll."""
        got, _ = self.sel(["basketball_nba"], ("tennis_",))
        self.assertNotIn("basketball_ncaab", got)
        self.assertNotIn("basketball_wncaab", got)

    def test_shipped_config_selects_no_three_way_sport(self):
        """Guards the real config, not a hypothetical one.

        Union can draw, so evaluate(expect_outcomes=2) discards it — polling it
        would spend a credit per poll on markets that are always thrown away.
        A prefix of "rugby" would catch it, which is exactly why the league key
        is named explicitly and the only prefix is tennis."""
        got, _ = self.sel(config.SPORT_KEYS, config.SPORT_PREFIXES)
        self.assertNotIn("rugbyunion_six_nations", got)
        self.assertNotIn("basketball_ncaab", got)
        self.assertIn("rugbyleague_nrl", got)

    def test_empty_config_selects_nothing(self):
        """Must spend zero rather than defaulting to everything in season."""
        got, _ = self.sel([], ())
        self.assertEqual(got, [])

    def test_filtering_happens_on_the_free_endpoint(self):
        """/sports costs 0 credits; /odds costs 1 per key. The allowlist has to
        be applied before any odds request, or it saves nothing."""
        _, api = self.sel(["rugbyleague_nrl"], ())
        self.assertEqual(len(api.calls), 1)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        api = StubAPI()
        serve.fetch_loop(api, config, interval=1, once=True)
        cls.srv = serve.ThreadingHTTPServer(("127.0.0.1", 8791), serve.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:8791{path}", timeout=5) as r:
            return r.status, r.read()

    def test_serves_dashboard(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Arb Desk", body)
        self.assertIn(b"evaluatePair", body)

    def test_api_returns_payload(self):
        code, body = self.get("/api/opportunities")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertEqual(d["books"], list(config.BOOKS))
        self.assertEqual(d["credits_remaining"], 451)
        self.assertGreaterEqual(len(d["games"]), 1)

    def test_in_play_event_excluded(self):
        _, body = self.get("/api/opportunities")
        ids = [g["id"] for g in json.loads(body)["games"]]
        self.assertIn("e1", ids)
        self.assertNotIn("e2", ids, "in-play game must never be served")

    def test_payload_carries_odds_not_margins(self):
        """The page recomputes; the server must not send precomputed pairs,
        or the two could disagree."""
        _, body = self.get("/api/opportunities")
        g = json.loads(body)["games"][0]
        self.assertIn("odds", g); self.assertIn("ages", g)
        self.assertNotIn("pairs", g)
        self.assertEqual(g["outcomes"][0], g["home"])   # home first

    def test_directory_traversal_blocked(self):
        try:
            code, _ = self.get("/../config.py")
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertNotEqual(code, 200)

    def test_unknown_path_404(self):
        try:
            code, _ = self.get("/nope.html")
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 404)

class TestPlayableGate(unittest.TestCase):
    """MIN_MARGIN_PCT is measured on the number the alert actually prints.

    Every surface quotes realised_margin — the post-rounding figure — because
    that is the only one guaranteed. Gating on the theoretical margin instead
    sent boards that cleared the floor in theory and then printed a percentage
    below it, which is indistinguishable from the threshold not working.
    """
    def opps(self):
        return [o for e in demo_events()
                for o in assess_event(e, "rugbyleague_nrl", "NRL (demo)", config)]

    def test_realised_never_exceeds_theoretical(self):
        # Rounding down and handing the remainder to the worst leg can lift the
        # floor but never above the un-rounded split, so this direction holds
        # for every board. If it ever flips, allocate() is over-staking.
        for o in self.opps():
            if o.arb is None:
                continue
            self.assertLessEqual(o.realised_margin_pct, o.best_margin_pct + 1e-9,
                                 f"{o.home} v {o.away}")

    def test_board_below_the_floor_after_rounding_is_rejected(self):
        staked = [o for o in self.opps() if o.arb is not None]
        # Pick the board with the widest gap between the two figures, then set
        # the floor between them: theoretical passes, realised does not.
        o = max(staked, key=lambda x: x.best_margin_pct - x.realised_margin_pct)
        gap = o.best_margin_pct - o.realised_margin_pct
        self.assertGreater(gap, 0, "demo data has no rounding loss to test against")
        floor = o.realised_margin_pct + gap / 2
        cfg = SimpleNamespace(MIN_PROFIT=0.0, MIN_MARGIN_PCT=floor)
        self.assertFalse(is_playable(o, cfg))
        cfg.MIN_MARGIN_PCT = o.realised_margin_pct - gap / 2
        self.assertTrue(is_playable(o, cfg))

    def test_unstaked_opportunity_never_passes(self):
        cfg = SimpleNamespace(MIN_PROFIT=0.0, MIN_MARGIN_PCT=-1e9)
        for o in self.opps():
            if o.arb is None:
                self.assertEqual(o.realised_margin_pct, float("-inf"))
                self.assertFalse(is_playable(o, cfg))

    def test_shipped_floor_is_one_percent(self):
        # The alert is meant to stay quiet below 1%. Guarded here so a config
        # edit that relaxes it is a failing test rather than a surprise message.
        self.assertGreaterEqual(config.MIN_MARGIN_PCT, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
