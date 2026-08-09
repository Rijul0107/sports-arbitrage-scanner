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

- **Sports**: NRL and tennis. Both are two-outcome — no draw leg to cover, so
  an arbitrage needs only two opposing prices. This is the clean case.
- **Market**: head-to-head only.
- **Region**: Australian bookmakers.
- **Timing**: pre-match only, for legal reasons (see §5).
- **Books**: four, configured in `config.py`.

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
arbtool/pairs.py ....... analyse_game() → GameAnalysis. Evaluates EVERY
     │                   pairing of books, not just the global best.
     ▼
arbtool/scan.py ........ assess_event() → Opportunity. Applies safety rules,
     │                   stakes the best pairing, ranks by cash.
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

### Correction 1 — commission

Charged on winnings, not on the returned stake. Effective odds become
`1 + (odds − 1) × (1 − c)`. Betfair Exchange charges it; traditional books do
not. Default 0.

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
`Opportunity.display_outcomes` for anything user-facing — home team first.

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

The cost is duplication. The two implementations were verified to return
identical results across the full demo dataset. **If you change one, change the
other and re-verify**, or the browser and terminal will silently diverge.

The verification approach: extract the JS functions from the HTML, run the same
inputs through both, compare margins to 1e-6 and `is_arb` exactly. There is a
worked example in the git history of this session; `tests/e2e_server_browser.mjs`
covers the integration path.

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
    "id": "e1",
    "sport": "NRL",
    "home": "Penrith Panthers",
    "away": "Melbourne Storm",
    "starts_in_min": 47,
    "outcomes": ["Penrith Panthers", "Melbourne Storm"],   // home first
    "odds":  {"Sportsbet": {"Penrith Panthers": 2.12, "Melbourne Storm": 1.78}, ...},
    "ages":  {"Sportsbet": 11, "Ladbrokes": 24, ...}       // seconds, or null
  }]
}
```

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
python3 tests/test_core.py       # 26 — arbitrage maths, staking, edge cases
python3 tests/test_pairs.py      # 16 — pairwise matrix, depth, the arb/is_arb trap
python3 tests/test_server.py     #  6 — server, payload shape, in-play exclusion
python3 verify.py                # independent verification (see below)
node tests/e2e_server_browser.mjs  # requires a server on :8792, see the file
```

**After any change to `core.py` or `pairs.py`, run `verify.py`.** It is not a
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

| Pattern | Credits | Against 500/month |
|---|---|---|
| One poll, NRL + tennis in season | 2 | |
| Dashboard open 2h at 60s refresh | ~240 | half the month |
| Terminal watch, 2h before a fixture | ~120 | four sessions |
| Continuous all month | ~86,000 | needs a paid plan |

The productive window is the couple of hours before a fixture — books are
actively repricing and disagreement is widest. Tennis offers more simultaneous
markets than NRL and is generally the better hunting ground.

If the user outgrows the free tier: RapidOddsAPI's $49/month tier gives 200,000
credits with deeper AU coverage, against The Odds API's $59 for 100,000. Their
$149 tier adds WebSocket, but it pushes after each scrape cycle so it removes
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

**Bet365 AU** is on The Odds API but only on paid plans, and only h2h/spreads/
totals for AFL and NRL.

**The real exposure is one leg, not the stake.** If leg one fills and leg two
has moved, the user holds an unhedged bet worth roughly half the total. Every
UI surface states this. Keep it there.

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
