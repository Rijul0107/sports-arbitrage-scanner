#!/usr/bin/env python3
"""
export_site.py — build a static, zero-setup copy of the dashboard.

    python3 export_site.py --db data/boards.db --out docs

Writes docs/index.html and docs/data.json. Publish docs/ with GitHub Pages and
the dashboard works for anyone with a browser: no Python, no API key, no
credits, no server.

Why a static export rather than a hosted demo
---------------------------------------------
The live dashboard is a Python process that holds the API key and proxies The
Odds API, because that API sends no CORS headers and a browser cannot call it
directly — and because putting a key in client-side JavaScript publishes it.
None of that can be deployed to Pages, which serves files and nothing else.

So the export takes the other half of the value: the page, and a real snapshot
of what the market looked like. The data is not fabricated. It is the highest-
margin opportunities the scanner actually recorded between 2026-08-12 and
2026-08-29, replayed out of the board log with the prices that were live at the
time. A reader gets the genuine article; they simply cannot press Fetch.

The page needs no fork. dashboard.html asks the server first and falls back to
./data.json, so one file serves both modes and there is no second copy to drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
from arbtool.record import connect
from arbtool.scan import commission_map

HERE = Path(__file__).parent


def rows_to_games(conn, limit: int):
    """The best recorded opportunities, in the shape Opportunity.to_dict emits.

    Read straight from the stored columns rather than by rebuilding
    Opportunity objects: every field to_dict sends — outcomes, odds, ages,
    fixture, market label — is already a column, and reconstructing the objects
    would mean re-running the engine to produce data the log already holds.

    Ordered by realised margin, so the export shows the strategy working rather
    than a random slice of a market that is uncrossed 98.7% of the time. That
    is a presentation choice and the page says so on its face.
    """
    # scanned_at lives on scans, not boards, so the join is required rather
    # than cosmetic.
    sql = (
        "SELECT b.event_id, b.sport_title, b.home, b.away, b.minutes_to_start,"
        "       b.market_key, b.market_label, b.outcomes_json, b.odds_json,"
        "       b.ages_json, b.realised_margin_pct, s.scanned_at"
        "  FROM boards b JOIN scans s ON s.id = b.scan_id"
        " WHERE b.playable = 1 AND b.realised_margin_pct BETWEEN 0 AND 10"
        " ORDER BY b.realised_margin_pct DESC LIMIT ?"
    )
    games, seen = [], set()
    for r in conn.execute(sql, (limit * 4,)):
        key = (r["event_id"], r["market_key"], r["market_label"])
        if key in seen:  # one row per market, best price kept
            continue
        seen.add(key)
        try:
            outcomes = json.loads(r["outcomes_json"])
            odds = json.loads(r["odds_json"])
            ages = json.loads(r["ages_json"] or "{}")
        except (TypeError, ValueError):
            continue
        games.append(
            {
                "id": f"{r['event_id']}:{r['market_key']}:{r['market_label']}",
                "sport": r["sport_title"],
                "home": r["home"],
                "away": r["away"],
                "starts_in_min": round(r["minutes_to_start"] or 0),
                "market": r["market_label"],
                "outcomes": outcomes,
                "odds": odds,
                "ages": {
                    b: (round(a) if isinstance(a, (int, float)) else None)
                    for b, a in (ages or {}).items()
                },
            }
        )
        if len(games) >= limit:
            break
    return games


def build(db_path, out_dir: Path, limit: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    with connect(db_path) as conn:
        games = rows_to_games(conn, limit)
        span = conn.execute(
            "SELECT min(scanned_at), max(scanned_at) FROM scans"
        ).fetchone()
        totals = conn.execute("SELECT count(*) FROM boards").fetchone()[0]

    if not games:
        print("No playable boards in the log — nothing to export.")
        return 1

    # Every book that appears in the exported snapshot, not config.BOOKS: the
    # export spans an era when twelve were configured, and a page told about
    # seven would render blank columns for the other five.
    books = sorted({b for g in games for b in g["odds"]})

    payload = {
        "static_export": True,
        "generated_from": {
            "window": [span[0], span[1]],
            "boards_logged": totals,
            "note": (
                "Real odds recorded by the scanner in this window. "
                "No bets were placed. Sorted by margin, so this is the "
                "best of what was found, not a typical minute."
            ),
        },
        "books": books,
        "commission": commission_map(config),
        "credits_remaining": None,
        "credits_spent": 0,
        "fetched_at": span[1],
        "poll_cost": 0,
        "auto": False,
        "games": games,
    }

    (out_dir / "data.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    # The served dashboard is genuinely live; this copy is a recorded snapshot,
    # and the browser tab is the one place that distinction is visible before a
    # reader has scrolled anywhere. Retitle it rather than shipping a page that
    # calls itself live when it cannot fetch.
    page = (HERE / "static" / "dashboard.html").read_text(encoding="utf-8")
    page = page.replace(
        "<title>Arb Desk — live</title>",
        "<title>Arb Desk — recorded snapshot</title>",
        1,
    )
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    # Pages runs files through Jekyll by default, which strips paths beginning
    # with an underscore and can mangle templating-like braces. .nojekyll turns
    # that off; without it the export can render locally and break once hosted.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Wrote {out_dir}/index.html and {out_dir}/data.json")
    print(
        f"  {len(games)} opportunities, {len(books)} books, "
        f"{span[0][:10]} to {span[1][:10]}"
    )
    print(f"  Preview: python3 -m http.server -d {out_dir} 8000")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=None)
    p.add_argument("--out", default="docs")
    p.add_argument("--limit", type=int, default=40)
    args = p.parse_args(argv)
    return build(args.db, Path(args.out), args.limit)


if __name__ == "__main__":
    sys.exit(main())
