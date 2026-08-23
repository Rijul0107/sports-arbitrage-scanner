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

# Only these are compared. Pairings grow as n(n-1)/2, so six books gives
# fifteen and eleven gives fifty-five. Adding a book costs no credits — The
# Odds API bills per sport key, and books are a filter on a response already
# paid for, so a book left out of this list is a price we paid for and threw
# away.
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
# the rest. Its commission is set per book in BOOK_COMMISSION_PCT below.
#
# Every title below was read out of a real /odds response on 2026-08-09, not
# guessed. Two of them are why that matters: the API returns "SportsBet" with a
# capital B, and the exchange is "Betfair", not "Betfair Exchange". The previous
# default here said "Sportsbet", which never matched anything — the largest book
# in the country was being dropped in silence on every scan.
BOOKS = [
    # Cut from twelve to seven on 2026-08-16, by the owner, mid book-cull
    # study: TAB, Neds, PointsBet (AU), Betfair and TABtouch are accounts the
    # owner has ruled out keeping, so scanning their prices only finds
    # arbitrages that cannot be placed. Neds was priced identically to
    # Ladbrokes on 99.9% of 22,200 shared outcomes in boards.db — one Entain
    # desk, and Ladbrokes keeps the slot. TABtouch matched Unibet on 62.8% of
    # 32,914 (shared feed on many markets); Unibet keeps that slot. Full
    # twelve-book prices remain in data/boards.db up to 2026-08-16 for
    # study.py replay. To re-add a book the title must match the API exactly:
    # "TAB", "Neds", "PointsBet (AU)", "Betfair", "TABtouch".
    #
    # Coverage percentages measured 2026-08-09 over 380 games: share of games
    # the book quoted at all. Betr outcovers SportsBet, which was a surprise,
    # and Dabble is thin but appears on tennis specifically.
    "SportsBet",
    "Ladbrokes",
    "PlayUp",
    "Betr",         # 32.4% — widest coverage of any AU book in the sample
    "Unibet",       # 20.0%
    "Bet Right",    # 12.9%
    "Dabble AU",    #  7.1% — tennis mostly
]

# Bet365 is absent because it is not obtainable, not because it was not wanted.
# The Odds API documents `bet365_au` as paid-plans-only, h2h/spreads/totals,
# AFL and NRL. Requested by name on 2026-08-09 with the paid key: 8 NRL games
# and 7 AFL games returned, zero Bet365 prices on any of them.

# Seven books gives twenty-one pairings, and after the 2026-08-16 cut each is
# an independent price source: the boards.db identical-price analysis put every
# remaining pair between 10% and 32% price agreement, against 99.9% for the
# Neds/Ladbrokes twins that motivated the check. One earlier belief did not
# survive that analysis: TAB and TABtouch were assumed related like the Entain
# pair, but matched on only 12.3% of shared outcomes.
#
# An earlier version of this file dropped Betr, Unibet, TABtouch and Bet Right
# on the grounds that they "quote only once a game is under way". That was
# measured on too small a sample and is wrong. Re-measured 2026-08-09 across 379
# pre-match games: Betr 123, Unibet 75, TABtouch 58, Bet Right 49 — Betr covers
# more pre-match fixtures than SportsBet does. Four books were being discarded
# for a reason that did not hold.
#
# Coverage percentages look low across the board because the sample spans US
# sports where AU books quote sparsely. Within NRL, AFL and tennis most of these
# books quote nearly every fixture.
#
# Still deliberately left out:
#   Bet365 AU  — not obtainable. See the note above BOOKS: requested by name on
#                the paid plan, zero prices returned on the only two sports it
#                is documented to cover.
#
# Betfair left the list on 2026-08-16 with the other ruled-out accounts. If it
# ever returns, two things still hold: its commission entry in
# BOOK_COMMISSION_PCT below is load-bearing (without it every Betfair position
# is overstated), and it once quoted 1.02/1.02 on a thin live NRL market —
# evaluate() only rejects prices at or below 1.00, so a garbage high side on an
# illiquid exchange market can manufacture an arb that cannot be placed.

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

# Total across both legs of a single arbitrage. Matches the dashboard's own
# default stake input, so the alert and the browser quote the same position
# for the same board — they disagreed while this said 2000.
# 1500 -> 1000 on 2026-08-23 at the owner's request: every alert quotes a
# $1000 position. Rounding loss grows as the stake falls (CLAUDE.md §8), so
# the realised margin sits slightly further below the theoretical one than it
# did at 1500; MIN_MARGIN_PCT gates on the realised figure, so the alerts stay
# honest, there are simply marginally fewer of them.
TOTAL_STAKE = 1000.00

# Smallest stake a book will accept. Whole dollars keeps the numbers quick to
# type under time pressure and avoids fat-finger errors.
STAKE_INCREMENT = 1.00

# Ignore anything paying less than this after rounding. A $2 edge is not worth
# the exposure of an unfilled second leg.
MIN_PROFIT = 10.00

# Log every board every scan looks at to data/boards.db, crossing or not.
#
# On for a fortnight from 2026-08-12 to decide which bookmakers keep their
# accounts funded — twelve accounts at TOTAL_STAKE each is a lot of money
# asleep, and study.py replays the stored boards through every 6-book subset to
# find which six lose the least. Costs no API credits; it writes prices already
# fetched. Turn off once the cut is made, or the file grows without a reader.
RECORD_BOARDS = True

# Ignore anything thinner than this regardless of stake size. Measured on the
# realised margin — the post-rounding figure every surface prints — so the
# percentage in an alert is never below the number set here. Gating on the
# theoretical margin instead would let a 1.0% board through and then print
# 0.94%, which reads as the threshold being broken.
# Raised 1.0 -> 2.0 on 2026-08-16 at the owner's request: alert only on
# margins above 2%.
MIN_MARGIN_PCT = 2.0

# Commission charged on winnings, per bookmaker. Books absent here charge
# nothing, which is every traditional Australian corporate.
#
# This is a mapping rather than one number because a single global rate is
# wrong whichever value it takes. Zero overstates a Betfair leg's winnings and
# turns a settled loss into an apparent arbitrage; Betfair's rate applied to
# everything penalises corporate legs by a fee they never charge and hides
# almost every real edge.
#
# The rate also decides which book wins an outcome, not just what it pays:
# Betfair at 2.20 less commission pays 2.14, so a corporate at 2.16 is the
# better leg despite the shorter headline price. core.best_odds_per_outcome
# ranks on the net figure for exactly this reason.
#
# CONFIRM YOUR OWN RATE before staking on a Betfair leg. Commission is charged
# on net market profit and the rate varies by market and by account — discount
# rates and market base rates both move it. 5% is Betfair Australia's standard
# starting point, not a promise about your account.
BOOK_COMMISSION_PCT = {
    "Betfair": 5.0,
}

# Applied to every book that has no entry above. Left at zero deliberately:
# a blanket rate is the mistake BOOK_COMMISSION_PCT exists to avoid.
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
#   icehockey_nhl    5/31 usable on h2h                     ADDED 2026-08-10, but
#                   for totals only — see SPORT_MARKETS. h2h is as poor as that
#                   count suggests and gets poorer, because a third of the AU
#                   h2h quotes carry a Draw leg (regulation-time pricing) which
#                   voids the whole market for that event. Totals reached 12/31
#                   with four books and produced the only crossing measured
#                   anywhere on 2026-08-10: Unibet Over 6.5 @ 2.14 against
#                   PointsBet Under 6.5 @ 1.90, +0.64%. The cause is structural,
#                   not a stale quote — all four books had updated inside 60
#                   seconds, but TABtouch and Unibet run a 7.2% overround on a
#                   sport no AU desk prioritises while SportsBet runs 2.9%.
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
    "icehockey_nhl",           # totals only — see SPORT_MARKETS
]

# ---------------------------------------------------------------------------
# Markets, per sport
# ---------------------------------------------------------------------------

# Which markets to request for which sport. Per sport rather than one global
# list because cost is [markets] x [regions] on every poll, and a market is
# only worth its credit where AU books actually quote it. Measured 2026-08-10
# over one full poll of the AU region:
#
#   NRL      spreads  9 books on 8 of 8 games   totals 4 books on 8 of 8
#   AFL      spreads  8 books on 9 of 9 games   totals 3 books on 3 of 9
#   NHL      totals   4 books on 12 of 31       spreads only 2 books
#   MLB      spreads  6 books on 10 of 10       totals 5 books on 10 of 10
#   WNBA     spreads  4 books on 5 of 5         totals 4 books on 5 of 5
#   NFL pre  spreads/totals 4 books             — not added, HALF the spread
#                                                 lines are whole numbers
#   MMA      spreads none, totals 1 book        — h2h only, nothing to add
#
# MLB and WNBA were added 2026-08-10 to spend the headroom left by the */30
# schedule, taking a poll from 15 credits to 19 and the month from 72% to 91%
# of plan. MLB is the better of the two: six books on every game, and its
# totals reached -1.27%, the closest any market came to crossing in the survey.
# Note ~22% of MLB lines are whole numbers, so ALLOW_PUSH_LINES silently
# discards a fifth of that board — that is the intended trade, not a fault.
#
# NRL and AFL quoted zero whole-number lines in that sample, so ALLOW_PUSH_LINES
# costs nothing there and protects the US sports if they are ever added.
#
# Honest caveat: spreads and totals are NOT softer than head-to-head. The best
# margin measured across ~200 line-markets was -1.27%, against a head-to-head
# baseline that had already reached -1.36%. What they buy is more markets on
# fixtures already paid for — 17 AFL spread-markets across 9 games where h2h
# offers 9 — not better prices per market.
SPORT_MARKETS = {
    "rugbyleague_nrl": "h2h,spreads,totals",
    "aussierules_afl": "h2h,spreads,totals",
    "baseball_mlb": "h2h,spreads,totals",
    "basketball_wnba": "h2h,spreads,totals",
    "icehockey_nhl": "totals",
}

# Every sport not named above. Changing this multiplies the whole bill.
DEFAULT_MARKETS = "h2h"

# ---------------------------------------------------------------------------
# Scan window
# ---------------------------------------------------------------------------

# Hours the scheduled scan runs, Australia/Sydney. (hour, minute), start
# inclusive, end exclusive. The scheduler is deliberately allowed to fire more
# often than this — alert.py trims to the window itself using zoneinfo, so
# daylight saving is handled by the timezone database rather than by someone
# remembering to edit a crontab in October and again in April. An out-of-window
# run exits before any request and costs nothing.
#
# 06:40 to 22:40 Sydney. The end moved in from 23:00 on 2026-08-10; at a 30
# minute interval that changes nothing, because no slot falls between 22:30 and
# 23:00 either way. It matters only if the interval is ever tightened.
SCAN_WINDOW_START = (6, 40)
SCAN_WINDOW_END = (22, 40)

# The scheduler's interval in minutes. Not read by alert.py — cron owns the
# real schedule — but books.py prices a day's polling from it, and a number
# here that disagrees with deploy/crontab.txt makes that estimate a lie.
CRON_MINUTES = 30

# Whole-number lines (Over 41, not Over 41.5) refund both stakes when the
# result lands exactly there. That is not a loss, but it is not the guaranteed
# profit the slip promises either, and the real danger is settlement drift:
# books differ on whether a push voids one leg or both, and a leg voided at one
# book while the other stands leaves a naked bet. Off by default. Turning it on
# means accepting that `worst_profit` overstates what some results pay.
ALLOW_PUSH_LINES = False

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
