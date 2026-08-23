# Arb Desk — project context

Read before change anything. Documents what code does, why non-obvious decisions made, which mistakes already made once.

---

## 1. What this is

Operational tool. Finds cross-book sports betting arbitrage across Australian bookmakers, tells user which two books to bet with and how much on each.

**User places real money off this output.** Wrong number here not failing test — losing bet. Correctness of money figures outranks everything else in repo: elegance, performance, feature completeness.

Scope, deliberately narrow:

- **Sports**: two-outcome markets only — tennis, NRL, AFL, NBA/WNBA/NBL, MLB, NFL, MMA, NHL. No draw leg to cover, so arbitrage needs only two opposing prices. Configured as `SPORT_KEYS` plus `SPORT_PREFIXES` in `config.py`.
- **Markets**: head-to-head everywhere; spreads and totals on NRL and AFL; totals on NHL. Per sport in `config.SPORT_MARKETS`, because cost is `markets × regions` per poll and market only worth its credit where AU books actually quote it. Every market must be two-outcome *at one line* — see §5, this is where money lost if got wrong.
- **Region**: Australian bookmakers.
- **Timing**: pre-match only, legal reasons (see §5).
- **Books**: seven, configured in `config.py` — cut from twelve on 2026-08-16 (book-cull study, see §12). All corporates; Betfair out, but its commission entry kept for any return.

### What this is NOT

**Separate** research project (`arb-desk`, different repo) logs detections to SQLite, produces figures for academic paper on arbitrage viability. That concern deliberately split out. If proposed change only makes sense for academic write-up — logging every scan, tracking outcomes over time, computing summary statistics for publication — does not belong here. This repo is tool someone actually trying to arbitrage would use.

---

## 2. Getting oriented

```bash
python3 serve.py --demo        # dashboard, fabricated odds, no key, no credits
python3 watch.py --demo        # same data, terminal view
```

Both work immediately, no configuration. Do first — fastest way to understand output shape before reading code.

Then read, in order: `arbtool/core.py` (maths), `arbtool/pairs.py` (pairwise matrix, distinctive feature), `arbtool/scan.py` (how API responses become ranked opportunities).

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
| `verify.py` | Independent verification of maths. Not unit test suite — re-derives results *different* way so shared bug less likely to hide. Run after any change to `core.py` or `pairs.py`. |
| `verify_live.py` | Makes one real API call, prints capture timestamps, asks user to compare against bookmaker apps. Answers "is feed actually live". |
| `config.py` | Every setting. Scripts read from here; flags override. |

---

## 4. The maths

Everything rests on one identity. For market with outcomes 1..n, take best available price for each outcome:

```
S = Σ (1 / best_odds_i)
```

Market arbitrageable **iff S < 1**. Backing every outcome in proportion to inverse odds returns same amount whichever outcome wins:

```
stake fraction on i = (1 / odds_i) / S
return on total T   = T / S
profit              = T × (1/S − 1)
```

`Arb.margin` is `1/S − 1` as fraction. Everything else in `core.py` is that identity plus corrections that make it survive contact with real bookmaker.

### Correction 1 — commission, per book

Charged on winnings, not on returned stake. Effective odds become `1 + (odds − 1) × (1 − c)`. Betfair charges it; corporates do not.

`c` is **per bookmaker**, not per market — `config.BOOK_COMMISSION_PCT` maps book to rate, `COMMISSION_PCT` is floor for books with no entry. `evaluate()` accepts either number or that mapping. Single global rate wrong whichever value it takes: zero overstates Betfair leg and turns settled loss into apparent arbitrage, while Betfair's rate applied to everything penalises corporate legs by fee they never charge.

Two consequences easy to miss:

- **Commission decides which book wins an outcome**, not only what it pays. Betfair at 2.20 less 5% pays 2.14, so corporate at 2.16 is better leg despite shorter headline. `best_odds_per_outcome()` ranks on net figure, returns raw one, because raw price is what betting slip shows and what user must confirm in app.
- **`Leg.returns` is net.** `Leg.gross_returns` is slip figure. Every guaranteed number built from net one; building from gross would overstate `worst_profit` by exactly the commission.

### Correction 2 — stake rounding (the one that matters)

Books take whole dollars. `allocate()` rounds every leg **down** to increment, then hands remainder out one increment at a time, always to whichever leg currently returns least.

Rounding down keeps outlay within stake, which fixed budget requires. Remainder pass lifts worst-case return rather than letting it sag. Provably optimal — `verify.py` brute-forces every possible whole-dollar split across hundreds of random arbitrages, confirms zero shortfall. **Do not "simplify" this to naive rounding.**

Thin edge can be genuinely real and still lose money after rounding. 1.23% edge on $150 at whole-dollar stakes returns guaranteed *loss*. Both UIs detect this, refuse to print slip, show minimum stake that would work instead.

**Always report `Arb.worst_profit`, never theoretical margin.** After rounding, legs no longer pay identically, so `worst_profit` is only number actually guaranteed.

### Correction 3 — stake limits

`apply_stake_limits()` rescales whole position by tightest binding ratio. Book capping one leg does not break arbitrage, caps its size — scaling both legs preserves hedge.

### Execution ordering

`recommend_first_leg()` returns **longest price**. Long odds move further on same shift in implied probability, and outlier long price is one about to be corrected. Getting fragile leg down first means if second has moved, you are unhedged on shorter price — smaller stake, easier to lay off.

---

## 5. Invariants that must not be broken

Each has reason. Do not relax one without understanding it.

### Pre-match only — this is a legal constraint, not a preference

Online in-play betting **prohibited in Australia** under Interactive Gambling Act. Licensed operators may accept in-play bets only by telephone or in retail venue. `assess_event()` returns `None` for anything already under way while `config.PRE_MATCH_ONLY` is True.

Do not add flag to bypass this. Do not surface in-play markets "for information". `tests/test_server.py::test_in_play_event_excluded` guards it.

Side effect worth knowing: this also why 60-second feed defensible here. Pre-match edges persist for minutes; in-play edges last seconds. Tool's latency profile adequate for what is legal, would not be for what is not.

### Arbitrage requires two different bookmakers

If one book holds best price on every outcome, raw maths may say `S < 1` but not placeable — bookmakers do not let you back both sides of market with them. `Arb.single_book` flags it, `PairResult.is_arb` already accounts for it.

### Two-outcome enforcement

`evaluate(..., expect_outcomes=2)` returns `None` rather than computing wrong answer if book erroneously lists third outcome. Never relax this silently — mis-evaluated three-way market presented as two-way arb is exactly kind of error that costs money.

`lines.submarkets()` enforces same rule earlier and harder: book listing three or more outcomes **poisons that market for the whole event**, so no grouping built from it at all. Not belt-and-braces — load bearing. Under single `h2h` key some books price ice hockey including overtime (two-way), others price regulation time (three-way, with Draw). Measured 2026-08-10: NHL returned 27 two-way and 14 three-way h2h quotes, boxing returned 17 two-way against 132 three-way. Backing one team at two-way book and other at three-way book loses **both** legs when game drawn in regulation and first team wins in overtime — sandwich with no line involved. Guarded by `tests/test_lines.py::test_three_way_quote_poisons_the_market_for_the_event`.

### Same line, or it is not a hedge

Totals and spreads only two-sided at one exact line. Over 40.5 against Under 40.5 is hedge; Over 41.5 against Under 40.5 is **sandwich** that loses both legs on total of exactly 41. Spreads carry nastier version, because two sides hold *mirrored* points rather than equal ones — grouping on `abs(point)` looks right and pairs `Penrith -1.5` with `Roosters -1.5`, which needs both teams to win by two.

`lines.submarkets()` groups on exact signed side set before anything downstream sees price, so no code path compares two different lines. `is_sandwich()` is named guard, asserted against every group built. Do not add market by extending `markets` string alone — must come through `submarkets()`.

### Partial coverage is not arbitrage

If any named outcome lacks usable price anywhere, `evaluate()` returns `None`. Cannot hedge result you have not covered.

### Prices at or below 1.00 are ignored

Decimal price of 1.00 returns stake, not real market. Treated as suspended/malformed along with `None` and `NaN`.

### Staleness gates everything

Arbitrage computed from three-minute-old prices is claim about past. `MAX_DATA_AGE_SECONDS` rejects it, every view displays quote age. The Odds API refreshes pre-match h2h roughly every 60 seconds, so anything much beyond that not actionable.

### Credit budget guard

Free tier 500/month. One poll costs 1 credit **per sport**. `OddsAPI` raises `BudgetExceeded` *before* making call that would overspend, `QuotaExhausted` when plan balance hits floor. `serve.py` catches these, serves last good snapshot rather than dying.

Do not remove these guards. Be extremely careful adding API calls inside loops — easy to burn month in afternoon. `/sports` and `/events` are free (cost=0); prefer them when you only need fixture times.

---

## 6. Known traps

These got wrong already. Tests named here exist specifically to stop them recurring.

### `PairResult.arb` vs `PairResult.is_arb`

`.arb` is `Arb` **object**, truthy whenever market could be evaluated at all — including when margin negative. `.is_arb` is boolean.

Testing wrong one made `best_pair` return least-bad **losing** pairing, UI bracketed it as "best", which reads as recommendation when nothing on offer. Worse than showing nothing.

`GameAnalysis.best_pair` returns `None` unless top pairing genuinely crosses. `GameAnalysis.top_pair` returns least-bad pairing regardless, for showing how close market getting. Use right one.

Guarded by `tests/test_pairs.py::test_best_pair_is_none_when_nothing_crosses`.

### Hardcoded margins drifting from their odds

Earlier dashboard demo payload carried hand-written margins that did not match prices displayed directly above them — one "arb" was actually 0.3% overround. Fixed by having page compute every margin from odds. Never reintroduce precomputed margins into payload or fixture.

### Alphabetical outcome ordering

`GameAnalysis.outcomes` sorted alphabetically so maths deterministic. Wrong order to *read* match in. Use `Opportunity.display_outcomes` for anything user-facing — home team first. Matches on prefix, not equality, because spreads outcome is team name plus handicap (`"Penrith Panthers -1.5"`).

### Truncating an outcome name loses the line

`"Sydney Roosters -1.5"` cut to 14 characters reads `"Sydney Rooster"`, which is outright bet on different market. `watch.col_name()` abbreviates team, keeps handicap. Anywhere else that shortens outcome for display must do same.

### A pairing that cannot be evaluated is not a pairing

`pair_matrix()` used to append `PairResult` even when `evaluate()` returned `None`, whose `margin_pct` is `-inf`. Terminal matrix rendered that as `-inf%` and counted it in "X of Y pairings work" denominator, while dashboard's `evaluatePair()` dropped it — so two engines disagreed about Y whenever book quoted only one side of market. Rare on head-to-head, routine on totals and spreads where books suspend one side. `pair_matrix()` now skips them. Guarded by `tests/test_pairs.py::TestUncoverablePairingsAreAbsent` and end to end by `tests/test_dual_engine.py`.

### Gating on the theoretical margin, printing the realised one

`MIN_MARGIN_PCT` used to be compared against `Opportunity.best_margin_pct`, which is `Arb.margin` — the pre-rounding figure. Every surface prints `Arb.realised_margin`, which is always the smaller of the two, so a floor of 0.5% produced alerts reading 0.47%. Read as the threshold being broken; it was the threshold measuring a different number than the one on screen.

Filter now lives in `scan.is_playable(opp, cfg)`, reads `Opportunity.realised_margin_pct`, and is shared by `alert.py`, `watch.py` and `serve.py` through `result["playable"]` so the three cannot disagree about what clears. Guarded by `tests/test_server.py::TestPlayableGate`.

Rounding loss is small at `TOTAL_STAKE = 1000` (1500 until 2026-08-23) — under 0.07 percentage points on the demo boards at 1500 — and grows as the stake falls, because the whole-dollar remainder is a larger share of a smaller position. Do not conclude from the demo gap that the distinction is cosmetic.

### Counting which books appear in arbitrages answers the wrong question

Ranking bookmakers by how often they show up in a crossing pairing credits a book for arbitrages another book would have supplied anyway. Ladbrokes and Neds are the standing case — both Entain, prices tracking each other (§11) — so a count marks both as valuable when either alone gives nearly the same result. The inverse also bites: a book appearing in few arbitrages can be the sole outlier price in them, and cutting it deletes those outright.

`study.py` measures what a subset **loses** instead: restrict the board to those books, re-run `analyse_game`, see what survives. That is why `record.py` stores the whole board including books that won no outcome — detections alone cannot answer the question after the fact, and the boards cannot be re-fetched.

Also: a standing arbitrage is re-logged every 30 minutes until it closes. `study.collapse()` folds boards to distinct `(event, market)` and scores the best each ever offered, because it is one bet. Summing rows pays the user thirty times for one opportunity and ranks slow markets far above their worth. Guarded by `tests/test_record.py::TestSubsetReplay::test_a_standing_arb_counts_once_however_often_it_is_logged`.

### `str.replace` with an empty match

Not code bug but process one: patching this repo by computing slice between two `str.index()` calls silently produced empty string when first anchor appeared in CSS comment before second. `s.replace("", x)` inserts between every character and produced 102 MB file. Prefer Edit tool with unique anchors over programmatic string surgery.

---

## 7. The dual-engine constraint

`static/dashboard.html` contains own JavaScript implementation of pairwise engine and staking algorithm, mirroring `arbtool/pairs.py` and `arbtool/core.py`.

Deliberate. `Opportunity.to_dict()` sends **odds and ages only** — never precomputed margins — so page derives every figure from prices it displays. What is on screen therefore cannot disagree with prices it came from.

Cost is duplication. **If you change one, change the other and re-verify**, or browser and terminal will silently diverge.

Verification no longer manual. `python3 tests/test_dual_engine.py` extracts real functions from shipped HTML — by walking braces from named function, never by slicing between string offsets, see §6 — and runs them over demo markets plus 600 random boards, comparing margins to 1e-6 and `is_arb` exactly. Needs `node` on PATH, skips without it. Random half earns its keep: includes books quoting only one side, which is where engines actually found to disagree.

`tests/e2e_server_browser.mjs` covers integration path but hardcodes playwright install that does not exist on every machine, so not the check that runs.

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

For lined market outcome names carry line — `"Over 50.5"`, `"Penrith Panthers -1.5"` — so no surface needs to know lines exist and no leg can be confirmed against wrong market in app. Line *not* sent as separate numeric field, same reason margins not sent: second copy of fact can drift from first.

No `pairs` key. Page computes them. See §7.

If fetch thread has not completed first poll, `/api/opportunities` returns 503 with `{"error": "warming up"}`. Dashboard treats any failure as "fall back to built-in demo payload" rather than blanking screen.

### `config.py` surface

Scripts read module-level constants; CLI flags override into `SimpleNamespace` copy. Anything reading config should accept `cfg` object rather than importing `config` directly, so flags work — `scan.py` follows this pattern, new code should too.

---

## 9. Testing policy

```bash
python3 tests/test_core.py        # 33 — arbitrage maths, staking, edge cases
python3 tests/test_pairs.py       # 19 — pairwise matrix, depth, the arb/is_arb trap
python3 tests/test_lines.py       # 23 — the sandwich guard and line grouping
python3 tests/test_server.py      # 21 — server, payload shape, in-play exclusion,
                                  #      the playable gate
python3 tests/test_alert.py       # 22 — Telegram suppression and formatting
python3 tests/test_bot.py         # 42 — reply bot, restake
python3 tests/test_record.py      # 13 — board log, subset replay
python3 tests/test_dual_engine.py #  1 — browser engine v Python, 600+ boards
python3 verify.py                 # independent verification (see below)
node tests/e2e_server_browser.mjs   # needs a server on :8792 AND a playwright
                                    # install it hardcodes — usually will not run
```

**After any change to `core.py`, `pairs.py` or `lines.py`, run `verify.py` and `tests/test_dual_engine.py`.** Not unit test suite. Re-derives results different way:

- headline numbers recomputed by hand in **exact fractions**, so float error cannot mask logic error
- `allocate()` brute-forced against exhaustive search over every possible whole-dollar split
- arbitrage detection cross-checked against exhaustive assignment search (every outcome to every book) over 2,000 random markets
- invariants over 3,000 random allocations: outlay never exceeds stake, no negative stakes, every stake whole multiple of increment

If you change maths, add test that would have caught change being wrong. Test that passes both before and after your change has not tested it.

---

## 10. Credit economics

Plan is $30 tier: **20,000 credits a month.** Cost is `[markets] × [regions]` per sport key that returns games. Empty responses free; `/sports` and `/events` cost nothing at all.

**`python3 books.py --sports` prices next poll for free, and is only number worth trusting** — active key count moves weekly with tennis, and it reads plan balance from response header rather than assuming tier. Per-sport market lists and full arithmetic live in `config.py` and `deploy/crontab.txt`, next to settings they describe.

Measured 2026-08-10: **11 keys, 19 credits a run, 32 in-window runs a day — about 18,240 a month, 91% of plan.** Deliberately close; unspent credits buy nothing.

Three things not obvious, each cost something:

- **`*/25` is not "every 25 minutes".** Cron reads `*/n` on minute field as "minutes 0–59 divisible by n", so fires at :00, :25, :50 — three times an hour, costing exactly what `*/20` costs. Coincide only when `n` divides 60. Anything counting runs must model this, which is why `books.py::runs_per_day` walks day a minute at a time.
- **Window lives in `config.SCAN_WINDOW_START/END`, not in cron**, so daylight saving is timezone database's problem and `books.py` can price month against same hours `alert.py` enforces. Two copies of hours is how credit estimate quietly stops describing what is being spent.
- **Headroom thin, cliff near.** 13 active keys puts current config over plan — two more live tennis tournaments does it. Run `books.py --sports` at start of each month; when it goes over, trim `SPORT_MARKETS` (WNBA first, then AFL totals). Guarded by `tests/test_alert.py::TestScanWindow`, which derives cost from `SPORT_MARKETS` rather than written-down number and fails before month does.

Before adding anything, multiply: one more market on one polled sport is +32 credits a day; one more market across all keys is +10,560 a month.

Productive window is couple hours before fixture, when books repricing and disagreement widest. That *not* how this tool used — which is days out — so size any advice against real pattern.

If plan outgrown: RapidOddsAPI's $49/month tier gives 200,000 credits with deeper AU coverage, against The Odds API's $59 for 100,000. Their $149 tier adds WebSocket, but pushes after each scrape cycle so removes polling latency, not capture latency — probably not worth it at this scale.

---

## 11. Domain notes worth carrying

**Ladbrokes and Neds are both Entain-owned**, prices track each other closely, so that pairing rarely disagrees. If user asks about improving hit rate, swapping one for Betfair Exchange or PointsBet is first suggestion.

**Tennis retirement rules differ between books** — some void, some pay out on whoever advances, some settle after one set. If two books differ, retirement turns hedged position into one-sided bet. Real risk specific to tennis, not modelled in code; documented in README.

**Bet365 AU** listed on The Odds API as paid-plans-only, h2h/spreads/totals, AFL and NRL — but does not deliver. Requested by name on paid key on 2026-08-09 it returned 8 NRL and 7 AFL fixtures with zero Bet365 prices on any of them. Not tier problem; all paid tiers list identical bookmaker access. Do not re-add on strength of documentation.

**Real exposure is one leg, not the stake.** If leg one fills and leg two has moved, user holds unhedged bet worth roughly half the total. Terminal cards, dashboard, README state this.

Telegram alert **deliberately does not**, at owner's request (2026-08-10). Reasoning: fixed warning repeated on every single message is skimmed past within a week, and alert already carries operative instruction in its CHECK BEFORE STAKING block — confirm both prices in both apps, do not place one leg on strength of message. `core.py` still exposes `unhedged_exposure()` and `tests/test_core.py` still guards it, so figure remains available to any surface that wants it.

---

## 12. Deliberately not built

Do not add these without being asked:

- **Automated bet placement.** No API access to AU books, failure modes severe. Tool advises; human places.
- **Additional markets** (spreads, totals). Would find more arbitrage but multiplies credit cost per poll — 3 markets is 3× the burn. Deliberate choice given free tier.
- **Persistence / history.** That is research repo's job (§1). **One scoped exception, live 2026-08-12:** `arbtool/record.py` logs every board to `data/boards.db` and `study.py` replays it. Not for a write-up — it answers an operational question with a cost attached: twelve funded accounts at `TOTAL_STAKE` each is $18,000 asleep, and the owner wants 5–6. Costs no credits, writes prices already fetched. Gated on `config.RECORD_BOARDS`; turn off once the cut is made. Do not grow it into outcome tracking or summary statistics — that is still the research repo.
- **Third-party dependencies.** Whole tool runs on standard library. Keep it that way unless strong reason.
- **Account or bankroll tracking.** Out of scope.

---

## 13. Style

Comments explain **why**, not what. Maths is load-bearing part — comment there should say what would go wrong if line were different.

Money figures lead with dollars; percentages secondary. `$74.56` first, `4.99%` as supporting detail. User reasons in cash.

Prose in docstrings and user-facing strings should be precise about what is guaranteed versus expected. "Guaranteed profit" means guaranteed *after* rounding, assuming both legs fill. Say so.

No emoji in output. Terminal cards use box-drawing characters and ANSI colour, degrade cleanly when piped (`C.off()` when not a TTY).