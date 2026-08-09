#!/usr/bin/env python3
"""
watch.py — terminal arbitrage watcher.

Polls your books and, when a pairing crosses, prints the full pairwise matrix
and an unambiguous instruction: which two bookmakers, which side at each, and
exactly how many dollars.

    python3 watch.py --demo          # fabricated odds, no API key, no credits
    python3 watch.py --once          # one real poll
    python3 watch.py                 # keep watching
    python3 watch.py --stake 1500 --min-profit 10

Settings live in config.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List

import config
from arbtool.api import BudgetExceeded, OddsAPI, OddsAPIError, QuotaExhausted
from arbtool.core import allocate, recommend_first_leg
from arbtool.scan import Opportunity, assess_event, scan, select_sports


class C:
    R="\033[0m"; B="\033[1m"; DIM="\033[2m"
    GREEN="\033[32m"; RED="\033[31m"; YEL="\033[33m"; CYAN="\033[36m"; GREY="\033[90m"
    @classmethod
    def off(cls):
        for k in list(vars(cls)):
            if k.isupper(): setattr(cls, k, "")


BELL = "\a"
W = 74
def rule(ch="="): return ch * W
def money(x): return f"${x:,.2f}"


def print_matrix(opp: Opportunity, books: List[str]):
    pairs = {frozenset((p.book_a, p.book_b)): p for p in opp.analysis.pairs}
    bp = opp.analysis.best_pair          # None unless it genuinely crosses
    best_key = frozenset((bp.book_a, bp.book_b)) if bp else None

    width = max(11, max(len(b) for b in books) + 2)
    print(f"  {C.GREY}PAIRWISE MARGIN{C.R}")
    print(f"  {'':<{width}}" + "".join(f"{b:>{width}}" for b in books))
    for rb in books:
        row = f"  {rb:<{width}}"
        for cb in books:
            if rb == cb:
                row += f"{'—':>{width}}"
                continue
            p = pairs.get(frozenset((rb, cb)))
            if p is None:
                row += f"{'·':>{width}}"
                continue
            cell = f"{p.margin_pct:+.2f}%"
            if frozenset((rb, cb)) == best_key:
                cell = f"{C.GREEN}{C.B}[{cell}]{C.R}"
                row += f"{cell:>{width+len(C.GREEN)+len(C.B)+len(C.R)}}"
            elif p.is_arb:
                row += f"{C.GREEN}{cell:>{width}}{C.R}"
            else:
                row += f"{C.GREY}{cell:>{width}}{C.R}"
        print(row)


def print_prices(opp: Opportunity, books: List[str]):
    outcomes = opp.display_outcomes
    bo = opp.analysis.book_odds
    best = {o: max((bo[b][o], b) for b in bo if o in bo[b])[1] for o in outcomes}
    w = max(11, max(len(b) for b in books) + 2)
    head = f"  {C.GREY}{'BOOK':<{w}}" + "".join(f"{o[:14]:>16}" for o in outcomes)
    print(head + f"{'OVERROUND':>12}{'AGE':>7}{C.R}")
    for b in books:
        if b not in bo:
            continue
        row = f"  {b:<{w}}"
        for o in outcomes:
            v = bo[b].get(o)
            if v is None:
                row += f"{'—':>16}"
            elif best[o] == b:
                row += f"{C.GREEN}{C.B}{v:>14.2f} *{C.R}"
            else:
                row += f"{v:>14.2f}  "
        inv = sum(1/bo[b][o] for o in outcomes if o in bo[b])
        age = opp.ages.get(b)
        agestr = f"{age:.0f}s" if age is not None else "—"
        flag = C.YEL if (age or 0) > 90 else C.GREY
        row += f"{C.GREY}{(inv-1)*100:>11.2f}%{C.R}{flag}{agestr:>7}{C.R}"
        print(row)


def print_card(opp: Opportunity, cfg):
    arb = opp.arb
    bp = opp.analysis.best_pair
    print(f"\n{C.GREEN}{C.B}{rule()}{C.R}")
    print(f"{C.GREEN}{C.B}  {money(arb.worst_profit)} GUARANTEED   "
          f"{opp.best_margin_pct:.2f}% on {money(arb.total_stake)}{C.R}")
    print(f"{C.GREEN}{rule()}{C.R}")
    print(f"  {C.B}{opp.home}  v  {opp.away}{C.R}")
    mins = opp.minutes_to_start
    print(f"  {C.GREY}{opp.sport_title}  ·  starts in {mins:.0f} min  ·  "
          f"oldest quote {opp.oldest_quote:.0f}s  ·  "
          f"{opp.depth} of {len(opp.analysis.pairs)} pairings work{C.R}")
    print(f"{C.GREY}{rule('-')}{C.R}")
    print_matrix(opp, [b for b in cfg.BOOKS if b in opp.analysis.book_odds])
    print()
    print_prices(opp, [b for b in cfg.BOOKS if b in opp.analysis.book_odds])
    print(f"{C.GREY}{rule('-')}{C.R}")

    first = recommend_first_leg(arb)
    order = [first] + [o for o in arb.outcomes if o != first]
    print(f"  {C.B}BET SLIP — {bp.label}{C.R}")
    for i, o in enumerate(order, 1):
        leg = arb.legs[o]
        tag = (f"{C.YEL}PLACE FIRST{C.R} {C.GREY}(longest price — most likely to move){C.R}"
               if o == first else f"{C.GREY}then{C.R}")
        print(f"    {C.B}LEG {i}{C.R}  {tag}")
        print(f"      {C.CYAN}{C.B}{leg.book}{C.R}   back {C.B}{o}{C.R} @ {C.B}{leg.odds:.2f}{C.R}")
        print(f"      {C.GREEN}{C.B}STAKE {money(leg.stake)}{C.R}   "
              f"{C.GREY}returns {money(leg.returns)}{C.R}")
    print(f"{C.GREY}{rule('-')}{C.R}")
    print(f"  Outlay {money(arb.total_stake)}   "
          f"Returns {money(arb.worst_return)}–{money(arb.best_return)}   "
          f"{C.GREEN}{C.B}Profit {money(arb.worst_profit)}{C.R} "
          f"({arb.realised_margin*100:.2f}%)")
    print(f"  {C.YEL}If leg 2 misses you are unhedged on {money(arb.legs[first].stake)}"
          f" — that, not the outlay, is the risk.{C.R}")
    if arb.single_book:
        print(f"  {C.RED}Both best prices sit at one book — not placeable.{C.R}")
    print(f"{C.GREEN}{rule()}{C.R}")


def demo_events():
    soon = (datetime.now(timezone.utc) + timedelta(minutes=47)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    def bk(t, a, b, h, w):
        return {"key": t.lower(), "title": t, "last_update": fresh,
                "markets": [{"key": "h2h", "outcomes":
                             [{"name": h, "price": a}, {"name": w, "price": b}]}]}
    h, a = "Penrith Panthers", "Melbourne Storm"
    h2, a2 = "Sydney Roosters", "Brisbane Broncos"
    return [
        {"id": "d1", "commence_time": soon, "home_team": h, "away_team": a,
         "bookmakers": [bk("SportsBet",2.12,1.78,h,a), bk("Ladbrokes",1.95,1.92,h,a),
                        bk("TAB",1.85,1.98,h,a), bk("Neds",1.80,2.08,h,a)]},
        {"id": "d2", "commence_time": soon, "home_team": h2, "away_team": a2,
         "bookmakers": [bk("SportsBet",1.90,1.90,h2,a2), bk("Ladbrokes",1.87,1.93,h2,a2),
                        bk("TAB",1.92,1.88,h2,a2), bk("Neds",1.88,1.94,h2,a2)]},
    ]


def run_demo(cfg):
    print(f"{C.CYAN}{C.B}Demo mode{C.R} {C.GREY}— fabricated prices, no API key, "
          f"no credits spent.{C.R}\n")
    opps = [o for o in (assess_event(e, "rugbyleague_nrl", "NRL (demo)", cfg)
                        for e in demo_events()) if o]
    opps.sort(key=lambda o: -o.profit)
    for o in opps:
        if o.placeable and o.profit >= cfg.MIN_PROFIT:
            sys.stdout.write(BELL)
            print_card(o, cfg)
        else:
            reason = ("edge too thin to survive rounding" if o.depth
                      else f"no pairing crosses (best {o.best_margin_pct:+.2f}%)")
            print(f"\n  {C.GREY}{o.home} v {o.away} — {reason}{C.R}")
            print_matrix(o, [b for b in cfg.BOOKS if b in o.analysis.book_odds])
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stake", type=float, default=config.TOTAL_STAKE)
    p.add_argument("--min-profit", type=float, default=config.MIN_PROFIT)
    p.add_argument("--min-margin", type=float, default=config.MIN_MARGIN_PCT)
    p.add_argument("--sport", help="single sport key, e.g. rugbyleague_nrl")
    p.add_argument("--interval", type=int, default=config.POLL_SECONDS)
    p.add_argument("--budget", type=int, default=config.SESSION_CREDIT_BUDGET)
    p.add_argument("--once", action="store_true")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--no-colour", action="store_true")
    args = p.parse_args()

    if args.no_colour or not sys.stdout.isatty():
        C.off()

    # Effective config: file defaults, overridden by flags.
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config)
                             if k.isupper()})
    cfg.TOTAL_STAKE = args.stake
    cfg.MIN_PROFIT = args.min_profit
    cfg.MIN_MARGIN_PCT = args.min_margin

    if args.demo:
        return run_demo(cfg)

    if not cfg.API_KEY:
        print(f"{C.RED}No API key.{C.R} export ODDS_API_KEY='your_key' "
              f"(free at the-odds-api.com)\n"
              f"Or try {C.CYAN}python3 watch.py --demo{C.R} to see it run without one.")
        return 2

    api = OddsAPI(cfg.API_KEY, session_budget=args.budget,
                  min_plan_credits=cfg.MIN_PLAN_CREDITS)
    try:
        sports = ([{"key": args.sport, "title": args.sport}] if args.sport
                  else select_sports(api, cfg))
        if not sports:
            print(f"{C.YEL}Nothing in season among "
                  f"{', '.join(list(cfg.SPORT_KEYS) + list(cfg.SPORT_PREFIXES))}.{C.R}")
            return 0

        print(f"{C.B}Watching{C.R} {', '.join(s['title'] for s in sports)}")
        print(f"{C.GREY}books {', '.join(cfg.BOOKS)} · stake {money(cfg.TOTAL_STAKE)} · "
              f"min profit {money(cfg.MIN_PROFIT)} · every {args.interval}s · "
              f"{len(sports)} credit(s) per poll · budget {args.budget}{C.R}")
        print(f"{C.GREY}pre-match only — online in-play betting is prohibited "
              f"in Australia{C.R}\n")

        poll = 0
        seen = set()
        while True:
            poll += 1
            try:
                result = scan(api, cfg, sports)
            except BudgetExceeded as e:
                print(f"\n{C.YEL}{e}{C.R}"); return 0
            except QuotaExhausted as e:
                print(f"\n{C.RED}{e}{C.R}"); return 1
            except OddsAPIError as e:
                print(f"  {C.RED}fetch failed ({e.kind}): {e}{C.R}")
                if args.once: return 1
                time.sleep(args.interval); continue

            for o in result["playable"]:
                key = (o.event_id, round(o.best_margin_pct, 2))
                if key in seen:
                    continue            # do not re-alert on an unchanged price
                seen.add(key)
                sys.stdout.write(BELL)
                print_card(o, cfg)

            best = result["opportunities"][0] if result["opportunities"] else None
            line = (f"{C.GREY}[{datetime.now():%H:%M:%S}] poll {poll} · "
                    f"{len(result['opportunities'])} games · "
                    f"{len(result['playable'])} playable")
            if best:
                line += (f" · best {best.best_margin_pct:+.2f}%"
                         f"{' (' + money(best.profit) + ')' if best.profit > 0 else ''}")
            print(line + f" · {api.credits.summary()}{C.R}")

            if args.once:
                break
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        print(f"\n{C.GREY}Stopped. {api.credits.summary()}{C.R}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
