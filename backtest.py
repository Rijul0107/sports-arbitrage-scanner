#!/usr/bin/env python3
"""
backtest.py — what a starting bankroll would have become, on the logged boards.

    python3 backtest.py --db data/boards.db --bankroll 2000
    python3 backtest.py --bankroll 2000 --max-margin 10 --json

THIS IS A SIMULATION, NOT A TRADING RECORD. No bet in it was ever placed. It
replays odds boards that were captured live between 2026-08-12 and 2026-08-29
and asks a counterfactual: if a bankroll had been available, and every
opportunity the scanner actually surfaced had been taken at the size the
bankroll allowed, what would the balance be at the end? Every number it prints
is the output of that model, and the model's assumptions are listed by
`--assumptions` and printed at the foot of every run. Read them before quoting
any figure.

Why a bankroll simulation rather than a sum of profits
------------------------------------------------------
Summing `worst_profit` over detected opportunities answers a question nobody
has: it assumes unlimited capital, instant settlement, and the ability to be in
thirty positions at once. The number it produces is large and meaningless.

What actually binds is capital. An arbitrage needs money at two books at the
same moment, and that money is locked from the instant the bets are placed
until the fixture settles. A $2,000 bankroll cannot take a $1,500 position on
Saturday afternoon and another one an hour later; it takes the first, and the
second passes it by. Modelling that turns "opportunities existed" into "this is
what could have been captured", which is the only version worth reporting.

Why the winner of each fixture is not needed
--------------------------------------------
That is the defining property of an arbitrage and the reason this backtest is
honest without result data: `Arb.worst_profit` is the profit in the WORST case,
the floor across every outcome. If the position is genuinely hedged, the
bankroll gains that amount whoever wins. So the simulation never has to guess a
result, and cannot flatter itself by assuming favourable ones.

The cost of that is per-book balances. Which book holds the money afterwards
DOES depend on the winner, and without results this model cannot track it —
see ASSUMPTIONS['rebalancing'], which is the most optimistic thing here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from arbtool.core import allocate, apply_stake_limits
from arbtool.pairs import analyse_game
from arbtool.record import connect
from study import load_boards


# ---------------------------------------------------------------------------
# The assumptions, stated once and printed with every result
# ---------------------------------------------------------------------------

ASSUMPTIONS = {
    "no bets were placed": (
        "Every figure is simulated from captured odds. There is no trading "
        "record behind any of it."),
    "rebalancing": (
        "Capital is treated as one pool, freely movable between bookmakers at "
        "no cost and no delay. In reality an arbitrage leaves the stake at "
        "whichever book won, and moving it back is a bank transfer taking "
        "days. THIS IS THE MOST OPTIMISTIC ASSUMPTION HERE and it flatters "
        "the result the most when opportunities cluster in time."),
    "settlement": (
        "Capital is locked from the moment a position is opened until "
        "SETTLE_HOURS after the fixture starts, then returned with profit."),
    "prices were obtainable": (
        "The board is assumed placeable at the quoted price for both legs. "
        "Real books reject, restrict, and move a price between the scan and "
        "the click; none of that is modelled."),
    "no account limits": (
        "Bookmakers limit and close winning arbitrage accounts, usually "
        "within weeks. An 18-day window is short enough to dodge this, which "
        "is itself a reason not to extrapolate the result to a year."),
    "stale prices excluded": (
        "Boards above --max-margin are dropped as data errors. A genuine "
        "double-digit arbitrage on a liquid AU market essentially does not "
        "occur; such a board is a stale or mistyped price that would have "
        "been voided on placement."),
}

#: Hours after kick-off at which a position is assumed settled and the capital
#: returned. Three hours covers a full NRL, AFL, NFL or MLB fixture; it is
#: deliberately not tuned per sport, because the sensitivity of the final
#: figure to this number is itself worth reporting (see --settle-hours).
SETTLE_HOURS = 3.0


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def opportunities(rows, cfg, max_margin_pct: float,
                  strategy: str = "gated", book_source: str = "logged"):
    """Every distinct opportunity the scanner surfaced, best price first seen.

    One opportunity is one (event, market, line) — not one board row. A market
    that crosses stays crossed for a while and is re-detected on every scan
    until it closes, so counting rows would pay the same bet thirty times.
    study.collapse() makes the same choice for the same reason.

    Unlike study.collapse(), the timestamp is kept: the whole point here is
    that capital is finite and opportunities are ordered in time. The scan
    taken is the FIRST that crossed the gates, not the best-priced one — you
    cannot know at 14:00 that a better price arrives at 15:30, and a backtest
    that picks the best one is reading the future.
    """
    best = {}
    for r in rows:
        odds = json.loads(r["odds_json"])
        # "logged" replays each board against every book actually present in
        # it. That matters because the book list changed mid-collection: the
        # first 117 scans carry 12 books and the remaining 433 carry 7, so
        # filtering everything through today's config.BOOKS would silently
        # discard four days of wider boards and understate what the tool found
        # at the time. "config" answers the different question of what the
        # current seven-book setup would have caught.
        books = (sorted(odds) if book_source == "logged"
                 else [b for b in cfg.BOOKS if b in odds])
        if len(books) < 2:
            continue
        commission = json.loads(r["commission_json"] or "{}") or cfg.COMMISSION_PCT
        ga = analyse_game(odds, r["market_key"], books, commission_pct=commission)
        if ga is None or ga.best_pair is None:
            continue

        arb = ga.best_pair.arb
        allocate(arb, r["stake"], increment=r["stake_increment"])
        if getattr(cfg, "STAKE_LIMITS", None):
            apply_stake_limits(arb, cfg.STAKE_LIMITS, increment=r["stake_increment"])

        margin = arb.realised_margin * 100
        if margin <= 0:
            continue                        # never crossed; not an arbitrage
        if strategy == "gated":
            # The two gates the live scanner applied, read off that scan's own
            # settings rather than today's file, because both moved during
            # collection. Scoring on raw crossings would credit the tool with
            # arbitrages it deliberately stayed silent about.
            if arb.worst_profit < r["min_profit"] or margin < r["min_margin_pct"]:
                continue
        if margin > max_margin_pct:
            continue                        # stale/erroneous price, see ASSUMPTIONS

        key = (r["event_id"], r["market_key"], r["market_label"])
        if key in best:
            continue                        # already taken at first sight
        best[key] = {
            "key": key,
            "seen_at": r["scanned_at"],
            "commence_time": r["commence_time"],
            "sport": r["sport_key"],
            "margin_pct": margin,
            # Margin is what scales; the recorded profit is at that scan's
            # stake, which is not the stake this simulation will use.
            "margin_frac": arb.realised_margin,
            "books": sorted(arb.books_used),
        }
    return sorted(best.values(), key=lambda o: o["seen_at"])


def capital_required(opps, stake: float, settle_hours: float = SETTLE_HOURS):
    """Peak capital locked at once, if every opportunity were taken at `stake`.

    The number that turns a profit total into a return. Ninety-nine positions
    worth $912 sounds good until you find they overlap twenty-six deep and need
    $26,000 standing behind them; the sum of profits is not a performance
    figure without it. Swept as an event list rather than by sampling, so a
    brief overlap cannot be missed between samples.
    """
    events = []
    for o in opps:
        opened = _parse(o["seen_at"])
        kickoff = _parse(o["commence_time"]) or opened
        if opened is None:
            continue
        closed = max(kickoff, opened) + timedelta(hours=settle_hours)
        events.append((opened, +stake))
        events.append((closed, -stake))
    events.sort()
    current = peak = 0.0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def simulate(opps, bankroll: float, max_position: float,
             settle_hours: float = SETTLE_HOURS):
    """Walk the opportunities in time order against a finite bankroll.

    Returns (final_bankroll, taken, skipped, timeline). An opportunity is
    skipped when the free capital at that moment is too small to make the
    minimum sensible position — that is the constraint the whole exercise
    exists to model, and skipped ones are reported, never silently dropped.
    """
    free = bankroll
    open_positions = []           # (settles_at, capital_returned_including_profit)
    taken, skipped, timeline = [], [], []
    peak = bankroll

    for o in opps:
        now = _parse(o["seen_at"])
        if now is None:
            continue

        # Return capital from anything that has settled by now.
        still_open = []
        for settles_at, amount in open_positions:
            if settles_at <= now:
                free += amount
            else:
                still_open.append((settles_at, amount))
        open_positions = still_open

        stake = min(free, max_position)
        if stake < 50:            # below this the whole-dollar rounding on two
            skipped.append({**o, "reason": f"only ${free:,.2f} free"})
            continue              # legs eats the edge; not a real position

        profit = stake * o["margin_frac"]
        kickoff = _parse(o["commence_time"]) or now
        settles_at = max(kickoff, now) + timedelta(hours=settle_hours)

        free -= stake
        open_positions.append((settles_at, stake + profit))
        taken.append({**o, "stake": stake, "profit": profit})
        timeline.append({"at": o["seen_at"], "equity": free + sum(a for _, a in open_positions),
                         "profit": profit})
        peak = max(peak, free + sum(a for _, a in open_positions))

    final = free + sum(amount for _, amount in open_positions)
    return final, taken, skipped, timeline, peak


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(bankroll, final, taken, skipped, opps, rows_span, settle_hours):
    gross = sum(t["profit"] for t in taken)
    roi = (final - bankroll) / bankroll * 100 if bankroll else 0.0
    days = max(1e-9, (rows_span[1] - rows_span[0]).total_seconds() / 86400)

    print()
    print("=" * 72)
    print("  BACKTEST — SIMULATED, NO BETS WERE PLACED")
    print("=" * 72)
    print(f"  Window            {rows_span[0]:%Y-%m-%d} to {rows_span[1]:%Y-%m-%d}"
          f"  ({days:.1f} days)")
    print(f"  Starting bankroll ${bankroll:,.2f}")
    print(f"  Final bankroll    ${final:,.2f}")
    print(f"  Profit            ${final - bankroll:,.2f}")
    print(f"  Return            {roi:.2f}% over {days:.1f} days")
    if days >= 1:
        # Stated, but see the warning below it. Annualising an 18-day window
        # through an account-limiting regime is not a forecast.
        ann = ((final / bankroll) ** (365.0 / days) - 1) * 100 if bankroll else 0
        print(f"  Annualised        {ann:,.0f}%   <- NOT a forecast, see assumptions")
    print()
    print(f"  Opportunities detected  {len(opps)}")
    print(f"  Taken                   {len(taken)}")
    print(f"  Skipped, no capital     {len(skipped)}")
    if taken:
        margins = sorted(t["margin_pct"] for t in taken)
        print(f"  Margin  median {margins[len(margins)//2]:.2f}%"
              f"   min {margins[0]:.2f}%   max {margins[-1]:.2f}%")
        print(f"  Mean position           ${sum(t['stake'] for t in taken)/len(taken):,.2f}")
        print(f"  Mean profit per bet     ${gross/len(taken):,.2f}")
    print()
    by_sport = defaultdict(lambda: [0, 0.0])
    for t in taken:
        by_sport[t["sport"]][0] += 1
        by_sport[t["sport"]][1] += t["profit"]
    if by_sport:
        print("  By sport")
        for s, (n, p) in sorted(by_sport.items(), key=lambda kv: -kv[1][1]):
            print(f"    {s:<34} {n:>3} bets   ${p:>8,.2f}")
    print()
    print(f"  ASSUMPTIONS (settlement at kickoff + {settle_hours:g}h)")
    for name, text in ASSUMPTIONS.items():
        print(f"    - {name}: {text}")
    print("=" * 72)


def sweep(opps, bankroll: float, settle_hours: float):
    """Return over position size, as a fraction of the bankroll.

    This is the only tunable in the model that changes the answer, so it is
    reported rather than quietly fixed at one value. The shape is a real
    trade-off and not a bug: a large position captures more of each margin but
    locks the whole bankroll until settlement, so the next opportunity to
    appear inside that window is missed entirely. A small one takes every
    opportunity and earns less on each.

    Read the result with the sample size in mind. Sixteen opportunities is far
    too few to identify an optimum — whichever fraction wins here won it partly
    by happening to be free when the two largest margins appeared. Quote the
    range, not the maximum.
    """
    out = []
    for frac in (1.0, 0.5, 0.34, 0.25, 0.2, 0.15, 0.1, 0.05):
        final, taken, skipped, _, peak = simulate(
            opps, bankroll, bankroll * frac, settle_hours)
        out.append({
            "fraction": frac,
            "max_position": round(bankroll * frac, 2),
            "final": round(final, 2),
            "return_pct": round((final - bankroll) / bankroll * 100, 2) if bankroll else 0.0,
            "taken": len(taken), "skipped": len(skipped),
            "peak_equity": round(peak, 2),
        })
    return out


def print_sweep(rows, bankroll, n_opportunities=None):
    print()
    print(f"  POSITION SIZE SWEEP on ${bankroll:,.0f} — simulated")
    print(f"  {'fraction':>8} {'max pos':>10} {'final':>11} {'return':>8} "
          f"{'taken':>6} {'skipped':>8}")
    print("  " + "-" * 56)
    for r in rows:
        print(f"  {r['fraction']:>8.2f} {r['max_position']:>10,.0f} "
              f"{r['final']:>11,.2f} {r['return_pct']:>7.2f}% "
              f"{r['taken']:>6} {r['skipped']:>8}")
    lo = min(r["return_pct"] for r in rows)
    hi = max(r["return_pct"] for r in rows)
    print(f"  Range {lo:.2f}% to {hi:.2f}% depending purely on sizing policy.")
    n = n_opportunities if n_opportunities is not None else max(r["taken"] + r["skipped"]
                                                                for r in rows)
    print(f"  {n} opportunities is too few to call an optimum — quote the range,")
    print("  not the maximum: the best fraction won partly by being free when the")
    print("  two largest margins appeared.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=None, help="boards database (default: config path)")
    p.add_argument("--bankroll", type=float, default=2000.0)
    p.add_argument("--max-position", type=float, default=None,
                   help="cap on one position (default: the whole bankroll)")
    p.add_argument("--max-margin", type=float, default=10.0,
                   help="drop boards above this %% as stale prices (default 10)")
    p.add_argument("--settle-hours", type=float, default=SETTLE_HOURS)
    p.add_argument("--strategy", choices=("gated", "all"), default="gated",
                   help="gated: only what the live scanner would have alerted "
                        "on. all: every crossing under --max-margin")
    p.add_argument("--books", choices=("logged", "config"), default="logged",
                   help="logged: every book present in each board (default). "
                        "config: only today's config.BOOKS")
    p.add_argument("--compare", action="store_true",
                   help="run both strategies and show the capital-efficiency "
                        "contrast")
    p.add_argument("--sweep", action="store_true",
                   help="report return across position sizes instead of one run")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    # connect() is a context manager (WAL, schema-on-open); the rows are
    # materialised inside it so the connection can close before the replay,
    # which is CPU-bound and holds no lock.
    with connect(args.db) as conn:
        rows = load_boards(conn)
    if not rows:
        print("No boards logged. Nothing to replay.")
        return 1

    stamps = [s for s in (_parse(r["scanned_at"]) for r in rows) if s]
    span = (min(stamps), max(stamps))

    opps = opportunities(rows, config, args.max_margin,
                         strategy=args.strategy, book_source=args.books)
    final, taken, skipped, timeline, peak = simulate(
        opps, args.bankroll, args.max_position or args.bankroll, args.settle_hours)

    if args.compare:
        stake = args.max_position or 1000.0
        out = {}
        for strat in ("gated", "all"):
            o = opportunities(rows, config, args.max_margin,
                              strategy=strat, book_source=args.books)
            gross = sum(x["margin_frac"] * stake for x in o)
            peak = capital_required(o, stake, args.settle_hours)
            out[strat] = {"opportunities": len(o), "gross_profit": round(gross, 2),
                          "peak_capital": round(peak, 2),
                          "return_on_capital_pct": round(gross / peak * 100, 2) if peak else 0.0,
                          "median_margin_pct": round(
                              sorted(x["margin_pct"] for x in o)[len(o) // 2], 2) if o else 0.0}
        if args.json:
            print(json.dumps({"simulated": True, "bets_placed": False,
                              "stake_per_position": stake, "compare": out,
                              "assumptions": ASSUMPTIONS}, indent=2))
        else:
            print()
            print(f"  STRATEGY COMPARISON at ${stake:,.0f} per position — simulated")
            print(f"  {'':<10} {'opps':>6} {'gross':>11} {'peak capital':>14} "
                  f"{'return':>9} {'med margin':>11}")
            print("  " + "-" * 66)
            for strat, d in out.items():
                print(f"  {strat:<10} {d['opportunities']:>6} "
                      f"${d['gross_profit']:>10,.2f} ${d['peak_capital']:>13,.2f} "
                      f"{d['return_on_capital_pct']:>8.2f}% {d['median_margin_pct']:>10.2f}%")
            print()
            print("  Taking every crossing earns more dollars on far more capital.")
            print("  The gates are a capital-efficiency constraint, not a caution setting.")
        return 0

    if args.sweep:
        rows_out = sweep(opps, args.bankroll, args.settle_hours)
        if args.json:
            print(json.dumps({"simulated": True, "bets_placed": False,
                              "bankroll": args.bankroll, "detected": len(opps),
                              "sweep": rows_out, "assumptions": ASSUMPTIONS}, indent=2))
        else:
            print_sweep(rows_out, args.bankroll, len(opps))
            print()
            for name, text in ASSUMPTIONS.items():
                print(f"    - {name}: {text}")
        return 0

    if args.json:
        print(json.dumps({
            "simulated": True, "bets_placed": False,
            "window": [span[0].isoformat(), span[1].isoformat()],
            "starting_bankroll": args.bankroll, "final_bankroll": round(final, 2),
            "return_pct": round((final - args.bankroll) / args.bankroll * 100, 2),
            "detected": len(opps), "taken": len(taken), "skipped": len(skipped),
            "peak_equity": round(peak, 2),
            "assumptions": ASSUMPTIONS,
            "bets": [{k: (list(v) if isinstance(v, tuple) else v)
                      for k, v in t.items()} for t in taken],
        }, indent=2, default=str))
    else:
        report(args.bankroll, final, taken, skipped, opps, span, args.settle_hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
