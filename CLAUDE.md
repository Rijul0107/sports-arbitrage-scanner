# Arb Desk — project context

Read this before changing anything. It documents not just what the code does
but why several non-obvious decisions were made, and which mistakes have
already been made once.

---

## 1. What this is

An operational tool for finding cross-book sports betting arbitrage across
Australian bookmakers and telling the user exactly which two books to bet with
and how much on each.

**The user places real money off this output.** A wrong number here is not a
failing test, it is a losing bet. Correctness of the money figures outranks
every other consideration in this repo, including elegance, performance and
feature completeness.

Scope, deliberately narrow:

- **Sports**: two-outcome markets only — tennis, NRL, AFL, NBA/WNBA/NBL, MLB,
  NFL, MMA and NHL. No draw leg to cover, so an arbitrage needs only two
  opposing prices. Configured as `SPORT_KEYS` plus `SPORT_PREFIXES` in
  `config.py`.
- **Markets**: head-to-head everywhere; spreads and totals on NRL and AFL;
  totals on NHL. Per sport in `config.SPORT_MARKETS`, because cost is
  `markets × regions` per poll and a market is only worth its credit where AU
  books actually quote it. Every market must be two-outcome *at one line* —
  see §5, this is where the money is lost if it is got wrong.
- **Region**: Australian bookmakers.
- **Timing**: pre-match only, for legal reasons (see §5).
- **Books**: twelve, configured in `config.py`. Betfair is an exchange and
  carries commission; see §4.

### What this is NOT

There is a **separate** research project (`arb-desk`, different repo) that logs
detections to SQLite and produces figures for an academic paper on arbitrage
viability. That concern was deliberately split out. If a proposed change only
makes sense for an academic write-up — logging every scan, tracking outcomes
over time, computing summary statistics for publication — it does not belong
here. This repo is the tool someone actually trying to arbitrage would use.

---

## 2. Getting oriented

```bash
python3 serve.py --demo        # dashboard, fabricated odds, no key, no credits
python3 watch.py --demo        # same data, terminal view
```

Both work immediately with no configuration. Do this first — it is the fastest
way to understand the output shape before reading any code.

Then read, in this order: `arbtool/core.py` (the maths), `arbtool/pairs.py`
(the pairwise matrix, which is the distinctive feature), `arbtool/scan.py` (how
API responses become ranked opportunities).

---

## 3. Layout and data flow

```
The Odds API
     │  (HTTPS, urllib — no third-party deps anywhere in this repo)
     ▼
arbtool/api.py ......... OddsAPI client. Credit accounting, budget guard,
     │                   freshness helpers (staleness_seconds, is_in_play).
     ▼
arbtool/core.py ........ evaluate() → Arb. allocate() → stakes.
     │                   Pure functions, no I/O, no imports from elsewhere
     │                   in the package. This is the load-bearing module.
     ▼
arbtool/lines.py ....... submarkets() → SubMarket. Splits one API market into
     │                   one two-outcome market per LINE, grouped on the exact
     │                   signed line so a sandwich cannot be assembled. Read
     │                   this before touching totals or spreads — see §6.
     ▼
arbtool/pairs.py ....... analyse_game() → GameAnalysis. Evaluates EVERY
     │                   pairing of books, not just the global best.
     ▼
arbtool/scan.py ........ assess_event() → list[Opportunity], one per market.
     │                   Applies safety rules, stakes the best pairing,
     │                   ranks by cash.
     ├──────────────────────────────┐
     ▼                              ▼
watch.py                       serve.py
(terminal, prints cards)       (HTTP server + API proxy)
                                    │
                                    ▼
                              static/dashboard.html
                              (recomputes margins in JS from the odds it
                               is sent — see §7, this is important)
```

Supporting scripts:

| File | Purpose |
|---|---|
| `verify.py` | Independent verification of the maths. Not a unit test suite — it re-derives results a *different* way so a shared bug is less likely to hide. Run after any change to `core.py` or `pairs.py`. |
| `verify_live.py` | Makes one real API call, prints capture timestamps, asks the user to compare against bookmaker apps. Answers "is the feed actually live". |
| `config.py` | Every setting. Scripts read from here; flags override. |

---

## 4. The maths

Everything rests on one identity. For a market with outcomes 1..n, take the
best available price for each outcome:

```
S = Σ (1 / best_odds_i)
```

The market is arbitrageable **iff S < 1**. Backing every outcome in proportion
to its inverse odds returns the same amount whichever outcome wins:

```
stake fraction on i = (1 / odds_i) / S
return on total T   = T / S
profit              = T × (1/S − 1)
```

`Arb.margin` is `1/S − 1` as a fraction. Everything else in `core.py` is that
identity plus the corrections that make it survive contact with a real
bookmaker.

### Correction 1 — commission, per book

Charged on winnings, not on the returned stake. Effective odds become
`1 + (odds − 1) × (1 − c)`. Betfair charges it; the corporates do not.

`c` is **per bookmaker**, not per market — `config.BOOK_COMMISSION_PCT` maps
book to rate and `COMMISSION_PCT` is the floor for books with no entry.
`evaluate()` accepts either a number or that mapping. A single global rate is
wrong whichever value it takes: zero overstates a Betfair leg and turns a
settled loss into an apparent arbitrage, while Betfair's rate applied to
everything penalises corporate legs by a fee they never charge.

Two consequences that are easy to miss:

- **Commission decides which book wins an outcome**, not only what it pays.
  Betfair at 2.20 less 5% pays 2.14, so a corporate at 2.16 is the better leg
  despite the shorter headline. `best_odds_per_outcome()` ranks on the net
  figure and returns the raw one, because the raw price is what the betting
  slip shows and what the user must confirm in the app.
- **`Leg.returns` is net.** `Leg.gross_returns` is the slip figure. Every
  guaranteed number is built from the net one; building them from gross would
  overstate `worst_profit` by exactly the commission.

### Correction 2 — stake rounding (the one that matters)

Books take whole dollars. `allocate()` rounds every leg **down** to the
increment, then hands the remainder out one increment at a time, always to
whichever leg currently returns least.

Rounding down keeps outlay within the stake, which a fixed budget requires.
The remainder pass lifts the worst-case return rather than letting it sag.
This is provably optimal — `verify.py` brute-forces every possible whole-dollar
split across hundreds of random arbitrages and confirms zero shortfall. **Do
not "simplify" this to naive rounding.**

A thin edge can be genuinely real and still lose money after rounding. A 1.23%
edge on $150 at whole-dollar stakes returns a guaranteed *loss*. Both UIs
detect this and refuse to print a slip, showing the minimum stake that would
work instead.

**Always report `Arb.worst_profit`, never the theoretical margin.** After
rounding the legs no longer pay identically, so `worst_profit` is the only
number that is actually guaranteed.

### Correction 3 — stake limits

`apply_stake_limits()` rescales the whole position by the tightest binding
ratio. A book capping one leg does not break the arbitrage, it caps its size —
scaling both legs preserves the hedge.

### Execution ordering

`recommend_first_leg()` returns the **longest price**. Long odds move further
on the same shift in implied probability, and an outlier long price is the one
about to be corrected. Getting the fragile leg down first means that if the
second has moved, you are unhedged on the shorter price — a smaller stake and
easier to lay off.

---

## 5. Invariants that must not be broken

Each of these has a reason. Do not relax one without understanding it.

### Pre-match only — this is a legal constraint, not a preference

Online in-play betting is **prohibited in Australia** under the Interactive
Gambling Act. Licensed operators may accept in-play bets only by telephone or
in a retail venue. `assess_event()` returns `None` for anything already under
way while `config.PRE_MATCH_ONLY` is True.

Do not add a flag to bypass this. Do not surface in-play markets "for
information". `tests/test_server.py::test_in_play_event_excluded` guards it.

A side effect worth knowing: this is also why a 60-second feed is defensible
here. Pre-match edges persist for minutes; in-play edges last seconds. The
tool's latency profile is adequate for what is legal and would not be for what
is not.

### Arbitrage requires two different bookmakers

If one book holds the best price on every outcome, the raw maths may say
`S < 1` but it is not placeable — bookmakers do not let you back both sides of
a market with them. `Arb.single_book` flags it and `PairResult.is_arb` already
accounts for it.

### Two-outcome enforcement

`evaluate(..., expect_outcomes=2)` returns `None` rather than computing a wrong
answer if a book erroneously lists a third outcome. Never relax this silently
— a mis-evaluated three-way market presented as a two-way arb is exactly the
kind of error that costs money.

`lines.submarkets()` enforces the same rule earlier and harder: a book listing
three or more outcomes **poisons that market for the whole event**, so no
grouping is built from it at all. This is not belt-and-braces, it is load
bearing. Under the single `h2h` key some books price ice hockey including
overtime (two-way) and others price regulation time (three-way, with a Draw).
Measured 2026-08-10: NHL returned 27 two-way and 14 three-way h2h quotes,
boxing returned 17 two-way against 132 three-way. Backing one team at a
two-way book and the other at a three-way book loses **both** legs when the
game is drawn in regulation and the first team wins in overtime — a sandwich
with no line involved. Guarded by
`tests/test_lines.py::test_three_way_quote_poisons_the_market_for_the_event`.

### Same line, or it is not a hedge

Totals and spreads are only two-sided at one exact line. Over 40.5 against
Under 40.5 is a hedge; Over 41.5 against Under 40.5 is a **sandwich** that
loses both legs on a total of exactly 41. Spreads carry a nastier version,
because the two sides hold *mirrored* points rather than equal ones — grouping
on `abs(point)` looks right and pairs `Penrith -1.5` with `Roosters -1.5`,
which needs both teams to win by two.

`lines.submarkets()` groups on the exact signed side set before anything
downstream sees a price, so there is no code path that compares two different
lines. `is_sandwich()` is the named guard, asserted against every group built.
Do not add a market by extending the `markets` string alone — it has to come
through `submarkets()`.

### Partial coverage is not arbitrage

If any named outcome lacks a usable price anywhere, `evaluate()` returns
`None`. You cannot hedge a result you have not covered.

### Prices at or below 1.00 are ignored

A decimal price of 1.00 returns the stake and is not a real market. Treated as
suspended/malformed along with `None` and `NaN`.

### Staleness gates everything

An arbitrage computed from three-minute-old prices is a claim about the past.
`MAX_DATA_AGE_SECONDS` rejects it and every view displays quote age. The Odds
API refreshes pre-match h2h roughly every 60 seconds, so anything much beyond
that is not actionable.

### Credit budget guard

Free tier is 500/month. One poll costs 1 credit **per sport**. `OddsAPI` raises
`BudgetExceeded` *before* making a call that would overspend, and
`QuotaExhausted` when the plan balance hits the floor. `serve.py` catches these
and serves its last good snapshot rather than dying.

Do not remove these guards. Be extremely careful adding API calls inside loops
— it is easy to burn a month in an afternoon. `/sports` and `/events` are free
(cost=0); prefer them when you only need fixture times.

---

## 6. Known traps

These have been got wrong already. The tests named here exist specifically to
stop them recurring.

### `PairResult.arb` vs `PairResult.is_arb`

`.arb` is the `Arb` **object** and is truthy whenever the market could be
evaluated at all — including when the margin is negative. `.is_arb` is the
boolean.

Testing the wrong one made `best_pair` return the least-bad **losing** pairing
and the UI bracketed it as "best", which reads as a recommendation when nothing
is on offer. Worse than showing nothing.

`GameAnalysis.best_pair` returns `None` unless the top pairing genuinely
crosses. `GameAnalysis.top_pair` returns the least-bad pairing regardless, for
showing how close a market is getting. Use the right one.

Guarded by `tests/test_pairs.py::test_best_pair_is_none_when_nothing_crosses`.

### Hardcoded margins drifting from their odds

An earlier dashboard demo payload carried hand-written margins that did not
match the prices displayed directly above them — one "arb" was actually a 0.3%
overround. Fixed by having the page compute every margin from the odds. Never
reintroduce precomputed margins into a payload or fixture.

### Alphabetical outcome ordering

`GameAnalysis.outcomes` is sorted alphabetically so the maths is deterministic.
That is the wrong order to *read* a match in. Use
`Opportunity.display_outcomes` for anything user-facing — home team first. It
matches on prefix, not equality, because a spreads outcome is the team name
plus its handicap (`"Penrith Panthers -1.5"`).

### Truncating an outcome name loses the line

`"Sydney Roosters -1.5"` cut to 14 characters reads `"Sydney Rooster"`, which
is the outright bet on a different market. `watch.col_name()` abbreviates the
team and keeps the handicap. Anywhere else that shortens an outcome for display
has to do the same.

### A pairing that cannot be evaluated is not a pairing

`pair_matrix()` used to append a `PairResult` even when `evaluate()` returned
`None`, whose `margin_pct` is `-inf`. The terminal matrix rendered that as
`-inf%` and counted it in the "X of Y pairings work" denominator, while the
dashboard's `evaluatePair()` dropped it — so the two engines disagreed about Y
whenever a book quoted only one side of a market. Rare on head-to-head, routine
on totals and spreads where books suspend one side. `pair_matrix()` now skips
them. Guarded by `tests/test_pairs.py::TestUncoverablePairingsAreAbsent` and end
to end by `tests/test_dual_engine.py`.

### `str.replace` with an empty match

Not a code bug but a process one: patching this repo by computing a slice
between two `str.index()` calls silently produced an empty string when the
first anchor appeared in a CSS comment before the second. `s.replace("", x)`
inserts between every character and produced a 102 MB file. Prefer the Edit
tool with unique anchors over programmatic string surgery.

---

## 7. The dual-engine constraint

`static/dashboard.html` contains its own JavaScript implementation of the
pairwise engine and the staking algorithm, mirroring `arbtool/pairs.py` and
`arbtool/core.py`.

This is deliberate. `Opportunity.to_dict()` sends **odds and ages only** —
never precomputed margins — so the page derives every figure from the prices it
displays. What is on screen therefore cannot disagree with the prices it came
from.

The cost is duplication. **If you change one, change the other and re-verify**,
or the browser and terminal will silently diverge.

Verification is no longer manual. `python3 tests/test_dual_engine.py` extracts
the real functions from the shipped HTML — by walking braces from a named
function, never by slicing between string offsets, see §6 — and runs them over
the demo markets plus 600 random boards, comparing margins to 1e-6 and `is_arb`
exactly. It needs `node` on PATH and skips without it. The random half is the
half that earns its keep: it includes books quoting only one side, which is
where the engines were actually found to disagree.

`tests/e2e_server_browser.mjs` covers the integration path but hardcodes a
playwright install that does not exist on every machine, so it is not the check
that runs.

---

## 8. Contracts

### `/api/opportunities` payload

```jsonc
{
  "books": ["Sportsbet", "Ladbrokes", "TAB", "Neds"],  // order fixes matrix axes
  "credits_remaining": 451,        // null if the header was unreadable
  "credits_spent": 3,
  "fetched_at": "2026-08-09T05:05:17+00:00",
  "games": [{
    // One entry per MARKET, not per game. A fixture with a head-to-head arb
    // and a 50.5 totals arb appears twice, with different ids.
    "id": "e1",                      // event id, or "<id>:<market>:<label>"
    "sport": "NRL",
    "home": "Penrith Panthers",
    "away": "Melbourne Storm",
    "starts_in_min": 47,
    "market": "",                    // "" for h2h, else "Total 50.5" / "Line 1.5"
    "outcomes": ["Penrith Panthers", "Melbourne Storm"],   // home first
    "odds":  {"Sportsbet": {"Penrith Panthers": 2.12, "Melbourne Storm": 1.78}, ...},
    "ages":  {"Sportsbet": 11, "Ladbrokes": 24, ...}       // seconds, or null
  }]
}
```

For a lined market the outcome names carry the line — `"Over 50.5"`,
`"Penrith Panthers -1.5"` — so no surface needs to know that lines exist and no
leg can be confirmed against the wrong market in the app. The line is *not*
sent as a separate numeric field, for the same reason margins are not sent: a
second copy of a fact can drift from the first.

No `pairs` key. The page computes them. See §7.

If the fetch thread has not completed a first poll, `/api/opportunities`
returns 503 with `{"error": "warming up"}`. The dashboard treats any failure as
"fall back to the built-in demo payload" rather than blanking the screen.

### `config.py` surface

Scripts read module-level constants; CLI flags override into a
`SimpleNamespace` copy. Anything reading config should accept a `cfg` object
rather than importing `config` directly, so flags work — `scan.py` follows this
pattern and new code should too.

---

## 9. Testing policy

```bash
python3 tests/test_core.py        # 33 — arbitrage maths, staking, edge cases
python3 tests/test_pairs.py       # 19 — pairwise matrix, depth, the arb/is_arb trap
python3 tests/test_lines.py       # 21 — the sandwich guard and line grouping
python3 tests/test_server.py      # 17 — server, payload shape, in-play exclusion
python3 tests/test_alert.py       # 22 — Telegram suppression and formatting
python3 tests/test_bot.py         # 42 — reply bot, restake
python3 tests/test_dual_engine.py #  1 — browser engine v Python, 600+ boards
python3 verify.py                 # independent verification (see below)
node tests/e2e_server_browser.mjs   # needs a server on :8792 AND a playwright
                                    # install it hardcodes — usually will not run
```

**After any change to `core.py`, `pairs.py` or `lines.py`, run `verify.py` and
`tests/test_dual_engine.py`.** It is not a
unit test suite. It re-derives results a different way:

- headline numbers recomputed by hand in **exact fractions**, so float error
  cannot mask a logic error
- `allocate()` brute-forced against exhaustive search over every possible
  whole-dollar split
- arbitrage detection cross-checked against exhaustive assignment search
  (every outcome to every book) over 2,000 random markets
- invariants over 3,000 random allocations: outlay never exceeds stake, no
  negative stakes, every stake a whole multiple of the increment

If you change the maths, add a test that would have caught the change being
wrong. A test that passes both before and after your change has not tested it.

---

## 10. Credit economics

The plan is the $30 tier: **20,000 credits a month.** Cost is
`[markets] × [regions]` per sport key that returns games; empty responses are
free, and `/sports` and `/events` cost nothing at all.

Markets are per sport in `config.SPORT_MARKETS`, so a poll is not one credit
per key any more:

| Sport key | Markets | Credits |
|---|---|---|
| `rugbyleague_nrl` | h2h, spreads, totals | 3 |
| `aussierules_afl` | h2h, spreads, totals | 3 |
| `baseball_mlb` | h2h, spreads, totals | 3 |
| `basketball_wnba` | h2h, spreads, totals | 3 |
| `icehockey_nhl` | totals | 1 |
| every other active key | h2h | 1 each |

`python3 books.py --sports` prices the next poll for free and is the only
number worth trusting, because the active key count moves weekly with tennis
tournaments. Measured 2026-08-10: **11 keys, 19 credits a run**. The cron is
`*/30` all day and `alert.py` trims to `config.SCAN_WINDOW_START/END`
(06:40–22:40 Sydney) itself, so 32 runs land in-window: **608 a day, about
18,240 a month — 91% of plan.** Deliberately close; unspent credits buy
nothing.

**The headroom is thin, and the cliff is nearer than the finals.** Two more
live tennis tournaments tips it, and tennis adds keys without warning:

| Active keys | Credits/run | Per month | |
|---|---|---|---|
| 11 (2026-08-10) | 19 | 18,240 | fits |
| 13 | 21 | 20,160 | over |
| 15 (finals) | 23 | 22,080 | over |

Run `books.py --sports` (free) at the start of each month. When it goes over,
trim `SPORT_MARKETS`: WNBA first (4 books, 5 fixtures, no crossing observed),
then AFL totals (3 books on 3 of 9 games). Both tripwires are tested in
`tests/test_alert.py::TestScanWindow`, which derives the cost from
`SPORT_MARKETS` rather than a written-down number.

The window lives in `config.py`, not in cron, so daylight saving is the
timezone database's problem. `books.py` counts the billable runs from it by
walking the day a minute at a time — cron fires on the wall clock, so `*/30`
hits :00 and :30 and misses a window edge at :40 entirely, which counting hours
and dividing gets wrong.

Tightening the interval does not fit, and **`*/25` is a trap**: cron reads
`*/n` on the minute field as "minutes 0–59 divisible by n", not "every n
minutes rolling". `*/25` fires at :00, :25 and :50 — three times an hour with a
ten-minute gap at the top, costing exactly what `*/20` costs rather than the
fifth less the name implies. They coincide only when `n` divides 60.

| Interval | Runs/day | Per day | Per month | |
|---|---|---|---|---|
| `*/30` | 32 | 608 | 18,240 | fits |
| `*/25` | 48 | 912 | 27,360 | over |
| `*/20` | 48 | 912 | 27,360 | over |

That headroom is the budget for anything new. Before adding a market or a key,
multiply: one more market on one already-polled sport is +32 credits a day, and
one more market across all keys is +11 a run, +10,560 a month. Blanket
`h2h,spreads,totals` everywhere would be 33 a run and ~31,700 a month — well
over the plan. It was `*/20` until 2026-08-10; halving the frequency is what
paid for spreads and totals, on the reasoning that prices days out from a
fixture barely move between polls.

The productive window is the couple of hours before a fixture — books are
actively repricing and disagreement is widest. That is *not* how this tool is
used in practice, which is days out; size any advice against the real pattern.

If the plan is outgrown: RapidOddsAPI's $49/month tier gives 200,000 credits
with deeper AU coverage, against The Odds API's $59 for 100,000. Their $149
tier adds WebSocket, but it pushes after each scrape cycle so it removes
polling latency, not capture latency — probably not worth it at this scale.

---

## 11. Domain notes worth carrying

**Ladbrokes and Neds are both Entain-owned** and their prices track each other
closely, so that pairing rarely disagrees. If the user asks about improving hit
rate, swapping one for Betfair Exchange or PointsBet is the first suggestion.

**Tennis retirement rules differ between books** — some void, some pay out on
whoever advances, some settle after one set. If two books differ, a retirement
turns a hedged position into a one-sided bet. This is a real risk specific to
tennis and is not modelled in the code; it is documented in the README.

**Bet365 AU** is listed on The Odds API as paid-plans-only, h2h/spreads/totals,
AFL and NRL — but it does not deliver. Requested by name on the paid key on
2026-08-09 it returned 8 NRL and 7 AFL fixtures with zero Bet365 prices on any
of them. It is not a tier problem; all paid tiers list identical bookmaker
access. Do not re-add it on the strength of the documentation.

**The real exposure is one leg, not the stake.** If leg one fills and leg two
has moved, the user holds an unhedged bet worth roughly half the total. The
terminal cards, the dashboard and the README state this.

The Telegram alert **deliberately does not**, at the owner's request
(2026-08-10). The reasoning: a fixed warning repeated on every single message
is skimmed past within a week, and the alert already carries the operative
instruction in its CHECK BEFORE STAKING block — confirm both prices in both
apps, and do not place one leg on the strength of the message. `core.py` still
exposes `unhedged_exposure()` and `tests/test_core.py` still guards it, so the
figure remains available to any surface that wants it.

---

## 12. Deliberately not built

Do not add these without being asked:

- **Automated bet placement.** No API access to AU books, and the failure modes
  are severe. The tool advises; the human places.
- **Additional markets** (spreads, totals). Would find more arbitrage but
  multiplies credit cost per poll — 3 markets is 3× the burn. A deliberate
  choice given the free tier.
- **Persistence / history.** That is the research repo's job (§1).
- **Third-party dependencies.** The whole tool runs on the standard library.
  Keep it that way unless there is a strong reason.
- **Account or bankroll tracking.** Out of scope.

---

## 13. Style

Comments explain **why**, not what. The maths is the load-bearing part — a
comment there should say what would go wrong if the line were different.

Money figures lead with dollars; percentages are secondary. `$74.56` first,
`4.99%` as supporting detail. The user reasons in cash.

Prose in docstrings and user-facing strings should be precise about what is
guaranteed versus expected. "Guaranteed profit" means guaranteed *after*
rounding, assuming both legs fill. Say so.

No emoji in output. The terminal cards use box-drawing characters and ANSI
colour, and degrade cleanly when piped (`C.off()` when not a TTY).
