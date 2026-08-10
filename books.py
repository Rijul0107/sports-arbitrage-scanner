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

2. One poll costs [markets] x [regions] per *active sport key*, not per sport.
   Markets are per sport in config.SPORT_MARKETS — most keys ask for h2h alone,
   NRL and AFL ask for three. SPORT_PREFIXES is a prefix match, and tennis
   carries a separate key per tournament, so the real cost of a poll changes
   week to week as tournaments come and go. --sports prices your next poll
   before you commit to an interval.

--sports uses only the free /sports endpoint. --list needs one odds request,
which is where the bookmaker names live; that is 1 credit for h2h in one region.
"""

from __future__ import annotations

import argparse
import sys

import config
from arbtool.api import OddsAPI, OddsAPIError
from arbtool.scan import markets_for, select_sports


def runs_per_day(cfg) -> int:
    """How many scheduled runs land inside the scan window.

    Cron fires on the wall clock, so a `*/30` schedule hits :00 and :30 and
    misses a window edge at :40 entirely — counting hours and dividing gets
    this wrong. Walk the day a minute at a time instead; it is exact and free.

    `*/n` on the minute field means "minutes 0-59 divisible by n", NOT "every n
    minutes rolling". They coincide only when n divides 60. `*/25` fires at :00,
    :25 and :50 — three times an hour with a ten minute gap at the top, so it
    costs exactly what `*/20` costs rather than the fifth less the name implies.
    Modelling it as a rolling interval understates a `*/25` month by a third.

    alert.py trims the window itself, so runs outside it exit before making a
    request and are free. Only the ones counted here are billed."""
    every = max(1, int(getattr(cfg, "CRON_MINUTES", 30)))
    start = tuple(getattr(cfg, "SCAN_WINDOW_START", (6, 40)))
    end = tuple(getattr(cfg, "SCAN_WINDOW_END", (22, 40)))
    return sum(1 for h in range(24) for m in range(60)
               if m % every == 0 and start <= (h, m) < end)


def show_sports(api: OddsAPI, cfg) -> list:
    """Active sport keys matching the configured prefixes. Costs nothing."""
    sports = select_sports(api, cfg)

    print(f"\nACTIVE SPORT KEYS — named {list(cfg.SPORT_KEYS)} "
          f"plus prefixes {list(cfg.SPORT_PREFIXES)}")
    print("-" * 72)
    if not sports:
        print("  None in season. A poll right now would return no games.")
        return sports
    # Cost is markets x regions per key, not one credit per key, so the markets
    # have to be priced here too — NRL and AFL ask for three each. Read through
    # scan.markets_for so this cannot drift from what a poll actually requests.
    n_regions = len([r for r in cfg.REGIONS.split(",") if r])
    per_key = {}
    for s in sports:
        markets = [m for m in markets_for(cfg, s["key"]).split(",") if m]
        per_key[s["key"]] = len(markets) * n_regions
        extra = "" if markets == ["h2h"] else f"   {','.join(markets)}"
        print(f"  {s['key']:<34} {s.get('title', ''):<22}"
              f"{per_key[s['key']]:>2} cr{extra}")

    n = sum(per_key.values())
    print("-" * 72)
    print(f"  {len(sports)} key(s) -> one poll costs {n} credit(s)")
    print(f"  at {cfg.POLL_SECONDS}s intervals that is {n * 3600 // cfg.POLL_SECONDS} "
          f"credits/hour")
    if n:
        print(f"  your session budget of {cfg.SESSION_CREDIT_BUDGET} lasts "
              f"{cfg.SESSION_CREDIT_BUDGET // n} poll(s) "
              f"= {cfg.SESSION_CREDIT_BUDGET // n * cfg.POLL_SECONDS // 60} min")
        # What the scheduled cron actually spends, which is the number that
        # decides whether a new market fits. Counted from the configured window
        # rather than written down, because a hardcoded run count is wrong the
        # first time either the window or the interval moves.
        runs = runs_per_day(cfg)
        start, end = cfg.SCAN_WINDOW_START, cfg.SCAN_WINDOW_END
        print(f"  cron */{cfg.CRON_MINUTES} over "
              f"{start[0]:02d}:{start[1]:02d}-{end[0]:02d}:{end[1]:02d} Sydney "
              f"= {runs} runs/day: {n * runs} a day, {n * runs * 30:,} a month")
        # Read from the response header rather than assuming a tier. The free
        # key is 500 a month and the paid one 20,000 — a poll that is a rounding
        # error on one is a third of a day's allowance on the other, and
        # printing the wrong plan size is how you talk yourself into a config
        # you cannot afford.
        left = api.credits.remaining_reported
        if left is not None:
            print(f"  {left:,} credits left on this key -> {left // n} more "
                  f"poll(s), or {left // (n * 32)} full day(s) of that cron")
            if left < n * 32:
                print("  NOT ENOUGH FOR ONE DAY of the scheduled cron. Trim "
                      "config.SPORT_MARKETS or run manually only.")
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
