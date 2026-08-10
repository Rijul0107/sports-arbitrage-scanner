"""The dual-engine constraint, checked (CLAUDE.md §7).

static/dashboard.html reimplements the pairwise engine in JavaScript so the
page can derive every figure from the prices it shows. The cost is that two
implementations have to agree, and "they agreed when I wrote them" decays.

This runs the *shipped* page's functions — extracted from the HTML, not copied
— over the same inputs as arbtool.pairs and compares margins to 1e-6 and
is_arb exactly. It needs node on PATH and nothing else; tests/e2e_server_browser.mjs
covers the integration path but hardcodes a playwright install that does not
exist on every machine, so this is the check that actually runs.

The random half matters more than the demo half: it covers books that quote
only one side of a market, which is rare on head-to-head and routine on totals
and spreads. That case is exactly where the two engines were found to disagree.
"""
import json, random, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from arbtool.pairs import pair_matrix
from arbtool.scan import assess_event, commission_map
from watch import demo_events

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "static" / "dashboard.html"
RUNNER = Path(__file__).resolve().parent / "parity_engine.mjs"

# Team names, an Over/Under pair and a mirrored handicap pair: the three
# outcome-name shapes the scanner can now produce.
SHAPES = [
    ("Penrith Panthers", "Melbourne Storm"),
    ("Over 50.5", "Under 50.5"),
    ("Sydney Roosters -1.5", "Brisbane Broncos +1.5"),
]


def build_payload(n_random=600, seed=20260810):
    """Every demo market, plus random boards including one-sided quotes."""
    boards, games = {}, []

    for ev in demo_events():
        for o in assess_event(ev, "rugbyleague_nrl", "NRL (demo)", config):
            games.append(o.to_dict())
            boards[o.uid] = o.analysis.book_odds

    rng = random.Random(seed)
    for i in range(n_random):
        outs = list(SHAPES[i % len(SHAPES)])
        pool = rng.sample(config.BOOKS, rng.randint(2, min(6, len(config.BOOKS))))
        odds = {}
        for b in pool:
            prices = {o: round(rng.uniform(1.30, 4.50), 2) for o in outs}
            if rng.random() < 0.12:
                prices.pop(rng.choice(outs))      # one side suspended
            if prices:
                odds[b] = prices
        if len(odds) < 2:
            continue
        gid = f"r{i}"
        games.append({"id": gid, "sport": "random", "home": outs[0], "away": outs[1],
                      "starts_in_min": 60, "market": "", "outcomes": outs,
                      "odds": odds, "ages": {b: 5 for b in odds}})
        boards[gid] = odds

    return {"books": list(config.BOOKS),
            "commission": commission_map(config),
            "games": games}, boards


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class TestDualEngine(unittest.TestCase):
    def test_browser_and_python_engines_agree(self):
        payload, boards = build_payload()
        commission = payload["commission"]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(payload, fh)
            path = fh.name

        proc = subprocess.run(
            ["node", str(RUNNER), str(DASHBOARD), path, json.dumps(commission)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js = {g["id"]: g["pairs"] for g in json.loads(proc.stdout)}

        compared = 0
        for gid, board in boards.items():
            books = [b for b in config.BOOKS if b in board]
            py = pair_matrix({b: board[b] for b in books}, books,
                             commission, expect_outcomes=2)
            py_by = {tuple(sorted((p.book_a, p.book_b))): p for p in py}
            js_by = {tuple(sorted(p["books"])): p for p in js[gid]}

            self.assertEqual(
                set(py_by), set(js_by),
                f"{gid}: the engines disagree about which pairings exist. A "
                f"pairing that cannot cover every outcome must be absent from "
                f"both, not present in one with a -inf margin.")

            for key in py_by:
                compared += 1
                p, j = py_by[key], js_by[key]
                self.assertAlmostEqual(p.margin_pct, j["margin"], delta=1e-6,
                                       msg=f"{gid} {key}: margins differ")
                self.assertEqual(bool(p.is_arb), bool(j["arb"]),
                                 f"{gid} {key}: is_arb differs")

        self.assertGreater(compared, 2000, "too few pairings to be worth trusting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
