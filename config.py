"""
Settings for Arb Desk. Edit this file, not the scripts.
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Read KEY=value lines from .env into os.environ.

    A real environment variable always wins: exporting a key in the shell must
    override a stale value left in .env, or you would poll with the wrong key
    and not know why. Values are read literally apart from one layer of
    surrounding quotes — an API key is an opaque string and must not be
    reinterpreted.

    Kept deliberately tiny: the repo has no third-party dependencies and this
    is not worth breaking that for.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return                                  # absent or unreadable: not an error
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:        # shell export wins
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

# Paste your key here, or leave empty and set ODDS_API_KEY in your shell
# (safer — the key never enters the repo). A .env file beside this one is read
# too; see .env.example.
API_KEY = os.environ.get("ODDS_API_KEY", "")

REGIONS = "au"

# ---------------------------------------------------------------------------
# The books you hold accounts with
# ---------------------------------------------------------------------------

# Only these are compared. Pairings grow as n(n-1)/2, so four books gives six
# and six books gives fifteen. Adding a book costs no credits — The Odds API
# bills per sport key, and books are a filter on a response already paid for.
#
# Names must match what the API returns exactly; scan.py silently drops any it
# does not recognise, which looks identical to "no arbitrage today". Run
# `python3 books.py --list` with a key set to print the real titles.
#
# Chosen for independent price formation rather than count. Ladbrokes and Neds
# are both Entain-owned and their prices track each other, so only one is worth
# a slot. Betfair is an exchange: its prices come from users laying against each
# other rather than a trading desk, so it disagrees with the corporates
# structurally, and The Odds API refreshes exchanges every 20s against 60s for
# the rest. If you add Betfair, set COMMISSION_PCT below.
#
# Every title below was read out of a real /odds response on 2026-08-09, not
# guessed. Two of them are why that matters: the API returns "SportsBet" with a
# capital B, and the exchange is "Betfair", not "Betfair Exchange". The previous
# default here said "Sportsbet", which never matched anything — the largest book
# in the country was being dropped in silence on every scan.
BOOKS = [
    "SportsBet",
    "TAB",
    "Ladbrokes",
    "Neds",
    "PointsBet (AU)",
    "PlayUp",
]

# Six books gives fifteen pairings. Ladbrokes and Neds are one Entain trading
# desk, so treat that as five independent price sources rather than six. On a
# 23-game board scanned 2026-08-09 they returned identical prices, identical
# coverage (the same 21 of 23 games) and Neds appeared in zero best pairings —
# it is kept for coverage, not for disagreement.
#
# Adding a book costs no credits: The Odds API bills per sport key, and books
# are a filter on a response already paid for. There is no reason to run a short
# list except that a book must actually quote when you are looking.
#
# Deliberately left out, and why:
#   Bet365 AU  — PAID TIER ONLY. Add it when the subscription starts, but expect
#                little: coverage is h2h/spreads/totals for AFL and NRL only, so
#                no tennis, and on the board above it would have reached 6 of 23
#                games. Every best pairing there already ran through SportsBet.
#   Dabble AU  — paid tier only, coverage unknown.
#   Betfair    — quoted 1.02/1.02 on a live NRL market, which is not a real
#                price. Thin exchange markets report garbage, and evaluate()
#                only rejects prices at or below 1.00, so a stale high side
#                could manufacture an arb that cannot be placed. Revisit with
#                COMMISSION_PCT set once the feed has been watched for a while.
#   Unibet,    — observed quoting only once a game was already under way, never
#   TABtouch,    on a pre-match fixture days ahead. Useless for how this is used.
#   Betr,
#   Bet Right

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

# Total across both legs of a single arbitrage.
TOTAL_STAKE = 2000.00

# Smallest stake a book will accept. Whole dollars keeps the numbers quick to
# type under time pressure and avoids fat-finger errors.
STAKE_INCREMENT = 1.00

# Ignore anything paying less than this after rounding. A $2 edge is not worth
# the exposure of an unfilled second leg.
MIN_PROFIT = 10.00

# Ignore anything thinner than this regardless of stake size.
MIN_MARGIN_PCT = 0.5

# Per-leg commission on winnings. Betfair charges it; traditional books do not.
COMMISSION_PCT = 0.0

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

# Never surface a market already under way. Online in-play betting is
# prohibited in Australia under the Interactive Gambling Act — operators may
# only accept in-play bets by telephone or in a retail venue. Leave True.
PRE_MATCH_ONLY = True

# Skip anything starting sooner than this: you need time to place both legs,
# and books suspend markets in the minutes before the jump.
MIN_MINUTES_TO_START = 5

# Reject quotes older than this. The vendor documents a 60s refresh for
# pre-match featured markets (20s for exchanges), so a quote past ~2 refresh
# cycles usually means the market suspended or a scrape failed, not that the
# price still stands. 180 accepted three missed cycles, which was too generous.
MAX_DATA_AGE_SECONDS = 120

# Sports to watch. Two-outcome only: with no draw leg to cover, two opposing
# prices are enough for an arbitrage. Rugby *union* is excluded deliberately —
# it can draw, so evaluate() rejects it anyway and polling it only burns credits.
#
# Named keys for competitions that sit still. One credit per key per poll, so
# every entry here has a monthly cost; keep it to what you would actually bet.
# Measured against the six books on 2026-08-09, counting games where at least
# two of them quoted — fewer than two produces no pairing at all. Coverage, not
# credits, is what decides this list: an empty response is not charged, and a
# poorly covered sport costs a credit to return games you can never hedge.
#
#   baseball_mlb    14/14 games usable, all six books      KEPT
#   nfl_preseason   11/16 usable, four books               KEPT
#   nfl             18/272 usable now, six books           KEPT — the ratio is
#                   low only because the whole season is listed months ahead;
#                   near-term games are well covered and it improves in September
#   mma             11/40 usable, five books               KEPT — undercards are
#                   sparse but UFC main cards are a real AU market
#   ncaaf           41/126 usable but only THREE books      dropped — three books
#                   is three pairings, and it is US college
#   icehockey_nhl    5/31 usable                            dropped
#   americanfootball_cfl  0/4 usable                        dropped
#   lacrosse_pll     0/3, none of my books                  dropped
#   baseball_npb    excluded on rules, not coverage: a 12-inning cap makes draws
#                   routine and books offer a Draw bet, so it is a 3-way market
SPORT_KEYS = [
    "basketball_nbl",          # Australian NBL
    "basketball_nba",          # men's
    "basketball_wnba",         # women's
    "baseball_mlb",            # best-covered sport tested: six books, every game
    "americanfootball_nfl",
    "americanfootball_nfl_preseason",
    "mma_mixed_martial_arts",
]

# Prefixes for whole competition families where every key is two-outcome, so
# new ones can be picked up without editing this file.
#
#   tennis_        46 keys, one per tournament, ATP and WTA both. Which are live
#                  changes weekly, so naming them would be stale within days.
#   rugbyleague_   only ever NRL, NRLW and State of Origin. All two-way, all
#                  worth polling, and a prefix means a new one is not missed.
#                  Note this does NOT catch rugbyunion_, which can draw.
#   aussierules_   AFL and AFLW. The deepest markets any Australian book prices,
#                  which is where disagreement between them is most likely.
#
# AFL and NRL can both be drawn, and AU books handle that by voiding a 2-way h2h
# rather than listing a third outcome — so expect_outcomes=2 still holds. The
# residual risk is books differing on settlement: if one voids a drawn game and
# the other pays out, the hedge becomes a one-sided bet. Rare, real, unmodelled.
#
# Basketball deliberately stays named above: a "basketball_" prefix would drag
# in NCAAB and WNCAAB, hundreds of US college games that AU books barely price,
# at a credit each per poll.
SPORT_PREFIXES = ("tennis_", "rugbyleague_", "aussierules_")

# ---------------------------------------------------------------------------
# Polling and credits
# ---------------------------------------------------------------------------

# Free tier is 500 credits/month; the $30 tier is 20,000. One poll costs 1
# credit per active sport key, so run `python3 books.py --sports` (free) to see
# what a poll costs you this week before choosing an interval.
SESSION_CREDIT_BUDGET = 120
MIN_PLAN_CREDITS = 10

# 10 minutes, not 60 seconds. The vendor refreshes pre-match h2h every 60s, so a
# faster poll cannot see anything newer — and scanning days out from a fixture,
# which is how this tool is used, prices barely move between polls. 60s costs
# ten times as much for the same information. Tighten it on match day if you
# start working the final hours, where books reprice and edges are short-lived.
POLL_SECONDS = 600

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Telegram alerts (alert.py)
# ---------------------------------------------------------------------------

# Token from @BotFather. Keep it out of this file — put it in .env beside
# config.py, which is gitignored. A bot token lets anyone post as your bot.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Who gets the alerts. A bot cannot start a conversation, so each recipient must
# message the bot once first; then `python3 alert.py --chats` prints their IDs.
# Not secret, so they can live here. Comma-separated in TELEGRAM_CHAT_IDS
# overrides, which is handy on a machine running cron for someone else.
TELEGRAM_CHAT_IDS = [c.strip() for c in
                     os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()] or [
    # "123456789",
    # "987654321",
]

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8787
