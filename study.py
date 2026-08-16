#!/usr/bin/env python3
"""Which bookmakers earn the money parked in their accounts.

Reads the boards logged by arbtool/record.py and replays them through the real
engine under every candidate subset of books, so the question "which six do I
keep" is answered by what the prices actually did rather than by counting
appearances.

    python3 study.py                    # progress, per-book value, best 6
    python3 study.py --keep 5           # best 5 instead
    python3 study.py --require Betfair  # force a book into every subset
    python3 study.py --since 2026-08-12 # ignore boards logged before a date

WHY NOT JUST COUNT APPEARANCES

Because appearing in an arbitrage is not the same as being the reason for it.
Two books that price alike both appear in every crossing their prices allow, yet
either one alone would have produced the same arbitrage — the count double-marks
a pairing that is really one opportunity. Ladbrokes and Neds are the obvious
case here: both Entain, prices tracking each other (CLAUDE.md §11), so a naive
count credits each with arbitrages the other would have supplied.

The measure used instead is what a subset LOSES. Restrict the board to the
books in the subset, re-run the engine, and see which arbitrages survive and
what they pay. A book whose removal costs nothing was never load-bearing,
however often its name appeared.

COUNTING ONE ARBITRAGE ONCE

A standing arbitrage is re-logged every 30 minutes until it closes, so raw rows
over-weight the slow ones. An arbitrage can only be taken once, so boards are
collapsed to distinct (event, market) and scored on the best they ever offered.
Instance counts are reported alongside, because a market that crosses for four
hours is genuinely easier to catch than one that crosses for ten minutes.

WHAT THIS DOES NOT KNOW

Whether a book has limited or closed the account, how fast it settles, and
whether it voids on tennis retirement (CLAUDE.md §11). Those decide real
outcomes and no price feed reveals them. Treat the ranking as the money part of
the decision, not the whole of it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from arbtool.core import allocate, apply_stake_limits
from arbtool.pairs import analyse_game
from arbtool.record import connect, summary


def load_boards(conn, since=None):
    """Every logged board, with the settings its scan ran under.

    Joined rather than read from config because stake and thresholds change
    mid-collection — 2000/0.5% became 1500/1.0% on 2026-08-12 — and a profit
    computed at one stake cannot be compared with one from another."""
    sql = ("SELECT b.*, s.scanned_at, s.stake, s.min_profit, s.min_margin_pct,"
           "       s.stake_increment, s.commission_json"
           "  FROM boards b JOIN scans s ON s.id = b.scan_id")
    args = []
    if since:
        sql += " WHERE s.scanned_at >= ?"
        args.append(since)
    return conn.execute(sql, args).fetchall()


def evaluate_subset(row, books, cfg):
    """Best guaranteed profit on this board using only `books`.

    Runs the shipped engine — analyse_game already takes the book list and
    restricts to it — so a subset is scored by exactly the code that would have
    found the arbitrage live. Reimplementing the restriction here would be the
    dual-engine problem (CLAUDE.md §7) for no gain.

    Returns 0.0 when nothing crosses, which is the honest score: a subset that
    finds no arbitrage earns nothing, it does not merely rank lower."""
    odds = json.loads(row["odds_json"])
    present = [b for b in books if b in odds]
    if len(present) < 2:
        return 0.0

    commission = json.loads(row["commission_json"] or "{}") or cfg.COMMISSION_PCT
    ga = analyse_game(odds, row["market_key"], present, commission_pct=commission)
    if ga is None or ga.best_pair is None:
        return 0.0

    arb = ga.best_pair.arb
    allocate(arb, row["stake"], increment=row["stake_increment"])
    if getattr(cfg, "STAKE_LIMITS", None):
        apply_stake_limits(arb, cfg.STAKE_LIMITS, increment=row["stake_increment"])

    # The same two gates the live scan applies, read off the scan's own
    # settings. Scoring on raw crossings would credit subsets with arbitrages
    # the tool would never have alerted on.
    if arb.worst_profit < row["min_profit"]:
        return 0.0
    if arb.realised_margin * 100 < row["min_margin_pct"]:
        return 0.0
    return arb.worst_profit


def collapse(rows, books, cfg):
    """{(event, market): best profit ever offered} for this subset.

    Best rather than sum: the arbitrage is one bet, available repeatedly until
    it closes. Summing every scan that saw it would pay the user for the same
    opportunity thirty times and rank slow-moving markets far above their
    worth."""
    best = defaultdict(float)
    for r in rows:
        p = evaluate_subset(r, books, cfg)
        if p > 0:
            key = (r["event_id"], r["market_key"], r["market_label"])
            best[key] = max(best[key], p)
    return best


def score(rows, books, cfg):
    best = collapse(rows, books, cfg)
    return sum(best.values()), len(best)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keep", type=int, default=6, help="subset size (default 6)")
    p.add_argument("--require", action="append", default=[],
                   help="book that must be in every subset; repeatable")
    p.add_argument("--exclude", action="append", default=[],
                   help="book removed from every subset and from the baseline; "
                        "repeatable. For accounts already ruled out — limited, "
                        "closed, or not wanted — so the ranking only compares "
                        "books that could actually be kept.")
    p.add_argument("--since", help="ignore scans before this ISO timestamp")
    p.add_argument("--db", help="path to boards.db")
    p.add_argument("--top", type=int, default=8, help="subsets to print")
    args = p.parse_args()

    with connect(args.db) as conn:
        rows = load_boards(conn, args.since)
        stats = summary(args.db)

    print(f"{stats['scans']} scans · {stats['boards']} boards · "
          f"{stats['crossed']} crossed · {stats['alertable']} alertable")
    print(f"window {stats['first']} .. {stats['last']}")
    if not rows:
        print("\nNothing logged yet. RECORD_BOARDS is set in config.py; the "
              "cron scan writes on its next in-window run.")
        return 1
    print(f"{len(rows)} boards in scope\n")

    books = list(config.BOOKS)
    excluded = set(args.exclude)
    unknown = excluded - set(books)
    if unknown:
        print(f"--exclude names not in config.BOOKS: {', '.join(sorted(unknown))}")
        return 1
    if excluded & set(args.require):
        both = excluded & set(args.require)
        print(f"Both required and excluded: {', '.join(sorted(both))}")
        return 1
    if excluded:
        books = [b for b in books if b not in excluded]
        print(f"Excluded up front: {', '.join(sorted(excluded))}\n")
    full_profit, full_count = score(rows, books, config)
    if full_count == 0:
        print("No board in the window clears the live thresholds. Nothing to "
              "rank yet — leave collection running.")
        return 1

    print(f"All {len(books)} books: {full_count} distinct arbitrages, "
          f"${full_profit:,.0f} total\n")

    # Leave-one-out. The only per-book number that means anything: what the
    # user gives up by closing that one account, everything else unchanged.
    print("Cost of dropping each book on its own")
    print("-" * 52)
    loo = []
    for b in books:
        prof, cnt = score(rows, [x for x in books if x != b], config)
        loo.append((full_profit - prof, full_count - cnt, b))
    for lost, lost_n, b in sorted(loo, reverse=True):
        share = 100 * lost / full_profit if full_profit else 0
        print(f"  {b:<16} -${lost:>8,.0f}  ({share:4.1f}%)  {lost_n:>3} arbs")

    required = set(args.require)
    missing = required - set(books)
    if missing:
        print(f"\nNot configured books: {', '.join(sorted(missing))}")
        return 1

    pool = [b for b in books if b not in required]
    need = args.keep - len(required)
    if need < 0:
        print(f"\n--require names more books than --keep {args.keep}")
        return 1

    print(f"\nBest {args.keep} of {len(books)}"
          f"{' (forcing ' + ', '.join(sorted(required)) + ')' if required else ''}")
    print("-" * 52)
    ranked = []
    for combo in combinations(pool, need):
        subset = sorted(required | set(combo))
        prof, cnt = score(rows, subset, config)
        ranked.append((prof, cnt, subset))
    ranked.sort(reverse=True)

    for prof, cnt, subset in ranked[:args.top]:
        keep = 100 * prof / full_profit if full_profit else 0
        print(f"  ${prof:>8,.0f}  {keep:5.1f}% of full  {cnt:>3} arbs  "
              f"{', '.join(subset)}")

    if ranked:
        prof, cnt, subset = ranked[0]
        print(f"\nKeeping {', '.join(subset)} holds "
              f"{100 * prof / full_profit:.1f}% of the profit and "
              f"{100 * cnt / full_count:.1f}% of the arbitrages, on "
              f"{args.keep} funded accounts instead of {len(books)} — "
              f"${config.TOTAL_STAKE * args.keep:,.0f} of float rather than "
              f"${config.TOTAL_STAKE * len(books):,.0f}.")
        print("Confirm against account status before closing anything: a book "
              "that has already limited you is worth nothing whatever this says.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
