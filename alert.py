#!/usr/bin/env python3
"""
alert.py — scan once and send the result to Telegram.

    python3 alert.py --chats        # who can I message? free, no odds call
    python3 alert.py --test         # send a hello, free, no odds call
    python3 alert.py --dry-run      # scan and print the message, send nothing
    python3 alert.py                # scan; send ONLY if something crosses
    python3 alert.py --ignore-window   # ...even outside scanning hours

Silence is the default. A scan that finds nothing sends nothing, because an
alert arriving every 20 minutes to say "no arbitrage" is an alert you stop
reading, and then you miss the one that matters. The same fixture is also
suppressed for COOLDOWN_H hours unless its guaranteed profit improves.

Scans run every 20 minutes between 06:40 and 23:00 Sydney — 50 runs a day. The
window is enforced here rather than in the schedule so that daylight saving is
handled by the timezone database, and so an out-of-hours run exits before
making any request. Cost is 1 credit per sport key that returns games; empty
responses are free. At eleven keys that is up to 550 credits a day and about
16,500 a month, against the $30 plan's 20,000.

Why Telegram rather than SMS: no per-message cost, no carrier, no account
approval, and the whole client is two HTTPS calls against the bot API, so the
no-third-party-dependencies rule survives.

Setup, once:
  1. Message @BotFather, /newbot, copy the token
  2. Put it in .env beside config.py:   TELEGRAM_BOT_TOKEN=123456:ABC...
  3. Every recipient sends the bot any message (a bot cannot open a chat first)
  4. python3 alert.py --chats, and paste the IDs into config.TELEGRAM_CHAT_IDS
  5. python3 alert.py --test

An alert is a lead, not an instruction. By the time it reaches your phone the
prices are minutes old, so every message says to re-check before staking.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from arbtool.api import BudgetExceeded, OddsAPI, OddsAPIError, QuotaExhausted
from arbtool.core import recommend_first_leg
from arbtool.scan import scan

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000              # Telegram's ceiling is 4096; leave room for the footer

# Suppression state. Not history and not analytics — CLAUDE.md §12 keeps that in
# the research repo. This is the minimum needed to avoid sending the same
# opportunity every hour for as long as it stands, which would train you to
# ignore the alerts and so defeat the point of having them.
# Where the small state files live. Overridable because a CI runner starts with
# an empty filesystem every run and restores this directory from a cache — the
# suppression window is worthless if it resets to empty each time, which would
# re-send a standing arbitrage every 20 minutes.
STATE_DIR = Path(os.environ.get("ARB_STATE_DIR") or Path(__file__).parent)

# Sydney, not the machine's clock. On a UTC runner "local time" is meaningless,
# and the window has to survive daylight saving without anyone editing a cron
# line twice a year.
TZ = ZoneInfo("Australia/Sydney")
WINDOW_START = (6, 40)      # 06:40
WINDOW_END = (23, 0)        # 23:00

SEEN_FILE = STATE_DIR / ".alert_seen.json"
COOLDOWN_H = 6.0            # don't repeat the same pairing inside this window
IMPROVE = 1.25              # ...unless the guaranteed profit grew by this factor

# What bot.py reads to restake an alert at a different total. Holds the prices
# as sent, so a reply recomputes from the same numbers the message quoted and
# spends no credits doing it.
LAST_FILE = STATE_DIR / ".alert_last.json"
KEEP_LAST = 10

# A single failed scan is not worth a message — the next run is 20 minutes away
# and transient errors fix themselves. A run of them means the key was revoked,
# the quota is gone, or the network is down, and silence then is
# indistinguishable from a quiet market.
FAIL_FILE = STATE_DIR / ".alert_fails.json"
FAIL_ALERT_AT = 3

# bot.py answers replies; nothing about a dead listener is visible from the
# outside. You would discover it by sending a price from in front of a betting
# app and getting nothing back, which is the worst possible moment. The bot
# touches a file as it polls; the scan is already running on a schedule, so it
# is the natural place to notice that file going stale.
ALIVE_FILE = STATE_DIR / ".bot_alive.json"
BOT_STALE_MIN = 15          # comfortably past one ~50s long-poll cycle
BOT_WARN_COOLDOWN_H = 6.0   # a dead bot stays dead; do not say so every 20 min
BOT_WARNED_FILE = STATE_DIR / ".bot_warned.json"


def in_window(now=None) -> bool:
    """Is it inside the hours we scan, in Sydney time?

    The scheduler fires more often than this on purpose: it covers the union of
    the AEST and AEDT windows in UTC, and this trims it precisely. Doing the
    trimming here rather than in cron means daylight saving is handled by the
    timezone database instead of by someone remembering to edit a schedule in
    October and again in April.

    Cheap to be wrong-side-of-the-line: an out-of-window run makes no API call
    and spends no credits."""
    t = (now or datetime.now(TZ)).astimezone(TZ)
    return WINDOW_START <= (t.hour, t.minute) < WINDOW_END


def _sig(opp) -> str:
    """Identity of an opportunity: the game.

    Deliberately not the game plus the book pair. Ladbrokes and Neds are one
    Entain desk and quote identically, so the best pairing on a game flips
    between them on ties — which would look like a new opportunity every run
    and send the same match every 20 minutes. One fixture, one alert.

    Prices are excluded for the same reason: a cent of drift is the same
    opportunity, not a new one."""
    return str(opp.event_id)


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (OSError, ValueError):
        return {}                       # absent or corrupt: alert rather than swallow


def save_seen(seen: dict) -> None:
    cutoff = time.time() - COOLDOWN_H * 3600 * 4
    pruned = {k: v for k, v in seen.items() if v.get("at", 0) > cutoff}
    try:
        SEEN_FILE.write_text(json.dumps(pruned))
    except OSError as ex:
        # A read-only disk must not stop the alert going out.
        print(f"  could not write {SEEN_FILE.name}: {ex}")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    try:
        path.write_text(json.dumps(value))
    except OSError as ex:
        print(f"  could not write {path.name}: {ex}")


def record_failure() -> int:
    """Count consecutive failures. Returns the new count."""
    n = _read_json(FAIL_FILE, {}).get("consecutive", 0) + 1
    _write_json(FAIL_FILE, {"consecutive": n, "at": time.time()})
    return n


def clear_failures() -> None:
    if FAIL_FILE.exists():
        FAIL_FILE.unlink(missing_ok=True)


def check_bot_alive(now: float = None):
    """Has the reply listener stopped? Returns a message to send, or None.

    Only warns once the file has existed at least once — a machine that has
    never run bot.py is not a fault, and warning about it would train the user
    to ignore the message that matters. Warns at most once per cooldown, so a
    listener that stays down does not turn every scan into a notification."""
    now = now or time.time()
    beat = _read_json(ALIVE_FILE, None)
    if not beat:
        return None
    quiet_min = (now - beat.get("at", 0)) / 60
    if quiet_min < BOT_STALE_MIN:
        if BOT_WARNED_FILE.exists():
            BOT_WARNED_FILE.unlink(missing_ok=True)
        return None
    warned = _read_json(BOT_WARNED_FILE, {}).get("at", 0)
    if now - warned < BOT_WARN_COOLDOWN_H * 3600:
        return None
    _write_json(BOT_WARNED_FILE, {"at": now})
    return ("<b>The reply bot has stopped.</b>\n"
            f"Last seen {quiet_min:.0f} minutes ago. Scans and alerts are "
            f"unaffected — you will still be told when a market crosses — but "
            f"replying with a stake or a price will get no answer until it is "
            f"back.")


def save_last(opps) -> None:
    """Store what was just alerted so a Telegram reply can restake it.

    Prices only — no derived figures. bot.py feeds them back through
    evaluate() and allocate(), the same functions that produced the message, so
    a restake cannot disagree with the alert it came from."""
    items = _read_json(LAST_FILE, [])
    for o in opps:
        items.insert(0, {
            "sig": _sig(o),
            "home": o.home, "away": o.away, "sport": o.sport_title,
            "at": time.time(),
            "legs": [{"book": o.arb.legs[x].book, "outcome": x,
                      "odds": o.arb.legs[x].odds} for x in o.arb.outcomes],
            # The whole board, not just the winning pairing. A price quoted back
            # from a book we do not carry (bet365, read off the app) has to be
            # tested against every book on the game, not only the two that
            # happened to win — the improvement may come from pairing it with a
            # book that placed third.
            "board": {b: dict(prices)
                      for b, prices in o.analysis.book_odds.items()},
            "display": list(o.display_outcomes),
        })
    # Keep the newest few. Older alerts describe prices long since moved.
    seen_sigs, keep = set(), []
    for it in items:
        if it["sig"] in seen_sigs:
            continue
        seen_sigs.add(it["sig"])
        keep.append(it)
    _write_json(LAST_FILE, keep[:KEEP_LAST])


def is_fresh(opp, seen: dict, now: float) -> bool:
    """Has this opportunity already been sent recently at no better a price?"""
    prev = seen.get(_sig(opp))
    if prev is None:
        return True
    if now - prev.get("at", 0) > COOLDOWN_H * 3600:
        return True
    # Materially better than last time is worth interrupting for again.
    return opp.profit > prev.get("profit", 0) * IMPROVE


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _call(token: str, method: str, params: dict) -> dict:
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "arb-desk/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        # 401 here is nearly always a token that was copied with a space in it.
        raise RuntimeError(f"Telegram HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram unreachable: {e.reason}") from e


def send(token: str, chat_ids, text: str) -> int:
    """Send to every recipient. One failure must not silence the others, so
    each is attempted independently and errors are reported, not raised."""
    sent = 0
    for chat_id in chat_ids:
        try:
            r = _call(token, "sendMessage", {
                "chat_id": chat_id,
                "text": text[:MAX_LEN],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
            if r.get("ok"):
                sent += 1
            else:
                print(f"  {chat_id}: refused — {r.get('description')}")
        except RuntimeError as e:
            print(f"  {chat_id}: {e}")
    return sent


def list_chats(token: str) -> int:
    """Print chat IDs of everyone who has messaged the bot. Costs no credits."""
    r = _call(token, "getUpdates", {})
    if not r.get("ok"):
        print(f"getUpdates refused: {r.get('description')}")
        return 1
    seen = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name"))
                            if x) or chat.get("title") or chat.get("username") or "?"
            seen[str(chat["id"])] = f"{name} ({chat.get('type')})"
    if not seen:
        print("No messages yet. Each recipient must send the bot a message first —")
        print("a bot cannot open a conversation. Then run this again.")
        print("Note Telegram only keeps recent updates, so do it shortly before this.")
        return 1
    print("Chat IDs that have messaged this bot:\n")
    for cid, who in seen.items():
        print(f"  {cid:<16}{who}")
    print("\nPaste into config.TELEGRAM_CHAT_IDS, or set TELEGRAM_CHAT_IDS in .env:")
    print(f"  TELEGRAM_CHAT_IDS={','.join(seen)}")
    return 0


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

def e(x) -> str:
    return html.escape(str(x))


def money(x) -> str:
    return f"${x:,.2f}"


def whole(x) -> str:
    """Stakes only. allocate() already rounds every leg to STAKE_INCREMENT, so
    these are whole dollars before they get here — printing the cents just adds
    digits to mistype into a betting slip under time pressure."""
    return f"${x:,.0f}"


def format_hit(opp, cfg) -> str:
    """One arbitrage, as a placeable instruction.

    Leads with worst_profit, never the headline margin: after whole-dollar
    rounding the legs no longer pay identically, so it is the only figure that
    is actually guaranteed."""
    arb = opp.arb
    first = recommend_first_leg(arb)
    order = [first] + [o for o in arb.outcomes if o != first]

    lines = [
        f"<b>{money(arb.worst_profit)} guaranteed</b>  "
        f"({arb.realised_margin * 100:.2f}% on {whole(arb.total_stake)})",
        f"{e(opp.home)} v {e(opp.away)}",
        f"<i>{e(opp.sport_title)}</i>",
        "",
    ]
    for i, o in enumerate(order, 1):
        leg = arb.legs[o]
        when = "PLACE FIRST" if o == first else "then"
        lines.append(f"<b>{i}. {when}</b> — {e(leg.book)}")
        lines.append(f"    back {e(o)} @ {leg.odds:.2f}")
        lines.append(f"    <b>stake {whole(leg.stake)}</b> → returns {money(leg.returns)}")
    lines.append("")
    # Return range first, then what went in, then what is locked in. Ordered the
    # way the money is reasoned about rather than the way it is calculated.
    # realised_margin, not Arb.margin: after whole-dollar rounding the legs stop
    # paying identically, so the theoretical margin is no longer achievable and
    # quoting it here would overstate every alert.
    lines.append(f"Returns {money(arb.worst_return)}–{money(arb.best_return)}, "
                 f"money put in {whole(arb.total_stake)}, "
                 f"<b>guaranteed {money(arb.worst_profit)} @ "
                 f"{arb.realised_margin * 100:.2f}%</b>")
    # The exposure is one leg, not the outlay. Every surface says so; so does this.
    lines.append(f"<i>If leg 2 misses you are unhedged on "
                 f"{money(arb.legs[first].stake)} — that, not the outlay, is the risk.</i>")

    # Named prices, not a general reminder. The whole edge usually rests on one
    # book being out of line with the rest, and that is exactly the price most
    # likely to be stale in the feed. Checking it is the difference between an
    # arbitrage and an unhedged bet.
    lines.append("")
    lines.append("<b>CHECK BEFORE STAKING</b> — open both apps and confirm:")
    for o in order:
        leg = arb.legs[o]
        lines.append(f"    {e(leg.book)} — {e(o)} showing <b>{leg.odds:.2f}</b> or better")
    lines.append("<i>If either price has shortened, the arbitrage is gone. "
                 "Do not place a leg on the strength of this message alone.</i>")
    return "\n".join(lines)


def build_messages(playable, cfg) -> list:
    """One message per arbitrage.

    Not one combined message: two games in a single Telegram bubble means
    scrolling past one bet slip to read another, on a phone, while both prices
    are moving. Separate messages also let Telegram's own notification grouping
    do the work."""
    return [format_hit(o, cfg) for o in playable]


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chats", action="store_true",
                   help="list chat IDs that have messaged the bot (no odds call)")
    p.add_argument("--test", action="store_true",
                   help="send a test message (no odds call)")
    p.add_argument("--dry-run", action="store_true",
                   help="scan and print the message, send nothing")
    p.add_argument("--force", action="store_true",
                   help="ignore the repeat-suppression window")
    p.add_argument("--ignore-window", action="store_true",
                   help="run even outside 06:40-23:00 Sydney (manual runs)")
    p.add_argument("--budget", type=int, default=config.SESSION_CREDIT_BUDGET)
    args = p.parse_args()

    token = config.TELEGRAM_BOT_TOKEN
    if not token and not args.dry_run:
        print("No bot token. Message @BotFather, then put it in .env:")
        print("    TELEGRAM_BOT_TOKEN=123456:ABC...")
        return 2

    if args.chats:
        return list_chats(token)

    chat_ids = list(config.TELEGRAM_CHAT_IDS)
    if not chat_ids and not args.dry_run:
        print("No recipients. Each person messages the bot once, then:")
        print("    python3 alert.py --chats")
        return 2

    if args.test:
        n = send(token, chat_ids, "<b>Arb Desk</b>\nAlerts are wired up. "
                                  "No odds were fetched and no credits spent.")
        print(f"sent to {n}/{len(chat_ids)}")
        return 0 if n else 1

    if not config.API_KEY:
        print("No ODDS_API_KEY. Put it in .env beside config.py.")
        return 2

    if not (args.ignore_window or args.dry_run) and not in_window():
        # Exits before any request, so an out-of-hours run costs nothing.
        print(f"[{datetime.now(TZ):%H:%M}] outside "
              f"{WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}-"
              f"{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} Sydney — no scan")
        return 0

    # Before spending a credit: a listener that died is worth knowing about
    # whether or not this scan finds anything.
    dead = check_bot_alive()
    if dead and not args.dry_run and chat_ids:
        send(token, chat_ids, dead)
        print("  reply bot looks stopped — warned")

    api = OddsAPI(config.API_KEY, session_budget=args.budget,
                  min_plan_credits=config.MIN_PLAN_CREDITS)
    try:
        result = scan(api, config)
    except (BudgetExceeded, QuotaExhausted, OddsAPIError) as ex:
        # One failure is noise — the next run is 20 minutes away and transient
        # errors clear themselves. Three in a row is an outage, and staying
        # quiet then looks exactly like a market with no arbitrage in it.
        fails = record_failure()
        print(f"[{datetime.now():%H:%M:%S}] scan failed ({fails} in a row): {ex}")
        if fails == FAIL_ALERT_AT and not args.dry_run and chat_ids:
            send(token, chat_ids,
                 f"<b>Arb Desk</b> — {fails} scans in a row have failed.\n"
                 f"<code>{e(ex)}</code>\n"
                 f"<i>No odds are being checked. Alerts will stay silent until "
                 f"this clears, and silence otherwise means no arbitrage.</i>")
        return 1
    clear_failures()

    playable = list(result["playable"])
    n_games = len(result["opportunities"])

    # Drop opportunities already sent recently at no better a price, so a
    # standing arb does not arrive every hour until it closes.
    now = time.time()
    seen = {} if args.force else load_seen()
    suppressed = [o for o in playable if not is_fresh(o, seen, now)]
    playable = [o for o in playable if is_fresh(o, seen, now)]
    result["playable"] = playable
    if suppressed:
        print(f"  {len(suppressed)} already alerted within {COOLDOWN_H:.0f}h — suppressed")

    if not playable:
        # Silence is the whole design. Nothing crossed, or everything that
        # crossed has already been sent, so there is nothing worth a buzz.
        print(f"[{datetime.now():%H:%M:%S}] nothing new ({n_games} games) · "
              f"{api.credits.summary()}")
        return 0

    messages = build_messages(playable, config)

    if args.dry_run:
        print(("\n" + "-" * 60 + "\n").join(messages))
        return 0

    ok = 0
    for opp, text in zip(playable, messages):
        n = send(token, chat_ids, text)
        if n:
            ok += 1
            # Record only what actually went out — a failed send must be retried
            # by the next run, not swallowed by the suppression window.
            seen[_sig(opp)] = {"at": now, "profit": opp.profit}
    if ok:
        save_seen(seen)
        save_last([o for o, t in zip(playable, messages)])
    print(f"[{datetime.now():%H:%M:%S}] {ok}/{len(playable)} alert(s) sent of "
          f"{n_games} games · {api.credits.summary()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
