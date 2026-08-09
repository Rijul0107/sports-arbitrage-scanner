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

# The "/" menu in Telegram. Registered with setMyCommands so tapping the slash
# button lists them instead of the user having to remember the wording.
# Descriptions are capped by Telegram at 256 characters and must be plain text.
COMMANDS = [
    {"command": "last",  "description": "Show the most recent alert again"},
    {"command": "stake", "description": "Restake it at a different total — /stake 4000"},
    {"command": "quote", "description": "Check another book's price — /quote bet365 2.15 roosters"},
    {"command": "help",  "description": "What I understand"},
]

HELP = (
    "<b>Arb Desk</b>\n\n"
    "<b>/stake 4000</b> — restake the most recent alert at that total. A bare "
    "number works too.\n\n"
    "<b>/quote bet365 2.15 roosters</b> — a price you have read off another "
    "bookmaker's app. I check it against every book on that game and say "
    "whether it improves the arbitrage. Works for any bookmaker.\n"
    "Say it however you like — all of these read the same:\n"
    "<code>bet365 1.72 2.15</code>  (home price first)\n"
    "<code>bet365 roosters 2.15</code>\n"
    "<code>bet365 has 2.15 on the roosters</code>\n\n"
    "<b>/last</b> — show the most recent alert again\n\n"
    "<i>No new odds are fetched. Replies recompute from the prices in the "
    "alert, so check both books before staking.</i>"
)


def register_commands(token: str) -> bool:
    """Publish the slash menu. Idempotent, so it is safe on every start."""
    try:
        r = _call(token, "setMyCommands", {"commands": json.dumps(COMMANDS)})
        return bool(r.get("ok"))
    except RuntimeError as ex:
        print(f"  could not register the /commands menu: {ex}")
        return False


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


# Words that carry no meaning in a quote. Stripped so "bet365 has 2.15 on the
# roosters" names the book "bet365" and not "bet365 has on the" — a wrong
# bookmaker name printed on a betting slip is a wrong instruction.
FILLER = {"has", "have", "had", "is", "are", "was", "on", "at", "the", "for",
          "and", "of", "to", "in", "it", "its", "showing", "shows", "paying",
          "pays", "price", "prices", "odds", "says", "say", "got", "now",
          "currently", "still", "a", "an", "with", "quote", "quoting"}


def _match_outcome(token: str, outcomes: list):
    """Resolve 'Roosters' to 'Sydney Roosters'. Ambiguity returns None rather
    than guessing — staking the wrong side is not a recoverable error.

    Matching is word-aware, never a bare substring of the whole name: "the" is
    inside "Panthers", so a substring test silently tied prices to the wrong
    team. Filler words are refused outright for the same reason."""
    t = token.strip().lower()
    if len(t) < 3 or t in FILLER:
        return None                      # too short or too common to be a team
    hits = [o for o in outcomes if t == o.lower()]
    if not hits:
        hits = [o for o in outcomes
                if any(w == t or w.startswith(t) or t.startswith(w)
                       for w in o.lower().split() if len(w) >= 3)]
    return hits[0] if len(hits) == 1 else None


def parse_quote(text: str, item: dict):
    """A price read off a bookmaker's app, for a book we do not carry.

        bet365 1.72 2.15                 both sides, home price first
        bet365 Roosters 2.15             one side, named
        bet365 has 2.15 on the roosters  same thing, said naturally
        bet365 panthers 1.72 roosters 2.15

    Order does not matter and filler words are ignored, but the reading is
    strictly positional where sides are named: the nth price belongs to the nth
    team mentioned. Nothing is inferred beyond that. If which side a price
    belongs to cannot be established, the reply asks rather than picks — the
    flattering guess would manufacture an arbitrage that does not exist.

    Returns (book, {outcome: odds}), a string explaining why it could not be
    read, or None if this is not a quote at all."""
    outs = _outcomes(item)
    raw = [t.strip("@$()").rstrip(".,:;") for t in text.replace(",", "").split()]
    raw = [t for t in raw if t]
    if len(raw) < 2:
        return None

    # Classify first, decide second. A number outside plausible odds is only a
    # typo worth complaining about when no real price is present; otherwise it
    # is part of a name, which is how "bet 365 roosters 2.15" reads correctly.
    kinds = []
    for t in raw:
        try:
            v = float(t)
        except ValueError:
            kinds.append(("word", t))
            continue
        kinds.append(("price" if MIN_QUOTE <= v <= MAX_QUOTE else "offscale", v))

    prices = [v for k, v in kinds if k == "price"]
    offscale = [v for k, v in kinds if k == "offscale"]
    if not prices:
        if offscale and any(k == "word" for k, _ in kinds):
            return (f"<b>{offscale[0]:g}</b> is not a usable decimal price. "
                    f"Send the decimal odds as the app shows them, "
                    f"e.g. <code>bet365 1.72 2.15</code>.")
        return None
    if len(prices) > 2:
        return ("Too many prices. Send two (home first), or one with the team "
                "named.")

    # Walk in order so a price can be tied to the team beside it.
    named, book_bits, seq = [], [], []
    for kind, val in kinds:
        if kind == "price":
            seq.append(("price", val))
            continue
        if kind == "offscale":
            # A stray number inside a name: "bet 365". Glue it to what precedes.
            if book_bits:
                book_bits[-1] += f"{val:g}"
            else:
                book_bits.append(f"{val:g}")
            continue
        o = _match_outcome(val, outs)
        if o is not None:
            # A second word of a name already matched ("Sydney" then "Roosters")
            # is part of that team, not part of the bookmaker's name.
            if o not in named:
                named.append(o)
                seq.append(("team", o))
        elif val.lower() not in FILLER:
            book_bits.append(val)

    book = " ".join(book_bits).strip()
    if not book:
        return ("Which bookmaker is that? Send the name with the price, "
                "e.g. <code>bet365 1.72 2.15</code>.")

    if named and len(named) == len(prices):
        # Positional: nth price belongs to nth team named, whichever came first.
        order = [v for k, v in seq if k == "team"]
        vals = [v for k, v in seq if k == "price"]
        return book, dict(zip(order, vals))
    if not named and len(prices) == 2:
        return book, {outs[0]: prices[0], outs[1]: prices[1]}
    if len(prices) == 1 and len(named) == 1:
        return book, {named[0]: prices[0]}
    if len(prices) == 1:
        # Name both sides. The user may have typed a team name I failed to
        # recognise, and seeing the exact names is what fixes that.
        return (f"Which side is <b>{prices[0]:g}</b> for — "
                f"<b>{e(outs[0])}</b> or <b>{e(outs[1])}</b>?\n"
                f"<code>{e(book)} {e(outs[1].split()[-1])} {prices[0]:g}</code>, "
                f"or send both prices with {e(outs[0])} first.")
    return (f"I matched {len(named)} team(s) to {len(prices)} prices. "
            f"Send <code>{e(book)} 1.72 2.15</code> with "
            f"{e(outs[0])} first.")


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
    # Telegram appends @botname when a command is tapped in a group.
    cmd, _, rest = t.partition(" ")
    cmd = cmd.lower().split("@")[0]
    if cmd.startswith("/"):
        t, low = rest.strip(), rest.strip().lower()
    else:
        cmd, low = "", t.lower()

    if cmd in ("/start", "/help") or low == "help":
        return HELP

    items = _read_json(LAST_FILE, [])
    if cmd == "/last" or low == "last":
        if not items:
            return "No alerts yet. You will get one when a market crosses."
        return restake(items[0], cfg.TOTAL_STAKE, cfg)

    # Tapping /quote or /stake with nothing after it should explain itself
    # rather than sit silent — the menu entry is the whole point of the prompt.
    if cmd in ("/quote", "/stake") and not t:
        return HELP
    if cmd == "/quote" and not items:
        return "No alert to compare against yet. You will get one when a market crosses."

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
        # Silence for stray chatter, but never for an explicit command — the
        # user tapped it and is owed an answer.
        return (f"I could not read <b>{e(t)}</b> as a stake. Send a number, "
                f"e.g. <code>/stake 4000</code>.") if cmd == "/stake" else None
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
    register_commands(token)
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
    p.add_argument("--commands", action="store_true",
                   help="publish the / menu to Telegram and exit")
    args = p.parse_args()

    if not config.TELEGRAM_BOT_TOKEN:
        print("No bot token. Put TELEGRAM_BOT_TOKEN in .env beside config.py.")
        return 2
    if args.commands:
        ok = register_commands(config.TELEGRAM_BOT_TOKEN)
        print("Menu published." if ok else "Could not publish the menu.")
        return 0 if ok else 1
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
