/* Runs the dashboard's own engine over a payload, for tests/test_dual_engine.py.
 *
 * static/dashboard.html carries a second implementation of the pairwise engine
 * (ARCHITECTURE.md §7). It exists so the page can recompute every figure from the
 * prices it displays, and its cost is that two implementations must agree. This
 * extracts the real functions from the shipped page — not a copy that could
 * drift — and prints what they produce.
 *
 *   node tests/parity_engine.mjs <dashboard.html> <payload.json> <commission.json>
 */
import { readFileSync } from 'node:fs';

const html = readFileSync(process.argv[2], 'utf8');

/* Extracted by walking braces from a named function, not by slicing between two
   string offsets. ARCHITECTURE.md §6 records what the offset approach did to this
   repo once: an anchor matched inside a CSS comment and the "patch" produced a
   102 MB file. */
function grab(name) {
  const start = html.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`no function ${name} in the page`);
  let depth = 0;
  for (let j = html.indexOf('{', start); j < html.length; j++) {
    if (html[j] === '{') depth++;
    else if (html[j] === '}' && --depth === 0) return html.slice(start, j + 1);
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const commission = JSON.parse(process.argv[4] || '{}');
const engine = new Function(`
  let COMMISSION = ${JSON.stringify(commission)};
  const commissionFor = b => Number(COMMISSION[b] || 0);
  const effOdds = (odds, book) => 1 + (odds - 1) * (1 - commissionFor(book) / 100);
  ${['evaluatePair', 'computePairs'].map(grab).join('\n')}
  return { computePairs };
`)();

const data = JSON.parse(readFileSync(process.argv[3], 'utf8'));
console.log(JSON.stringify(data.games.map(g => ({
  id: g.id,
  pairs: engine.computePairs(g, data.books).map(p => ({
    books: [p.a, p.b].sort(),
    margin: p.margin,
    arb: !!p.arb,
  })),
}))));
