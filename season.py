#!/usr/bin/env python3
"""
season.py — tell me when there is enough sport on to be worth paying for odds.

    python3 season.py              # check, alert on change, update state
    python3 season.py --dry-run    # check and print, send nothing, save nothing
    python3 season.py --force      # send the report even if nothing changed

Why this exists
---------------
The $30 plan buys 20,000 credits a month and the scheduled cron spends about
18,240 of them. When the codes that carry the board wind down — NRL and AFL
finish within a fortnight of each other — that spend keeps happening against a
thinner and thinner set of fixtures. The honest response is to stop paying and
come back when the calendar refills.

Coming back needs a signal, and the signal must not itself cost money, or you
are paying a subscription to find out whether to pay a subscription. The
/sports endpoint is free on every tier including the 500-credit free one, so
this script can run daily forever without spending a credit.

THE ONE RULE: this script must never call api.odds(). Every other entry point
in this repo spends credits; this one is the exception that makes the free tier
viable, and the moment it fetches a price it stops being safe to run on a timer
against a 500-credit allowance. arbtool.api.OddsAPI is constructed here with a
session budget of zero so that a future edit that adds an odds call fails
immediately and loudly rather than quietly eating the month.

What counts as news
-------------------
A key that was not active on the previous run and is now, or the reverse.
Tennis is collapsed to one pseudo-key (see config.SEASON_COLLAPSE_PREFIXES),
because it carries a key per tournament and the set turns over weekly — tracked
individually it would send a message most days and none of them would mean "a
season has started".

State lives in config.SEASON_STATE_PATH. A missing state file is treated as
first run: it records what is active and stays silent, rather than announcing
every sport in the world as new.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from arbtool.api import OddsAPI, OddsAPIError
from arbtool.scan import markets_for

import alert  # for send() — reuses the one Telegram path the rest of the tool uses


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict | None:
    """The previous run's view, or None if there wasn't one.

    None and {} mean different things and must not be conflated: no file means
    first run and stay quiet, while an empty key set is a real observation
    meaning nothing was in season, and a sport appearing after it is news.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(path: Path, keys: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "keys": keys,
    }, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

def collapse(key: str, cfg) -> str:
    """Map a sport key to the identity it is tracked under.

    Tennis returns "tennis_*" for every tournament so that Cincinnati ending
    and Winston-Salem starting is not reported as one sport leaving and another
    arriving on the same day.
    """
    for prefix in getattr(cfg, "SEASON_COLLAPSE_PREFIXES", ()):
        if key.startswith(prefix):
            return prefix + "*"
    return key


def observe(api: OddsAPI, cfg) -> dict:
    """Currently active, watchable sport keys as {key: title}. Costs nothing.

    two_way_only=False with prefixes=() asks the API for everything active and
    non-outright; the narrowing to sports this tool can hedge is done here
    against config.SEASON_WATCH_PREFIXES rather than against the api module's
    own TWO_WAY_PREFIXES, which omits AFL, NFL and NHL — all three are in
    config.SPORT_KEYS and all three are two-outcome in the markets we request.
    """
    prefixes = tuple(getattr(cfg, "SEASON_WATCH_PREFIXES", ()))
    active = api.sports(two_way_only=False, prefixes=())
    out: dict[str, str] = {}
    for s in active:
        key = s.get("key", "")
        if not prefixes or key.startswith(prefixes):
            tracked = collapse(key, cfg)
            # First title wins for a collapsed group; the group name is what is
            # displayed, so which tournament supplied it does not matter.
            out.setdefault(tracked, s.get("title") or tracked)
    return out


def poll_cost(api: OddsAPI, cfg) -> tuple[int, int]:
    """What one alert.py poll would cost right now, and how many keys it hits.

    Priced through scan.markets_for and the same SPORT_KEYS/SPORT_PREFIXES
    selection alert.py uses, so the number in the message is the number the
    cron would actually spend rather than an estimate. Uses the /sports
    response already fetched semantics — free either way.
    """
    keys = set(getattr(cfg, "SPORT_KEYS", None) or ())
    prefixes = tuple(getattr(cfg, "SPORT_PREFIXES", None) or ())
    n_regions = len([r for r in cfg.REGIONS.split(",") if r])
    active = api.sports(two_way_only=False, prefixes=())
    selected = [s for s in active
                if s["key"] in keys or (prefixes and s["key"].startswith(prefixes))]
    cost = sum(len([m for m in markets_for(cfg, s["key"]).split(",") if m]) * n_regions
               for s in selected)
    return cost, len(selected)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(new: dict, gone: dict, current: dict,
                 cost: int, n_selected: int, cfg) -> str:
    """The Telegram message. HTML, because alert.send() posts with parse_mode
    HTML and an unescaped bare "&" in a sport title would be refused."""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = []
    if new:
        lines.append(f"<b>Season watch — {len(new)} sport(s) started</b>")
        for key in sorted(new):
            lines.append(f"  + <code>{esc(key)}</code>  {esc(new[key])}")
    if gone:
        if lines:
            lines.append("")
        lines.append(f"<b>Season watch — {len(gone)} sport(s) ended</b>")
        for key in sorted(gone):
            lines.append(f"  − <code>{esc(key)}</code>  {esc(gone[key])}")
    if not lines:
        lines.append("<b>Season watch — no change</b>")

    lines.append("")
    lines.append(f"{len(current)} watchable key(s) in season.")

    # The decision line. A poll cost of zero means the configured sports are all
    # out of season and paying for odds would buy nothing; a cost that no longer
    # fits the free allowance is the cue to re-subscribe rather than to tighten.
    plan = int(getattr(cfg, "SEASON_PLAN_CREDITS", 500) or 500)
    if n_selected == 0:
        lines.append("Nothing in config.SPORT_KEYS is in season — a poll would "
                     "return no games and cost nothing.")
    else:
        per_day = cost * 32           # the */30 cron's billable runs in the window
        lines.append(f"A poll of the {n_selected} configured key(s) costs {cost} "
                     f"credit(s): {per_day}/day, ~{per_day * 30:,}/month on the "
                     f"*/30 cron, against a {plan:,}-credit plan.")
        if per_day * 30 > plan:
            lines.append("That does not fit the current plan. Run alert.py by "
                         "hand, or re-subscribe.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="print the report, send nothing, write no state")
    p.add_argument("--force", action="store_true",
                   help="send even when nothing changed")
    args = p.parse_args(argv)

    if not config.API_KEY:
        print("No API key. Put ODDS_API_KEY in .env beside config.py.")
        return 1

    # session_budget=0 is a tripwire, not a limit: /sports is charged cost=0 so
    # it passes, while any odds call would raise BudgetExceeded on the spot.
    # This script running on a timer against a 500-credit plan is only safe for
    # as long as that stays true.
    api = OddsAPI(config.API_KEY, session_budget=0,
                  min_plan_credits=0)

    try:
        current = observe(api, config)
        cost, n_selected = poll_cost(api, config)
    except OddsAPIError as e:
        print(f"/sports failed: {e}")
        return 1

    state = load_state(Path(config.SEASON_STATE_PATH))
    first_run = state is None
    previous = dict((state or {}).get("keys") or {})

    new = {k: v for k, v in current.items() if k not in previous}
    gone = {k: v for k, v in previous.items() if k not in current}

    report = build_report(new, gone, current, cost, n_selected, config)
    print(report.replace("<b>", "").replace("</b>", "")
                .replace("<code>", "").replace("</code>", ""))

    if args.dry_run:
        print("\n(dry run — nothing sent, state not written)")
        return 0

    # A first run has nothing to compare against. Recording the baseline in
    # silence is the difference between a useful watcher and one whose first
    # act is to list every sport on earth as breaking news.
    if first_run:
        save_state(Path(config.SEASON_STATE_PATH), current)
        print(f"\nFirst run — baseline of {len(current)} key(s) recorded, "
              f"nothing sent.")
        return 0

    changed = bool(new or gone)
    should_send = changed or args.force or getattr(config, "SEASON_ALWAYS_REPORT", False)

    if should_send and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_IDS:
        sent = alert.send(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_IDS, report)
        print(f"\nSent to {sent}/{len(config.TELEGRAM_CHAT_IDS)} chat(s).")
    elif should_send:
        print("\nNo Telegram token or chat IDs configured — printed only.")
    else:
        print("\nNo change; nothing sent.")

    save_state(Path(config.SEASON_STATE_PATH), current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
