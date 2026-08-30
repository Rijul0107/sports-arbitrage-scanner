# Operating notes

Day-to-day running detail: price freshness, the verification step before
trusting a number, Telegram setup, and the deployment layout. The project
overview and the analysis of what it found are in [README.md](README.md).

---

Finds cross-book arbitrage across Australian bookmakers and tells you exactly
which two books to bet with and how much on each.

Covers **NRL and tennis** — both two-outcome sports, so an arbitrage needs only
two opposing prices and there is no draw leg to cover.

---

## Try it right now, without an API key

```bash
cd arb-tool
python3 watch.py --demo      # terminal view, fabricated prices
python3 serve.py --demo      # dashboard in your browser
```

Nothing is fetched and no credits are spent. This is the fastest way to see
what the tool does before deciding whether to connect anything.

## Going live

```bash
export ODDS_API_KEY="your_key"        # free key at the-odds-api.com
python3 verify_live.py                # FIRST — see below
python3 serve.py                      # dashboard, refreshes every 60s
python3 watch.py                      # terminal, alerts on a find
```

---

## Run `verify_live.py` before you trust anything

"Live odds API" is doing a lot of work in that phrase. The endpoint returns the
most recent price the API *captured* from each bookmaker — not the price on the
bookmaker's screen this instant. The Odds API refreshes pre-match head-to-head
markets roughly every 60 seconds, so your view can be a minute or two behind
what your phone shows.

`verify_live.py` makes one real call, prints every book's price with the exact
capture timestamp and age, and asks you to compare against the bookmaker apps
line by line. It reports the median quote age and tells you plainly if the lag
makes arbitrage unrealistic.

Do this before you stake anything. Don't take it on trust — including from
whoever wrote this tool.

---

## The pairwise matrix

The thing this tool does that a simple arbitrage checker does not: it evaluates
**every pairing of your books**, not just whether an arbitrage exists somewhere.

```
  PAIRWISE MARGIN
               Sportsbet  Ladbrokes        TAB       Neds
  Sportsbet            —     +0.75%     +2.38%   [+4.99%]
  Ladbrokes       +0.75%          —     -1.76%     +0.65%
  TAB             +2.38%     -1.76%          —     -2.09%
  Neds          [+4.99%]     +0.65%     -2.09%          —
```

Same game, same moment. Sportsbet + Neds pays 4.99%; Sportsbet + Ladbrokes pays
0.75%. Nearly seven times the difference depending on which two accounts you
use. TAB + Neds is *negative* even though Neds holds the best Storm price,
because TAB's Penrith price is too short to cover it.

The count matters as much as the best figure. Four working pairings means that
if Sportsbet shortens while you are typing, you drop to Sportsbet + TAB at
2.38% and still profit. One working pairing means the moment either price moves
it is gone. The tool shows both.

Four books gives six pairings; five gives ten.

---

## What you get when it finds one

Dollars first, percentages second. On a $1,500 stake:

```
  $74.56 GUARANTEED   4.99% on $1,500.00

  LEG 1  PLACE FIRST (longest price — most likely to move)
    Sportsbet   back Penrith Panthers @ 2.12
    STAKE $743.00   returns $1,575.16
  LEG 2  then
    Neds        back Melbourne Storm @ 2.08
    STAKE $757.00   returns $1,574.56

  Outlay $1,500.00   Profit $74.56 (4.97%)
  If leg 2 misses you are unhedged on $743.00 — that, not the outlay, is the risk.
```

Stakes are whole dollars because that is what you can type into a betslip, and
the profit shown is the worst case **after** rounding — not the headline
margin, which rounding often erodes. On a thin edge the tool refuses to print a
slip at all and tells you the minimum stake that would work.

---

## Things worth knowing before you place anything

**Online in-play betting is illegal in Australia.** Under the Interactive
Gambling Act operators may only accept in-play bets by telephone or in person.
The tool refuses to surface anything already under way. Everything you can act
on is pre-match. This is also why the 60-second feed is defensible: pre-match
edges persist for minutes, where in-play edges last seconds.

**Your exposure is one leg, not the stake.** The way people lose money at this
is placing leg one, finding leg two has moved, and holding an unhedged bet. On
$1,500 that is roughly $750 at risk. Place the longer price first — it moves
furthest and is the one about to be corrected — and if the second leg has gone,
decide immediately whether to take the worse price or wear the exposure.

**Tennis has a retirement trap.** Books differ on what happens when a player
retires mid-match: some void, some pay out on whoever advances, some settle
after one set. If your two books have different rules, a retirement turns a
hedged position into a one-sided bet. Check both books' tennis rules, or stick
to NRL.

**Ladbrokes and Neds are both Entain-owned** and their prices track each other
closely, so that pairing rarely disagrees. Swapping one for Betfair Exchange or
PointsBet would widen your matrix.

**Books restrict winning accounts.** That, rather than the maths, is what makes
arbitrage hard at scale.

---

## Credit budget

The free tier gives 500 credits a month. One poll costs 1 credit per sport in
season, so NRL plus tennis costs 2.

| Pattern | Cost | |
|---|---|---|
| Dashboard open 2h, 60s refresh | ~240 | half your month |
| Terminal watch, 2h before a fixture | ~120 | four sessions a month |
| Continuous, all month | ~86,000 | needs a paid plan |

`SESSION_CREDIT_BUDGET` in `config.py` caps a single run, and the server serves
its last snapshot rather than burning through your month. `/sports` and
`/events` are free, so checking what is on costs nothing.

The productive window is the couple of hours before a fixture: books are
actively repricing and disagreement between them is widest. Tennis gives more
simultaneous markets than NRL and is generally the better hunting ground.

If you outgrow the free tier, RapidOddsAPI's $49/month tier gives 200,000
credits with deeper AU coverage than The Odds API's $59/100,000 — worth
comparing before you upgrade in place.

---

## Configuration

Everything lives in `config.py`:

`BOOKS` is the set compared — must match the API's names exactly. `TOTAL_STAKE`
is the amount split across both legs. `MIN_PROFIT` hides anything paying less
than it, so a $2 edge does not clutter the screen. `PRE_MATCH_ONLY` and
`MAX_DATA_AGE_SECONDS` are the safety rails; leave them on.

---

## Tests

```bash
python3 tests/test_core.py      # 26 — the arbitrage maths
python3 tests/test_pairs.py     # 16 — the pairwise matrix
python3 tests/test_server.py    #  6 — server and payload shape
python3 verify.py               # independent verification, see below
```

`verify.py` is not a unit test suite — it re-derives the results a different
way, so a shared bug is less likely to hide. It checks headline numbers by hand
in exact fractions, brute-forces every possible whole-dollar split to confirm
the staking is optimal, and cross-checks arbitrage detection against an
exhaustive assignment search over 2,000 random markets.

---

## Files

| | |
|---|---|
| `serve.py` | dashboard + API proxy |
| `watch.py` | terminal watcher |
| `verify_live.py` | prove the feed is live and accurate |
| `verify.py` | independent verification of the maths |
| `config.py` | all settings |
| `arbtool/core.py` | arbitrage maths and staking |
| `arbtool/pairs.py` | pairwise matrix |
| `arbtool/scan.py` | API responses to ranked opportunities |
| `arbtool/api.py` | API client, credit accounting, freshness |
| `static/dashboard.html` | the UI |

The dashboard recomputes every margin from the prices it is sent, rather than
trusting figures from the server. The JavaScript and Python engines were
verified to return identical results, so the browser and the terminal cannot
disagree.

---

Not financial advice. Odds move, books limit and void bets, and a hedge that
only half fills is just a bet.
