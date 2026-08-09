"""Independent verification of the arbitrage maths.

Deliberately re-derives results a different way from the implementation, so a
shared bug is less likely to hide. Nothing here imports the staking logic to
check the staking logic.
"""
from fractions import Fraction as F
from itertools import combinations, product
import random, sys
from arbtool.core import evaluate, allocate, Arb
from arbtool.pairs import analyse_game

FAIL = []
def check(name, cond, detail=""):
    (print(f"  ok   {name}") if cond else (print(f"  FAIL {name}  {detail}"), FAIL.append(name)))

print("=" * 72)
print("1. HEADLINE NUMBERS FROM THE SCREENSHOT, RE-DERIVED BY HAND (exact fractions)")
print("=" * 72)
# Use exact rational arithmetic so float error cannot mask an error in the logic.
cases = [
    ("Penrith v Storm",  "Sportsbet", F(212,100), "Neds", F(208,100), 743, 757, F("74.56")),
    ("Swiatek v Sabalenka","Sportsbet", F(210,100), "Neds", F(198,100), 728, 772, F("28.56")),
    ("Alcaraz v Sinner", "TAB",       F(195,100), "Neds", F(206,100), 771, 729, F("1.74")),
]
for name, b1, o1, b2, o2, s1, s2, expect_profit in cases:
    S = 1/o1 + 1/o2
    margin = (1/S - 1) * 100
    r1, r2 = s1*o1, s2*o2
    worst = min(r1, r2)
    profit = worst - (s1+s2)
    print(f"\n  {name}")
    print(f"    S = 1/{float(o1):.2f} + 1/{float(o2):.2f} = {float(S):.9f}")
    print(f"    margin = 1/S - 1 = {float(margin):.4f}%")
    print(f"    {s1}x{float(o1):.2f} = {float(r1):.2f} | {s2}x{float(o2):.2f} = {float(r2):.2f}")
    print(f"    outlay {s1+s2}, worst return {float(worst):.2f}, profit {float(profit):.2f}")
    check(f"{name}: profit matches screen ({float(expect_profit):.2f})",
          profit == expect_profit, f"got {float(profit):.4f}")
    check(f"{name}: S < 1 so it is a real arb", S < 1)
    check(f"{name}: outlay does not exceed 1500", s1+s2 <= 1500)

print()
print("=" * 72)
print("2. IS allocate() ACTUALLY OPTIMAL? brute force every whole-dollar split")
print("=" * 72)
def brute_best(odds, total, inc=1):
    """Exhaustive search over 2-leg whole-dollar splits."""
    n = int(total/inc)
    best, arg = None, None
    for k in range(n+1):
        s1, s2 = k*inc, total - k*inc
        w = min(s1*odds[0], s2*odds[1])
        p = w - total
        if best is None or p > best:
            best, arg = p, (s1, s2)
    return best, arg

random.seed(11)
worst_gap = 0.0
tested = 0
for _ in range(400):
    o1 = round(random.uniform(1.4, 4.0), 2)
    o2 = round(random.uniform(1.4, 4.0), 2)
    if 1/o1 + 1/o2 >= 1:            # only test genuine arbs
        continue
    total = random.choice([100, 150, 500, 1000, 1500, 5000])
    arb = evaluate({"A": {"X": o1, "Y": 1.01}, "B": {"X": 1.01, "Y": o2}})
    allocate(arb, total, increment=1.0)
    mine = arb.worst_profit
    best, arg = brute_best([o1, o2], total, 1)
    gap = best - mine
    worst_gap = max(worst_gap, gap)
    tested += 1
check(f"allocate() is optimal on all {tested} random 2-leg arbs",
      worst_gap < 1e-9, f"worst shortfall ${worst_gap:.4f}")
print(f"    tested {tested} arbs; largest gap vs exhaustive search ${worst_gap:.6f}")

print()
print("=" * 72)
print("3. PROPERTIES THAT MUST HOLD FOR EVERY ALLOCATION")
print("=" * 72)
random.seed(23)
viol_outlay = viol_neg = viol_sum = 0
n = 0
for _ in range(3000):
    o1 = round(random.uniform(1.2, 6.0), 2)
    o2 = round(random.uniform(1.2, 6.0), 2)
    total = random.choice([37, 100, 150, 743, 1500, 9999])
    inc = random.choice([0.01, 0.5, 1.0, 5.0])
    arb = evaluate({"A": {"X": o1, "Y": 1.01}, "B": {"X": 1.01, "Y": o2}})
    if arb is None: continue
    allocate(arb, total, increment=inc)
    n += 1
    if arb.total_stake > total + 1e-9: viol_outlay += 1
    if any(l.stake < -1e-9 for l in arb.legs.values()): viol_neg += 1
    # every leg's stake must be a whole multiple of the increment
    for l in arb.legs.values():
        if abs(l.stake/inc - round(l.stake/inc)) > 1e-6: viol_sum += 1
check(f"outlay never exceeds the stake ({n} cases)", viol_outlay == 0, f"{viol_outlay} violations")
check("no negative stakes", viol_neg == 0, f"{viol_neg} violations")
check("every stake is a whole multiple of the increment", viol_sum == 0, f"{viol_sum} violations")

print()
print("=" * 72)
print("4. ARB DETECTION CROSS-CHECKED AGAINST A DIFFERENT ALGORITHM")
print("=" * 72)
def independent_is_arb(book_odds):
    """Brute force: try every assignment of outcomes to books, see if any
    combination returns more than it costs on a unit-payout basis."""
    outcomes = sorted({o for p in book_odds.values() for o in p})
    books = sorted(book_odds)
    best_S = None
    for assign in product(books, repeat=len(outcomes)):
        try:
            S = sum(1/book_odds[assign[i]][o] for i, o in enumerate(outcomes))
        except KeyError:
            continue
        if best_S is None or S < best_S:
            best_S = S
    return best_S

random.seed(31)
mismatch = 0
for _ in range(2000):
    books = {f"B{i}": {"X": round(random.uniform(1.3,3.5),2),
                       "Y": round(random.uniform(1.3,3.5),2)} for i in range(random.randint(2,5))}
    arb = evaluate(books)
    ref_S = independent_is_arb(books)
    if arb is None: continue
    if abs(arb.inverse_sum - ref_S) > 1e-9 or (arb.is_arb != (ref_S < 1)):
        mismatch += 1
check("greedy best-price equals exhaustive assignment search (2000 markets)",
      mismatch == 0, f"{mismatch} mismatches")

print()
print("=" * 72)
print("5. PAIRWISE MATRIX CONSISTENCY")
print("=" * 72)
BOOKS = ["Sportsbet","Ladbrokes","TAB","Neds"]
G = {"Sportsbet":{"Penrith Panthers":2.12,"Melbourne Storm":1.78},
     "Ladbrokes":{"Penrith Panthers":1.95,"Melbourne Storm":1.92},
     "TAB":{"Penrith Panthers":1.85,"Melbourne Storm":1.98},
     "Neds":{"Penrith Panthers":1.80,"Melbourne Storm":2.08}}
ga = analyse_game(G, "h2h", BOOKS)
check("6 pairings for 4 books", len(ga.pairs) == 6, f"got {len(ga.pairs)}")
check("pairs sorted best first",
      all(ga.pairs[i].margin_pct >= ga.pairs[i+1].margin_pct for i in range(len(ga.pairs)-1)))
check("best pair margin equals all-books best price",
      abs(ga.pairs[0].margin_pct - ga.best_overall.margin*100) < 1e-9,
      f"{ga.pairs[0].margin_pct} vs {ga.best_overall.margin*100}")
# every pair recomputed by hand
handok = True
for p in ga.pairs:
    o1 = max(G[p.book_a]["Penrith Panthers"], G[p.book_b]["Penrith Panthers"])
    o2 = max(G[p.book_a]["Melbourne Storm"], G[p.book_b]["Melbourne Storm"])
    m = (1/(1/o1 + 1/o2) - 1)*100
    if abs(m - p.margin_pct) > 1e-9: handok = False
check("every pair margin matches hand calculation", handok)

print()
print("=" * 72)
print("6. THE CASE THAT MUST NOT BE CALLED AN ARB")
print("=" * 72)
# One book best on both sides: not placeable, must not be reported as a pair arb
solo = {"Solo": {"X": 2.10, "Y": 2.10}, "Other": {"X": 1.50, "Y": 1.50}}
a = evaluate(solo)
check("single-book dominance flagged", a.single_book)
gp = analyse_game(solo, "h2h", ["Solo","Other"])
check("single-book pairing not counted as arbitrage",
      not gp.pairs[0].is_arb, f"is_arb={gp.pairs[0].is_arb}")
check("...even though the raw maths says S<1", a.inverse_sum < 1)
# exactly 100%
fair = evaluate({"A": {"X": 2.0}, "B": {"Y": 2.0}})
check("exactly 100% is not an arb", not fair.is_arb)

print()
print("=" * 72)
print(f"{'ALL CHECKS PASSED' if not FAIL else str(len(FAIL)) + ' CHECK(S) FAILED: ' + ', '.join(FAIL)}")
print("=" * 72)
sys.exit(1 if FAIL else 0)
