#!/usr/bin/env python3
"""
verify_live.py — prove to yourself that the feed is live and accurate.

Run this with your phone in your hand. It makes ONE real API call, prints every
bookmaker's price with the exact timestamp the API says it was captured, and
then asks you to compare against the bookmaker's own app.

    python3 verify_live.py --sport rugbyleague_nrl
    python3 verify_live.py --sport rugbyleague_nrl --watch 5   # 5 polls, 60s apart

Cost: 1 credit per poll (h2h, au region, one sport).

Why this script exists
----------------------
"Live" is doing a lot of work in "live odds API". The endpoint returns the most
recent price The Odds API captured from each bookmaker — not the price on that
bookmaker's screen this instant. The gap between those two things is the single
biggest risk in arbitrage betting off an API feed, because an arbitrage that
existed 90 seconds ago may not exist now.

Nobody should take that on trust, including from whoever wrote this tool. Run
it, compare against your phone, and find out what the real lag is for your
books before you put money through it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from arbtool.api import OddsAPI, OddsAPIError, parse_iso

BOOKS = ["Sportsbet", "Ladbrokes", "TAB", "Neds"]


def fmt_local(dt):
    return dt.astimezone().strftime("%H:%M:%S") if dt else "—"


def poll(api, sport, regions, books, n):
    request_sent = datetime.now(timezone.utc)
    events = api.odds(sport, regions=regions, markets="h2h")
    response_at = datetime.now(timezone.utc)

    print(f"\n{'='*78}")
    print(f"POLL {n}   request sent {fmt_local(request_sent)}   "
          f"response {fmt_local(response_at)}   "
          f"round trip {(response_at-request_sent).total_seconds():.2f}s")
    print("=" * 78)

    if not events:
        print("  No events returned. Either nothing is scheduled or the market is closed.")
        return []

    rows = []
    for ev in events[:6]:
        start = parse_iso(ev.get("commence_time", ""))
        mins = (start - response_at).total_seconds() / 60 if start else None
        state = "IN PLAY" if (mins is not None and mins <= 0) else \
                (f"starts in {mins:.0f} min" if mins is not None else "?")
        print(f"\n  {ev.get('home_team','?')}  v  {ev.get('away_team','?')}   [{state}]")
        print(f"  {'BOOKMAKER':<14}{'HOME':>8}{'AWAY':>8}   "
              f"{'CAPTURED AT':>12}{'AGE':>8}")
        print(f"  {'-'*52}")

        for bk in ev.get("bookmakers", []):
            title = bk.get("title", bk.get("key", "?"))
            if books and title not in books:
                continue
            upd = parse_iso(bk.get("last_update", ""))
            age = (response_at - upd).total_seconds() if upd else None
            h2h = next((m for m in bk.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            prices = {o.get("name"): o.get("price") for o in h2h.get("outcomes", [])}
            ph = prices.get(ev.get("home_team"), "—")
            pa = prices.get(ev.get("away_team"), "—")
            flag = "  <-- STALE" if (age is not None and age > 120) else ""
            print(f"  {title:<14}{ph:>8}{pa:>8}   "
                  f"{fmt_local(upd):>12}{(f'{age:.0f}s' if age is not None else '—'):>8}{flag}")
            rows.append((title, age))
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sport", default="rugbyleague_nrl")
    p.add_argument("--regions", default="au")
    p.add_argument("--watch", type=int, default=1,
                   help="number of polls (60s apart). Each costs 1 credit.")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--all-books", action="store_true",
                   help="show every bookmaker, not just the four tracked")
    args = p.parse_args()

    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        print("Set ODDS_API_KEY first:  export ODDS_API_KEY='your_key'")
        return 2

    api = OddsAPI(key, session_budget=args.watch + 2)
    books = None if args.all_books else BOOKS

    print(f"Verifying the feed is live — {args.sport}, region {args.regions}")
    print(f"{args.watch} poll(s), {args.interval}s apart. "
          f"Have a bookmaker app open on your phone.")

    ages = []
    try:
        for i in range(1, args.watch + 1):
            ages += [a for _, a in poll(api, args.sport, args.regions, books, i) if a is not None]
            if i < args.watch:
                print(f"\n  waiting {args.interval}s…")
                time.sleep(args.interval)
    except OddsAPIError as e:
        print(f"\n  Request failed ({e.kind}): {e}")
        return 1

    print(f"\n{'='*78}")
    print("WHAT TO CHECK")
    print("=" * 78)
    if ages:
        ages.sort()
        med = ages[len(ages)//2]
        print(f"  Quote age: median {med:.0f}s, freshest {min(ages):.0f}s, "
              f"oldest {max(ages):.0f}s across {len(ages)} quotes.")
        print()
        if med > 120:
            print("  These are OLD. Prices this stale are not safe to arbitrage from —")
            print("  by the time you place, the market has moved. Treat any edge the")
            print("  tool reports as a lead to check manually, not an instruction.")
        elif med > 45:
            print("  Middling freshness. Usable, but confirm both prices on the books'")
            print("  own sites before staking anything.")
        else:
            print("  Fresh. Still confirm on the book's site before placing —")
            print("  the API can only ever tell you what was true a moment ago.")
    print()
    print("  Now open the bookmaker apps and compare, line by line:")
    print("    1. Do the prices match what the app shows right now?")
    print("    2. If they differ, by how much, and is the app higher or lower?")
    print("    3. Does the app offer a boosted or promotional price the API cannot see?")
    print()
    print("  Prices differing is NOT necessarily a bug. Known causes:")
    print("    - the API reports its last capture, not the live screen")
    print("    - books show personalised or promotional prices to logged-in users")
    print("    - some books are only partially covered by the API")
    print("    - your state may see a different market to the one the API carries")
    print()
    print("  Run this a few times before you stake anything. If the API consistently")
    print("  lags your phone by more than a minute or two, arbitrage off this feed")
    print("  is not realistic and it is better to know that now than after a bet.")
    print(f"\n  {api.credits.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
