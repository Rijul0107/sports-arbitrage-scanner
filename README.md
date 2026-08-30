# Arb Desk

Finds cross-bookmaker arbitrage on Australian sports markets, logs every price
it sees, and backtests what a bankroll would have done. Standard library only —
no dependencies, no framework, no build step.

[![CI](https://github.com/Rijul0107/sports-arbitrage-scanner/actions/workflows/ci.yml/badge.svg)](https://github.com/Rijul0107/sports-arbitrage-scanner/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-214%20passing-brightgreen)
![Dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)

**[Live dashboard →](https://rijul0107.github.io/sports-arbitrage-scanner/)** — real recorded
odds, no setup, no API key.

An arbitrage exists when two bookmakers disagree enough that backing both sides
returns a profit whichever way the fixture goes. The edge is small, brief, and
mostly absent: across **115,583 recorded odds boards, 1.34% ever crossed**, and
the median board sat **3.6% the wrong side of break-even** — that gap is the
bookmakers' margin, measured rather than assumed.

---

## What it actually found

The scanner ran live against The Odds API from **12–29 August 2026**, polling
every 30 minutes and recording the complete odds board on every scan — not only
the boards that crossed. That distinction is what makes the analysis below
possible, and it cannot be reconstructed after the fact.

| | |
|---|---|
| Odds boards recorded | **115,583** |
| Fixtures, across 18 sports | 984 |
| Scans / API credits spent | 550 / 11,423 |
| Boards where a market crossed | 1,553 (**1.34%**) |
| Cleared the profit and margin gates | 26 |
| Median margin, all boards | **−3.60%** |
| Median margin when crossed | 0.21% |

### Simulated bankroll

> **No bets were placed.** Every figure below is a replay of recorded odds.
> `backtest.py` prints its six assumptions on every run and the JSON output
> carries `"bets_placed": false`.

Starting with **$5,000** over those 17.7 days:

| Position size | Final | Return | Opportunities taken |
|---|---|---|---|
| 100% of bankroll | **$6,006.55** | **+20.13%** | 8 of 26 |
| 50% | $5,743.98 | +14.88% | 12 of 26 |
| 25% | $5,587.28 | +11.75% | 16 of 26 |
| 10% | $5,421.73 | +8.43% | 26 of 26 |

**The return is a range, not a number: +8.4% to +20.1%, decided entirely by
position sizing.** Larger positions capture more of each margin but lock the
bankroll until settlement, so the next opportunity inside that window is missed.
Twenty-six opportunities is far too few to identify an optimum — the tool says
so in its own output rather than quoting the best cell.

```bash
arb backtest --bankroll 5000 --sweep
```

### The finding worth the two weeks

Taking **every** arbitrage is worse than taking the **filtered** ones:

| Strategy | Opportunities | Gross | Peak capital | Return on capital |
|---|---|---|---|---|
| Every crossing under 10% | 99 | $919 | $26,000 | **3.53%** |
| Cleared the gates | 26 | $843 | $10,000 | **8.43%** |

The filtered strategy earns 92% of the dollars on 38% of the capital. Ninety-
nine opportunities at a 0.52% median margin tie up money for almost nothing.
`MIN_MARGIN_PCT` and `MIN_PROFIT` are not caution settings — they are a
capital-efficiency constraint, and the data is what shows it.

```bash
arb backtest --compare
```

### Two accounts, one price feed

```
BOOK PAIRS BY IDENTICAL PRICING (>= 200 shared markets)
  Ladbrokes / Neds                 11,198 shared     99.9% identical
  TABtouch / Unibet                16,594 shared     52.4% identical
  PlayUp / TAB                     11,708 shared     23.8% identical
```

Ladbrokes and Neds are both Entain-owned and post the **same price 99.9% of the
time**. Holding both is one account's worth of information behind two accounts'
worth of capital. This is measured as *exact price agreement*, not correlation:
two books that move together but differ by a tick still create arbitrage; two
that post identical numbers never can.

### Crossing rate is not opportunity rate

| Sport | Boards | Crossed | Rate | **Actionable** |
|---|---|---|---|---|
| icehockey_nhl | 2,352 | 550 | 23.38% | **0** |
| basketball_nba | 5,880 | 357 | 6.07% | 3 |
| aussierules_aflw | 3,616 | 140 | 3.87% | **45** |
| baseball_mlb | 20,308 | 27 | 0.13% | 4 |

Ice hockey crossed seventy times more often than baseball and produced nothing:
average margin 0.20%, maximum 0.64%, and 433 of the 550 sat on thin two-book
boards. Ranking sports by crossing rate would have put the worst one first.

Three boards showed margins above 10% — up to 30.18%. Those are stale or
mistyped prices that would have been voided on placement, not opportunities.
They are excluded from every figure above and counted separately, because a
backtest that banks a 30% "arbitrage" is measuring its own data errors.

---

## Try it

No API key, no credits, nothing fetched:

```bash
git clone https://github.com/Rijul0107/sports-arbitrage-scanner && cd sports-arbitrage-scanner
python3 watch.py --demo        # terminal view
python3 serve.py --demo        # dashboard at http://127.0.0.1:8787
```

Live, with a free key from [the-odds-api.com](https://the-odds-api.com):

```bash
export ODDS_API_KEY="..."
python3 verify_live.py         # check price freshness before trusting anything
arb scan                       # one poll
arb serve                      # dashboard
```

## Commands

```
arb scan       one-off scan of the live board
arb watch      poll continuously
arb alert      scan once and send to Telegram (cron entry point)
arb bot        Telegram bot that answers replies to alerts
arb serve      local dashboard
arb books      list bookmaker titles and price the next poll
arb season     which sports are in season — costs no credits
arb study      replay logged boards under each candidate book subset
arb backtest   simulate a bankroll over the logged boards
arb verify     prove the staking maths against known cases
```

## How it works

```
The Odds API  ──▶  scan.py      find two-outcome markets, best price per side
                     │
                     ▼
                   core.py      evaluate → allocate stakes → whole-dollar round
                     │
       ┌─────────────┼──────────────┬───────────────┐
       ▼             ▼              ▼               ▼
    alert.py      serve.py      record.py       bot.py
    Telegram      dashboard     board log       reply handler
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                    backtest.py          analytics.py
                    bankroll sim         margins, correlation
```

**Design decisions worth defending in a code review:**

- **The page recomputes every margin from the raw prices.** The server sends
  odds and commission rates, never a derived figure, so the dashboard can never
  disagree with the numbers it was built from. Two implementations of the same
  arithmetic is the failure mode this repo works hardest to avoid.
- **Whole-dollar stakes.** Bookmakers do not accept $412.67. `realised_margin`
  is the margin that survives rounding both legs, and it gates every alert —
  the theoretical margin is never what gets reported.
- **Commission is per-book.** Betfair is an exchange and charges on winnings;
  applying a flat rate would overstate its profit and rank it first.
- **Empty API responses are free**, so out-of-season keys cost nothing while
  a poorly-covered sport costs a credit to return games you cannot hedge.
  Sport selection is priced, not guessed.
- **No dependencies.** urllib, sqlite3, http.server, zoneinfo. It deployed to a
  $4 droplet with `git pull` and no virtualenv.

## Tests

```bash
pip install -e ".[dev]"
pytest -q            # 214 tests
python verify.py     # staking maths against hand-computed cases
```

Every test runs offline against recorded fixtures. A test that needed a live
key would be a test that silently stops running in CI.

Economic thresholds used by fixtures are pinned in `tests/fixture_config.py`
rather than read from `config.py`. When the production stake moved 1500 → 1000,
eighteen tests began failing with `IndexError` — not because the alerting logic
broke, but because the demo fixture's profit fell below `MIN_PROFIT` and the
filtered list went empty. A pricing decision should not be able to take out a
fifth of the suite.

## Status

Retired August 2026. The scanner is stopped and the cloud host destroyed; the
board log it produced is what the analysis above runs on. `season.py` still runs
daily on the free API tier — it reads only the unmetered `/sports` endpoint and
constructs the client with a zero credit budget, so an accidental odds call
raises rather than spending.

## Licence

MIT. Nothing here is betting advice, and none of the returns above were realised.
