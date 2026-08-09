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
from alert import LAST_FILE, _call, _read_json, _write_json, e, money, send, whole
from arbtool.core import allocate, evaluate, recommend_first_leg
from arbtool.pairs import analyse_game

OFFSET_FILE = Path(__file__).parent / ".bot_offset.json"
POLL_TIMEOUT = 50           # seconds of long-poll; Telegram holds the connection
MIN_STAKE = 10.0
MAX_STAKE = 100_000.0

# A quoted price must be a plausible decimal h2h price. Below 1.01 returns
# effectively nothing and is not a real market; above this is a typo far more
# often than a genuine outsider, and acting on a mistyped 210.0 would size the
# other leg at almost nothing and leave the position unhedged in practice.
MIN_QUOTE = 1.01
MAX_QUOTE = 100.0

HELP = (
    "<b>Arb Desk</b>\n"
    "Send a number to restake the most recent alert, e.g. <code>4000</code>.\n\n"
    "<b>Quote a book we do not carry</b> — read the price off the app and send "
    "it. Works for any bookmaker, Bet365 included:\n"
    "<code>bet365 1.72 2.15</code>  (home price, then away)\n"
    "<code>bet365 Roosters 2.15</code>  (one side only)\n"
    "I check it against every book on that game and tell you whether it "
    "improves the arbitrage.\n\n"
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


def _board(item: dict) -> dict:
    """Every book's prices on the stored game.

    Alerts written before the board was stored only carry the winning pair, so
    fall back to that. Comparing against two books instead of eleven understates
    what is already on offer, which makes a quoted price look better than it is
    — so the reply says which it used."""
    board = {b: dict(p) for b, p in (item.get("board") or {}).items()}
    if not board:
        for leg in item["legs"]:
            board.setdefault(leg["book"], {})[leg["outcome"]] = leg["odds"]
    return board


def _outcomes(item: dict) -> list:
    """Outcome names in reading order — home team first.

    GameAnalysis sorts outcomes alphabetically so the maths is deterministic;
    that is the wrong order to read a match in, and the wrong order to interpret
    two bare numbers typed into a phone."""
    names = {leg["outcome"] for leg in item["legs"]}
    display = [o for o in (item.get("display") or []) if o in names]
    if len(display) == len(names):
        return display
    ordered = [o for o in (item.get("home"), item.get("away")) if o in names]
    return ordered if len(ordered) == len(names) else sorted(names)


def _match_outcome(token: str, outcomes: list):
    """Resolve 'Roosters' to 'Sydney Roosters'. Ambiguity returns None rather
    than guessing — staking the wrong side is not a recoverable error."""
    t = token.strip().lower()
    if not t:
        return None
    hits = [o for o in outcomes if t == o.lower()]
    if not hits:
        hits = [o for o in outcomes if t in o.lower()]
    if not hits:
        hits = [o for o in outcomes if any(w.startswith(t) for w in o.lower().split())]
    return hits[0] if len(hits) == 1 else None


def parse_quote(text: str, item: dict):
    """A price read off a bookmaker's app, for a book we do not carry.

        bet365 1.72 2.15          both sides, home price first
        bet365 Roosters 2.15      one side, named

    Returns (book, {outcome: odds}), or a string to send back explaining why it
    could not be read, or None if this is not a quote at all."""
    parts = text.split()
    if len(parts) < 2:
        return None
    nums, words = [], []
    for p in parts:
        try:
            nums.append(float(p.replace(",", "").lstrip("@$")))
        except ValueError:
            words.append(p)
    if not nums or not words:
        return None

    book = " ".join(words[:-1]) if len(nums) == 1 and len(words) > 1 else " ".join(words)
    name_token = words[-1] if len(nums) == 1 and len(words) > 1 else None
    book = book.strip()
    if not book:
        return None

    for v in nums:
        if not (MIN_QUOTE <= v <= MAX_QUOTE):
            return (f"<b>{v:g}</b> is not a usable decimal price. "
                    f"Send the decimal odds as the app shows them, "
                    f"e.g. <code>bet365 1.72 2.15</code>.")

    outs = _outcomes(item)
    if len(nums) >= 2:
        if len(nums) > 2:
            return ("Too many numbers. Send two prices (home first) or one "
                    "price with the team name.")
        return book, {outs[0]: nums[0], outs[1]: nums[1]}

    if name_token is None:
        return ("Which side is that price for? Send both — "
                f"<code>{e(book)} 1.72 2.15</code> — or name the team, "
                f"<code>{e(book)} {e(outs[1].split()[-1])} {nums[0]:g}</code>.")
    o = _match_outcome(name_token, outs)
    if o is None:
        return (f"I could not tell which side <b>{e(name_token)}</b> is. "
                f"Use <b>{e(outs[0])}</b> or <b>{e(outs[1])}</b>.")
    return book, {o: nums[0]}


def quote(item: dict, book: str, prices: dict, cfg) -> str:
    """Does a price from a book we do not carry improve this game?

    Runs both boards through analyse_game rather than comparing the quoted
    price to the winning leg by hand. The improvement often comes from pairing
    the new price with a book that placed third, which a hand comparison would
    miss entirely."""
    board = _board(item)
    partial = len(board) < 3 and not item.get("board")
    outs = _outcomes(item)

    before = analyse_game(board, "h2h", sorted(board),
                          commission_pct=cfg.COMMISSION_PCT)
    merged = {b: dict(p) for b, p in board.items()}
    merged.setdefault(book, {}).update(prices)
    after = analyse_game(merged, "h2h", sorted(merged),
                         commission_pct=cfg.COMMISSION_PCT)
    if before is None or after is None:
        return ("Could not rebuild that market from the stored prices. "
                "Wait for the next alert.")

    was, now = before.top_pair, after.top_pair
    head = [f"{e(item['home'])} v {e(item['away'])}", ""]
    for o in outs:
        if o in prices:
            best = max((p[o] for p in board.values() if o in p), default=None)
            verdict = ("<b>better</b>" if best is None or prices[o] > best
                       else "not better" if prices[o] < best else "level")
            head.append(f"{e(book)} {prices[o]:.2f} on {e(o)} — {verdict}"
                        + (f", best we have is {best:.2f}" if best else ""))
    head.append("")

    improved = now.arb.margin > was.arb.margin + 1e-9
    uses_it = book in (now.book_a, now.book_b)
    head.append(f"Board was {was.arb.margin * 100:+.2f}% "
                f"({e(was.book_a)} + {e(was.book_b)}), "
                f"now {now.arb.margin * 100:+.2f}% "
                f"({e(now.book_a)} + {e(now.book_b)})")
    if partial:
        head.append("<i>Only the two alerted books were stored for this game, "
                    "so this compares against those, not the full board.</i>")

    if not now.is_arb:
        head.append("")
        head.append("<b>Still no arbitrage.</b>" if not improved else
                    "<b>Better, but still not an arbitrage.</b>")
        return "\n".join(head)
    if not uses_it:
        head.append("")
        head.append("That price does not change the best pairing.")
        return "\n".join(head)

    arb = now.arb
    allocate(arb, cfg.TOTAL_STAKE, increment=cfg.STAKE_INCREMENT)
    if arb.worst_profit <= 0:
        need = _min_viable(arb, cfg)
        head.append("")
        head.append(f"Crosses, but not at {whole(cfg.TOTAL_STAKE)} — whole-dollar "
                    f"rounding leaves {money(arb.worst_profit)}.")
        if need:
            head.append(f"Smallest total that clears: <b>{whole(need)}</b>")
        return "\n".join(head)

    # A quoted price is the user's own reading of an app, so it is the freshest
    # number here — but the book it pairs with is still as old as the alert.
    return "\n".join(head + [""] + _slip(arb, item).split("\n") + [
        "", f"<i>{e(book)} is your reading, not our feed. The other leg is the "
            f"alert's price and may have moved.</i>"])


def _slip(arb, item: dict) -> str:
    """The placeable instruction. Shared so a quote reply and a restake reply
    cannot drift apart in wording or in arithmetic."""
    first = recommend_first_leg(arb)
    order = [first] + [o for o in arb.outcomes if o != first]
    lines = [
        f"<b>{money(arb.worst_profit)} guaranteed</b>  "
        f"({arb.realised_margin * 100:.2f}% on {whole(arb.total_stake)})",
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

    # A quote carries a book name as well as a number, so it can never be
    # mistaken for a bare stake — but check it first regardless, because
    # parse_amount would reject it silently and the user would get no reply.
    if items:
        q = parse_quote(t, items[0])
        if isinstance(q, str):
            return q                     # unreadable quote, say why
        if q is not None:
            book, prices = q
            reply = quote(items[0], book, prices, cfg)
            _remember_quote(items, book, prices, cfg)
            return reply

    amount = parse_amount(t)
    if amount is None:
        return None                      # not a stake; ignore rather than nag
    if not items:
        return "No alert to restake yet. You will get one when a market crosses."
    return restake(items[0], amount, cfg)


def _remember_quote(items: list, book: str, prices: dict, cfg) -> None:
    """Keep a quoted price so a following stake reply restakes what was just
    shown. Without this, sending 'bet365 2.15' and then '4000' would silently
    restake the old pairing and quote a profit the user was not offered."""
    item = items[0]
    board = _board(item)
    board.setdefault(book, {}).update(prices)
    item["board"] = board
    ga = analyse_game(board, "h2h", sorted(board), commission_pct=cfg.COMMISSION_PCT)
    bp = ga.best_pair if ga else None            # None unless it genuinely crosses
    if bp is not None and book in (bp.book_a, bp.book_b):
        item["legs"] = [{"book": bp.arb.legs[o].book, "outcome": o,
                         "odds": bp.arb.legs[o].odds} for o in bp.arb.outcomes]
    try:
        _write_json(LAST_FILE, items)
    except OSError:
        pass                 # a lost quote only means retyping it, not a wrong number


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
