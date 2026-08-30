#!/usr/bin/env python3
"""
analytics.py — what 115,000 logged odds boards say about the market.

    python3 analytics.py --db data/boards.db            # everything
    python3 analytics.py --correlation                  # which books price alike
    python3 analytics.py --margins                      # margin distribution
    python3 analytics.py --coverage                     # who quotes what
    python3 analytics.py --json

The scanner answers "is there an arbitrage right now". This answers the
questions you can only ask after logging the whole board for a fortnight,
which is why RECORD_BOARDS exists and why it stores every price rather than
only the ones that crossed.

The finding that justified the log
----------------------------------
Ranking bookmakers by how often they appear in an arbitrage is confidently
wrong in both directions, and this module is the demonstration. A book with
many appearances can be redundant because a neighbour prices within a tick of
it — Ladbrokes and Neds are both Entain-owned and agree on 99.9% of prices, so
holding both is one account's worth of information for two accounts' worth of
capital. A book with few appearances can be the lone outlier that made those
arbitrages exist at all. Deciding which accounts to keep needs the prices of
the books that did NOT participate, and those cannot be recovered afterwards.

Everything here reads the log only. It makes no API request and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations

from arbtool.record import connect


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load(conn):
    """Board rows with their odds parsed. One row is one market at one scan."""
    out = []
    for r in conn.execute(
            "SELECT sport_key, market_key, market_label, event_id, odds_json,"
            "       realised_margin_pct, best_margin_pct, playable, scanned_at"
            "  FROM boards b JOIN scans s ON s.id = b.scan_id"):
        try:
            odds = json.loads(r["odds_json"])
        except (TypeError, ValueError):
            continue
        if odds:
            out.append((r, odds))
    return out


# ---------------------------------------------------------------------------
# Book correlation
# ---------------------------------------------------------------------------

def correlation(boards, min_shared: int = 200):
    """For each pair of books, how often they quote an identical price.

    Identical price, not correlated price. Two books whose numbers move
    together but differ by a tick still create arbitrage; two that post the
    same number never can, however often both appear. That makes exact
    agreement the right measure for "is this second account earning its
    capital", and it is why this is a share of matching quotes rather than a
    Pearson coefficient.

    Pairs seen fewer than `min_shared` times are dropped: a 100% agreement rate
    over six shared markets is noise, and reporting it next to a rate computed
    over nine thousand invites exactly the wrong conclusion.
    """
    same = Counter()
    seen = Counter()
    for _, odds in boards:
        books = sorted(odds)
        for a, b in combinations(books, 2):
            oa, ob = odds[a], odds[b]
            if not isinstance(oa, dict) or not isinstance(ob, dict):
                continue
            shared = set(oa) & set(ob)
            if not shared:
                continue
            seen[(a, b)] += 1
            if all(oa[k] == ob[k] for k in shared):
                same[(a, b)] += 1

    rows = [{"pair": f"{a} / {b}", "shared_markets": seen[(a, b)],
             "identical_pct": round(same[(a, b)] / seen[(a, b)] * 100, 1)}
            for (a, b) in seen if seen[(a, b)] >= min_shared]
    return sorted(rows, key=lambda r: -r["identical_pct"])


# ---------------------------------------------------------------------------
# Margins
# ---------------------------------------------------------------------------

def margins(boards):
    """Distribution of the best margin available on each board.

    Negative is the normal state and means the market is not crossed: the two
    best prices together imply more than 100%, which is the bookmakers' edge.
    Positive is an arbitrage. The shape of this distribution is the real story
    of the strategy — the mass sits just below zero, which is why the tool
    finds so few opportunities and why a fortnight of scanning yields tens
    rather than thousands.
    """
    # best_margin_pct is finite on every board; realised_margin_pct is stored
    # only for the boards that crossed and were therefore staked (it is -inf by
    # design otherwise, see tests/test_record.py). Using the realised column for
    # the distribution would silently restrict it to the crossed boards and
    # report a ~99% crossing rate over a population of ~1.3% — the first draft
    # of this function did exactly that.
    vals = sorted(r["best_margin_pct"] for r, _ in boards
                  if r["best_margin_pct"] is not None
                  and r["best_margin_pct"] > -1e300)
    if not vals:
        return {}
    crossed = [v for v in vals if v > 0]
    realised = sorted(r["realised_margin_pct"] for r, _ in boards
                      if r["realised_margin_pct"] is not None
                      and r["realised_margin_pct"] > -1e300)

    def pct(p):
        return round(vals[min(len(vals) - 1, int(len(vals) * p))], 2)

    return {
        "boards": len(vals),
        "crossed": len(crossed),
        "crossed_pct": round(len(crossed) / len(vals) * 100, 3),
        "median": round(statistics.median(vals), 2),
        "mean": round(statistics.fmean(vals), 2),
        "p50": pct(0.50), "p90": pct(0.90), "p99": pct(0.99),
        "p999": pct(0.999),
        "max": round(vals[-1], 2),
        "crossed_median": round(statistics.median(crossed), 2) if crossed else None,
        # The margin that survives whole-dollar rounding on both legs. Always
        # at or below the theoretical one, and the figure the alerts gate on.
        "realised_boards": len(realised),
        "realised_median": round(statistics.median(realised), 2) if realised else None,
        "realised_max": round(realised[-1], 2) if realised else None,
        # Above 10% on a liquid AU market is not an opportunity, it is a stale
        # or mistyped price. Counting them separately keeps them out of any
        # average without hiding that they existed.
        "implausible_over_10pct": len([v for v in realised if v > 10]),
    }


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def coverage(boards):
    """How many markets each book quotes, and how often it supplies a best price.

    A book earns its account by being the best price on one side of a market
    that crosses, not by being present. The two columns diverge sharply and
    that gap is the argument for culling a book list.
    """
    present = Counter()
    in_crossed = Counter()
    for r, odds in boards:
        for b in odds:
            present[b] += 1
            if (r["realised_margin_pct"] or -1e9) > 0:
                in_crossed[b] += 1
    return sorted(
        ({"book": b, "markets_quoted": n,
          "present_when_crossed": in_crossed[b],
          "share_of_crossed_pct": round(in_crossed[b] / n * 100, 2) if n else 0.0}
         for b, n in present.items()),
        key=lambda d: -d["markets_quoted"])


def by_sport(boards):
    """Boards, crossings and ACTIONABLE opportunities per sport.

    The third column is the one that matters, and the gap between it and the
    second is the point. Ice hockey crossed on 23.4% of its boards — seventy
    times the rate of MLB — and produced not one playable opportunity: the
    average crossing was 0.20% and the largest 0.64%, so every one of them died
    at the profit and margin gates, and 433 of the 550 sat on thin two-book
    boards. A crossing rate is a measure of how often two books disagree, not
    of how often there is money in it.
    """
    agg = defaultdict(lambda: {"boards": 0, "crossed": 0, "playable": 0})
    for r, _ in boards:
        a = agg[r["sport_key"]]
        a["boards"] += 1
        if (r["best_margin_pct"] or -1e9) > 0:
            a["crossed"] += 1
        if r["playable"]:
            a["playable"] += 1
    out = [{"sport": s, **v,
            "crossed_pct": round(v["crossed"] / v["boards"] * 100, 3),
            "playable_pct": round(v["playable"] / v["boards"] * 100, 3)}
           for s, v in agg.items()]
    return sorted(out, key=lambda d: -d["boards"])


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=None)
    p.add_argument("--correlation", action="store_true")
    p.add_argument("--margins", action="store_true")
    p.add_argument("--coverage", action="store_true")
    p.add_argument("--sports", action="store_true")
    p.add_argument("--min-shared", type=int, default=200)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    with connect(args.db) as conn:
        boards = load(conn)
    if not boards:
        print("No boards logged.")
        return 1

    everything = not any((args.correlation, args.margins, args.coverage, args.sports))
    out = {}
    if everything or args.margins:
        out["margins"] = margins(boards)
    if everything or args.correlation:
        out["correlation"] = correlation(boards, args.min_shared)
    if everything or args.coverage:
        out["coverage"] = coverage(boards)
    if everything or args.sports:
        out["by_sport"] = by_sport(boards)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    if "margins" in out:
        m = out["margins"]
        print(f"\n  MARGIN DISTRIBUTION over {m['boards']:,} boards")
        print(f"    crossed (an arbitrage existed) {m['crossed']:,}"
              f"  = {m['crossed_pct']}% of boards")
        print(f"    median {m['median']}%   mean {m['mean']}%   max {m['max']}%")
        print(f"    p90 {m['p90']}%   p99 {m['p99']}%   p99.9 {m['p999']}%")
        print(f"    median margin when crossed: {m['crossed_median']}%")
        print(f"    after whole-dollar rounding: median {m['realised_median']}%"
              f"  max {m['realised_max']}%  over {m['realised_boards']:,} staked boards")
        print(f"    above 10% (stale prices, not opportunities): "
              f"{m['implausible_over_10pct']}")

    if "correlation" in out:
        print(f"\n  BOOK PAIRS BY IDENTICAL PRICING (>= {args.min_shared} shared markets)")
        print(f"    {'pair':<34} {'shared':>8} {'identical':>10}")
        for r in out["correlation"][:12]:
            print(f"    {r['pair']:<34} {r['shared_markets']:>8,} "
                  f"{r['identical_pct']:>9.1f}%")
        print("    A pair near 100% is one price feed behind two accounts.")

    if "coverage" in out:
        print("\n  BOOK COVERAGE")
        print(f"    {'book':<20} {'markets':>10} {'when crossed':>14} {'share':>8}")
        for r in out["coverage"]:
            print(f"    {r['book']:<20} {r['markets_quoted']:>10,} "
                  f"{r['present_when_crossed']:>14,} {r['share_of_crossed_pct']:>7.2f}%")

    if "by_sport" in out:
        print("\n  BY SPORT")
        print(f"    {'sport':<32} {'boards':>8} {'crossed':>8} {'rate':>8} "
              f"{'playable':>9}")
        for r in out["by_sport"][:16]:
            print(f"    {r['sport']:<32} {r['boards']:>8,} {r['crossed']:>8,} "
                  f"{r['crossed_pct']:>7.3f}% {r['playable']:>9,}")
        print("    Crossed = two books disagreed. Playable = it cleared the")
        print("    profit and margin gates. The gap is the whole strategy.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
