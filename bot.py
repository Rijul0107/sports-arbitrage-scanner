#!/usr/bin/env python3
"""
bot.py — reply to an alert with a stake and get the numbers back.

    python3 bot.py                 # run the listener (Ctrl-C to stop)
    python3 bot.py --once          # drain pending messages and exit

Send the bot a number and it restakes the most recent alert at that total:

    you   4000
    bot   $2,000 -> $4,000  ·  Brewers v Twins
          1. PointsBet (AU)   Twins    @ 3.60   $1,111
          2. Ladbrokes        Brewers  @ 1.44   $2,889
          guaranteed $84.48

Costs no API credits. It recomputes from the prices already in the alert, and
it recomputes them with evaluate() and allocate() — the same functions that
produced the original message — so a restake can never disagree with the alert
it came from.

That also means the prices are as old as the alert. A restake is arithmetic on
what was true when the message was sent, not a fresh quote, so the reply repeats
the instruction to check both books before staking.

Runs alongside cron, not instead of it: alert.py finds arbitrage on a schedule,
this answers questions about what it found. Keep it running under launchd if you
want it always available — see the README section this file's setup prints.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from pathlib import Path

import config
from alert import LAST_FILE, _call, _read_json, e, money, send, whole
from arbtool.core import allocate, evaluate, recommend_first_leg

OFFSET_FILE = Path(__file__).parent / ".bot_offset.json"
POLL_TIMEOUT = 50           # seconds of long-poll; Telegram holds the connection
MIN_STAKE = 10.0
MAX_STAKE = 100_000.0

HELP = (
    "<b>Arb Desk</b>\n"
    "Send a number to restake the most recent alert, e.g. <code>4000</code>.\n\n"
    "<code>/last</code> — show the most recent alert again\n"
    "<code>/help</code> — this message\n\n"
    "<i>No new odds are fetched. Replies recompute from the prices in the "
    "alert, so check both books before staking.</i>"
)


def parse_amount(text: str):
    """A bare number, with or without $ and commas. Anything else is not a
    stake and must not be silently treated as one."""
    t = text.strip().lstrip("$").replace(",", "").replace("_", "")
    if t.lower().startswith("/stake"):
        t = t[6:].strip().lstrip("$").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    if not (MIN_STAKE <= v <= MAX_STAKE):
        return None
    return v


def restake(item: dict, total: float, cfg) -> str:
    """Recompute one stored alert at a new total.

    Rebuilds the market from the stored prices and runs it back through the
    real engine rather than reimplementing the split here — the dual-engine
    problem in CLAUDE.md §7 is already one duplicate implementation too many."""
    book_odds = {}
    for leg in item["legs"]:
        book_odds.setdefault(leg["book"], {})[leg["outcome"]] = leg["odds"]

    arb = evaluate(book_odds, commission_pct=cfg.COMMISSION_PCT, expect_outcomes=2)
    if arb is None:
        return ("Could not rebuild that market from the stored prices. "
                "Wait for the next alert.")
    allocate(arb, total, increment=cfg.STAKE_INCREMENT)

    if arb.worst_profit <= 0:
        # Rounding can destroy a thin edge: the legs no longer pay identically,
        # so a stake too small to absorb the rounding returns a certain loss.
        need = _min_viable(arb, cfg)
        msg = [f"<b>{whole(total)} does not work here.</b>",
               f"{e(item['home'])} v {e(item['away'])}",
               f"After whole-dollar rounding the guaranteed return is "
               f"{money(arb.worst_profit)} — a loss."]
        if need:
            msg.append(f"Smallest total that still clears: <b>{whole(need)}</b>")
        return "\n".join(msg)

    first = recommend_first_leg(arb)
    order = [first] + [o for o in arb.outcomes if o != first]
    lines = [
        f"<b>{money(arb.worst_profit)} guaranteed</b>  "
        f"({arb.realised_margin * 100:.2f}% on {whole(arb.total_stake)})",
        f"{e(item['home'])} v {e(item['away'])}",
        f"<i>{e(item.get('sport', ''))}</i>",
        "",
    ]
    for i, o in enumerate(order, 1):
        leg = arb.legs[o]
        when = "PLACE FIRST" if o == first else "then"
        lines.append(f"<b>{i}. {when}</b> — {e(leg.book)}")
        lines.append(f"    back {e(o)} @ {leg.odds:.2f}")
        lines.append(f"    <b>stake {whole(leg.stake)}</b> → returns {money(leg.returns)}")
    lines += [
        "",
        # Same shape as alert.py's summary line. The two must stay in step, or a
        # restaked reply reads differently from the alert it answers.
        f"Returns {money(arb.worst_return)}–{money(arb.best_return)}, "
        f"money put in {whole(arb.total_stake)}, "
        f"<b>guaranteed {money(arb.worst_profit)} @ "
        f"{arb.realised_margin * 100:.2f}%</b>",
        f"<i>If leg 2 misses you are unhedged on {money(arb.legs[first].stake)}.</i>",
        "",
        "<b>CHECK BEFORE STAKING</b> — these are the alert's prices, not fresh ones:",
    ]
    for o in order:
        leg = arb.legs[o]
        lines.append(f"    {e(leg.book)} — {e(o)} showing <b>{leg.odds:.2f}</b> or better")
    return "\n".join(lines)


def _min_viable(arb, cfg):
    """Smallest total at which the edge survives rounding."""
    inc = cfg.STAKE_INCREMENT
    t = inc * 2
    while t <= 20_000:
        allocate(arb, t, increment=inc)
        if arb.worst_profit > 0:
            return t
        t += inc
    return None


def handle(text: str, cfg) -> str | None:
    """Map an incoming message to a reply, or None to stay silent."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if low in ("/start", "/help", "help"):
        return HELP

    items = _read_json(LAST_FILE, [])
    if low in ("/last", "last"):
        if not items:
            return "No alerts yet. You will get one when a market crosses."
        return restake(items[0], cfg.TOTAL_STAKE, cfg)

    amount = parse_amount(t)
    if amount is None:
        return None                      # not a stake; ignore rather than nag
    if not items:
        return "No alert to restake yet. You will get one when a market crosses."
    return restake(items[0], amount, cfg)


def poll(token: str, cfg, once: bool = False) -> int:
    offset = _read_json(OFFSET_FILE, {}).get("offset", 0)
    allowed = set(str(c) for c in cfg.TELEGRAM_CHAT_IDS)
    print(f"Listening. {len(allowed)} recipient(s). Ctrl-C to stop.")
    while True:
        try:
            r = _call(token, "getUpdates",
                      {"offset": offset, "timeout": POLL_TIMEOUT})
        except RuntimeError as ex:
            # Network blips are expected on a long-lived connection.
            print(f"  getUpdates: {ex}")
            time.sleep(5)
            if once:
                return 1
            continue

        for u in r.get("result", []):
            offset = max(offset, u.get("update_id", 0) + 1)
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            # Only answer the configured recipients. A bot's username is public,
            # so anyone can message it; nobody else gets our betting positions.
            if allowed and chat not in allowed:
                print(f"  ignored message from unknown chat {chat}")
                continue
            reply = handle(msg.get("text", ""), cfg)
            if reply:
                send(token, [chat], reply)
                print(f"  {chat}: {msg.get('text','')!r} -> replied")

        _write_offset(offset)
        if once:
            return 0


def _write_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(json.dumps({"offset": offset}))
    except OSError:
        pass                 # losing the offset only replays messages, harmless


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true",
                   help="drain pending messages and exit (for testing)")
    args = p.parse_args()

    if not config.TELEGRAM_BOT_TOKEN:
        print("No bot token. Put TELEGRAM_BOT_TOKEN in .env beside config.py.")
        return 2
    if not config.TELEGRAM_CHAT_IDS:
        print("No recipients configured. Run: python3 alert.py --chats")
        return 2
    try:
        return poll(config.TELEGRAM_BOT_TOKEN, config, once=args.once)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
