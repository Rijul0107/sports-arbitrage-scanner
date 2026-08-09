#!/usr/bin/env python3
"""
books.py — find out what the API actually calls things, before you spend on odds.

    python3 books.py --sports        # in-season sport keys and poll cost. FREE.
    python3 books.py --list          # bookmaker titles in your region. 1 credit.
    python3 books.py --list --sport tennis_atp_cincinnati

Why this exists
---------------
Two things silently produce an empty dashboard rather than an error, and both
are answered here.

1. config.BOOKS must match the API's bookmaker `title` exactly. scan.py keeps
   only books whose title is in that list and returns None for a game with
   fewer than two of them left. A single wrong character therefore looks
   identical to "no arbitrage today". --list prints the real titles and marks
   which of your configured names matched.

2. One poll costs 1 credit per *active sport key*, not per sport. SPORT_PREFIXES
   is a prefix match, and tennis carries a separate key per tournament, so the
   real cost of a poll changes week to week as tournaments come and go.
   --sports prices your next poll before you commit to an interval.

--sports uses only the free /sports endpoint. --list needs one odds request,
which is where the bookmaker names live; that is 1 credit for h2h in one region.
"""

from __future__ import annotations

import argparse
import sys

import config
from arbtool.api import OddsAPI, OddsAPIError
from arbtool.scan import select_sports


def show_sports(api: OddsAPI, cfg) -> list:
    """Active sport keys matching the configured prefixes. Costs nothing."""
    sports = select_sports(api, cfg)

    print(f"\nACTIVE SPORT KEYS — named {list(cfg.SPORT_KEYS)} "
          f"plus prefixes {list(cfg.SPORT_PREFIXES)}")
    print("-" * 72)
    if not sports:
        print("  None in season. A poll right now would return no games.")
        return sports
    for s in sports:
        print(f"  {s['key']:<34} {s.get('title', '')}")

    n = len(sports)
    # Every key in this list is one odds request per poll, and the free tier is
    # 500 a month. Stating the cost here is the whole point of the free call.
    print("-" * 72)
    print(f"  {n} key(s) -> one poll costs {n} credit(s)")
    print(f"  at {cfg.POLL_SECONDS}s intervals that is {n * 3600 // cfg.POLL_SECONDS} "
          f"credits/hour")
    if n:
        print(f"  your session budget of {cfg.SESSION_CREDIT_BUDGET} lasts "
              f"{cfg.SESSION_CREDIT_BUDGET // n} poll(s) "
              f"= {cfg.SESSION_CREDIT_BUDGET // n * cfg.POLL_SECONDS // 60} min")
    return sports


def show_books(api: OddsAPI, cfg, sport_key: str) -> int:
    """Bookmaker titles present in one sport's odds response. Costs 1 credit."""
    events = api.odds(sport_key, regions=cfg.REGIONS, markets="h2h")
    if not events:
        print(f"\n  No events returned for {sport_key}. Nothing scheduled, or the")
        print("  market is closed. Try another key from --sports.")
        return 1

    # Titles vary by event only in that some books do not price every game, so
    # union across all events rather than trusting the first one.
    seen: dict[str, int] = {}
    for ev in events:
        for bk in ev.get("bookmakers", []) or []:
            title = bk.get("title") or bk.get("key") or "?"
            seen[title] = seen.get(title, 0) + 1

    print(f"\nBOOKMAKER TITLES — {sport_key}, region {cfg.REGIONS}, "
          f"{len(events)} event(s)")
    print("-" * 72)
    print(f"  {'TITLE':<28}{'PRICES':>8}   CONFIGURED")
    for title, count in sorted(seen.items(), key=lambda kv: -kv[1]):
        mark = "yes" if title in cfg.BOOKS else ""
        print(f"  {title:<28}{count:>8}   {mark}")

    missing = [b for b in cfg.BOOKS if b not in seen]
    print("-" * 72)
    if missing:
        # This is the failure that looks like "no arbitrage" instead of an error.
        print(f"  NOT FOUND, so silently ignored by every scan: {', '.join(missing)}")
        print(f"  Copy the exact title from the list above into config.BOOKS.")
    else:
        print(f"  All {len(cfg.BOOKS)} configured books present. "
              f"{len(cfg.BOOKS) * (len(cfg.BOOKS) - 1) // 2} pairings per game.")

    matched = len(cfg.BOOKS) - len(missing)
    if matched < 2:
        print("  Fewer than two books match — no game can be assessed at all.")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sports", action="store_true",
                   help="list in-season sport keys and price a poll (free)")
    p.add_argument("--list", action="store_true", dest="list_books",
                   help="list bookmaker titles in your region (1 credit)")
    p.add_argument("--sport", help="sport key for --list; default is the first active one")
    args = p.parse_args()

    if not (args.sports or args.list_books):
        p.print_help()
        return 2

    if not config.API_KEY:
        print("No API key. Either:")
        print("    export ODDS_API_KEY='your_key'   (free at the-odds-api.com)")
        print("  or put ODDS_API_KEY=... in a .env file beside config.py")
        return 2

    # A tight budget: this is a diagnostic, it should never be able to eat the
    # month even if something upstream starts looping.
    api = OddsAPI(config.API_KEY, session_budget=2,
                  min_plan_credits=config.MIN_PLAN_CREDITS)

    try:
        sports = []
        if args.sports or not args.sport:
            sports = show_sports(api, config)

        rc = 0
        if args.list_books:
            sport_key = args.sport
            if not sport_key:
                if not sports:
                    print("\n  No active sport to query. Pass --sport explicitly.")
                    return 1
                sport_key = sports[0]["key"]
                print(f"\n  Using {sport_key}. Pass --sport to choose another.")
            rc = show_books(api, config, sport_key)
    except OddsAPIError as e:
        print(f"\n  Request failed ({e.kind}): {e}")
        return 1

    print(f"\n  {api.credits.summary()}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
