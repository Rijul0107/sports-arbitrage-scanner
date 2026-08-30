# Handover

**What this file is:** the walkthrough for picking this codebase up cold,
written for whoever works on it next — including AI assistants, which is why it
opens with a prompt to paste. [CLAUDE.md](CLAUDE.md) is the reference it points
at. Neither is user documentation: [README.md](README.md) is the project and its
findings, [OPERATING.md](OPERATING.md) is how to run it.

---

## Paste this into Claude Code to begin

> I'm working on Arb Desk, a cross-book sports betting arbitrage tool. Read
> CLAUDE.md first — it documents the architecture, the maths, and several
> invariants that must not be broken, including a legal one about in-play
> betting.
>
> For this first session: verify the repo is sound before we change anything.
> Run the three test suites and verify.py, then `python3 serve.py --demo` and
> `python3 watch.py --demo` so I can see both interfaces working. Report
> anything that fails or looks wrong. Don't make changes yet.

Once that passes, subsequent sessions can start with just "read CLAUDE.md".

---

## State of play

**Complete and verified.** 48 tests pass, plus an independent verification pass
that re-derives the maths a different way. The full stack has been exercised
end to end — server through browser — producing figures identical to the
terminal and to hand calculation.

**Never run against the real API.** Every number produced so far came from
fabricated data. The sandbox this was built in had no network access to The
Odds API. The code paths for auth failure, quota exhaustion and network error
are implemented and unit-tested against stubs, but have not met the real
service. Treat the first live run as a test.

---

## First session checklist

```bash
cd arb-tool
python3 tests/test_core.py       # expect 26 OK
python3 tests/test_pairs.py      # expect 16 OK
python3 tests/test_server.py     # expect  6 OK
python3 verify.py                # expect ALL CHECKS PASSED

python3 serve.py --demo          # dashboard opens in browser
python3 watch.py --demo          # terminal view of the same data
```

No API key needed for any of that. No `pip install` — the tool runs on the
standard library alone.

### Then, going live

```bash
export ODDS_API_KEY="..."        # free key at the-odds-api.com
python3 verify_live.py           # 1 credit — do this before trusting anything
```

`verify_live.py` prints every bookmaker's price with the exact timestamp the
API captured it, then asks you to compare against the bookmaker apps on your
phone. It reports median quote age and says plainly whether the lag makes
arbitrage realistic.

**Do this before staking money.** The whole tool rests on the assumption that
the feed is fresh enough to act on, and that assumption has never been tested
against reality. If the API consistently lags your phone by more than a minute
or two, better to find out now.

Then:

```bash
python3 serve.py                 # dashboard, ~2 credits per 60s refresh
python3 watch.py                 # terminal, alerts on a find
python3 watch.py --once          # single poll
```

---

## Configuration you will want to change

All in `config.py`:

| Setting | Default | Note |
|---|---|---|
| `BOOKS` | Sportsbet, Ladbrokes, TAB, Neds | Must match the API's names exactly. Ladbrokes and Neds are both Entain-owned and rarely disagree — consider swapping one for Betfair or PointsBet. |
| `TOTAL_STAKE` | 1000 | Split across both legs of one arbitrage. |
| `MIN_PROFIT` | 10 | Hides edges paying less. Set to 0 to see everything. |
| `SESSION_CREDIT_BUDGET` | 120 | Hard cap per run. The guard that stops an afternoon eating the month. |
| `POLL_SECONDS` | 60 | Below ~30 you are mostly paying for the API's own refresh cycle. |

To confirm the book names your region actually returns, run `verify_live.py
--all-books` — it prints every bookmaker in the response.

---

## Known gaps, in the order I would address them

**1. Book names are unverified.** `config.BOOKS` uses the names The Odds API
documents, but casing and spacing have not been confirmed against a live
response. If a book silently never appears in the matrix, this is why. First
live run should check.

**2. No minimum-edge control in the dashboard.** `MIN_PROFIT` filters the
terminal and the server-side ranking, but the dashboard renders whatever it is
sent. A sub-$5 edge will still show a card. Small fix, needs a threshold
control in the header.

**3. Single market only.** Head-to-head. Spreads and totals would find more
arbitrage but multiply credit cost per poll — deliberate given the free tier.
If the user upgrades, `scan()` takes a `markets` string and `analyse_game()`
already accepts a `market_key`; the work is in the UI, which assumes one market
per game.

**4. No alerting beyond a terminal bell.** `watch.py` writes `\a` on a find.
Desktop notification or a push would be more useful for a long watch.

**5. Fixed 4×4 matrix layout.** The dashboard matrix is built from
`DATA.books`, so five books produces a 5×5 grid and works — but it has only
been eyeballed at four. Check the layout if the user adds one.

---

## The one thing to get right

The user places real money off this output. If a change touches
`arbtool/core.py` or `arbtool/pairs.py`, run `verify.py` before saying it
works. It brute-forces the staking against exhaustive search and cross-checks
detection against a completely different algorithm — it catches things unit
tests do not.

And if you change the JavaScript engine in `static/dashboard.html`, change the
Python to match, or the browser and terminal will quietly disagree about how
much money is on the table. §7 of `CLAUDE.md` explains why the duplication
exists.
