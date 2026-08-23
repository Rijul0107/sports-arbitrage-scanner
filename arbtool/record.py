"""Append-only log of every board a scan looked at.

CLAUDE.md §12 says persistence belongs to the research repo, and that still
holds for anything serving a write-up. This is not that. It exists to answer an
operational question with a cost attached: which bookmakers earn the float
sitting in their accounts, and which can be closed. Twelve funded accounts at
TOTAL_STAKE each is a lot of money asleep, and the only honest way to decide
which to keep is to watch what the books actually do for a fortnight.

WHAT IS STORED, AND WHY IT IS THE WHOLE BOARD

Not the arbitrages — every board, crossing or not, with every book's price on
every outcome. Storing only the detections would answer "which books appeared
in an arb", which is the wrong question and gives a confidently wrong answer:

  - A book in many arbs can still be redundant. If another book prices within a
    tick of it every time, cutting it loses almost nothing, because the pairing
    that crossed would have crossed anyway through its neighbour.
  - A book in few arbs can be irreplaceable. If it holds the lone outlier price
    in the ones it does appear in, cutting it deletes those arbs outright and
    nothing else covers them.

Both cases need the prices of the books that did NOT participate, so the
question can be re-asked of any subset. With full boards, choosing 6 of 12 is a
replay over stored prices — see study.py, which brute-forces all 924 subsets
through the real engine rather than reasoning about counts.

WHAT IS NOT STORED

Margins, stakes and profits for the subset case. Those are derived at replay
time from the odds, for the same reason Opportunity.to_dict() sends no margins
(CLAUDE.md §7): a second copy of a fact drifts from the first. The margin
columns here describe the ALL-BOOKS case only, and exist so a board can be
found without recomputing it — never as the input to a subset decision.

The thresholds and stake in force are copied onto every scan row. They change —
TOTAL_STAKE moved 2000 to 1500 and MIN_MARGIN_PCT 0.5 to 1.0 on 2026-08-12,
MIN_MARGIN_PCT 1.0 to 2.0 on 2026-08-16, TOTAL_STAKE 1500 to 1000 on
2026-08-23, all mid-collection — and a profit figure whose stake is unknown
cannot be compared against one from a different week.

That the stake is recorded is what keeps study.py honest, but it does not make
the weeks equal. study.py collapses a market to the best it ever offered across
the whole window, so an edge seen only after 2026-08-23 is scored on a $1000
position while an identical edge from the first fortnight is scored on $1500.
Rank books on the window that shares a stake, or read the totals as ordinal,
not as dollars earned.

COST

No API credits. This writes what the scan already fetched and paid for. The
only cost is disk: roughly one row per market per run, a few hundred KB a day.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Optional

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "boards.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at        TEXT    NOT NULL,
    sports_json       TEXT    NOT NULL,
    events_seen       INTEGER NOT NULL,
    markets_seen      INTEGER NOT NULL,
    credits_spent     INTEGER,
    credits_remaining INTEGER,
    -- The settings this scan ran under. Copied per scan, not read from config
    -- at analysis time, because they change mid-collection.
    stake             REAL    NOT NULL,
    min_profit        REAL    NOT NULL,
    min_margin_pct    REAL    NOT NULL,
    stake_increment   REAL    NOT NULL,
    books_json        TEXT    NOT NULL,
    commission_json   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS boards (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id           INTEGER NOT NULL REFERENCES scans(id),
    event_id          TEXT    NOT NULL,
    sport_key         TEXT    NOT NULL,
    sport_title       TEXT,
    home              TEXT,
    away              TEXT,
    commence_time     TEXT,
    minutes_to_start  REAL,
    market_key        TEXT    NOT NULL,
    market_label      TEXT    NOT NULL DEFAULT '',
    outcomes_json     TEXT    NOT NULL,
    -- {book: {outcome: price}} — the whole board, including books that lost
    -- every outcome. This is the column the subset replay reads.
    odds_json         TEXT    NOT NULL,
    ages_json         TEXT    NOT NULL,
    -- All-books case only. For finding a board, not for deciding a subset.
    best_margin_pct     REAL,
    realised_margin_pct REAL,
    profit              REAL,
    placeable           INTEGER NOT NULL,
    playable            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS boards_scan   ON boards(scan_id);
CREATE INDEX IF NOT EXISTS boards_event  ON boards(event_id, market_key, market_label);
CREATE INDEX IF NOT EXISTS boards_sport  ON boards(sport_key);
"""


@contextmanager
def connect(db_path=None):
    """Open the log, creating it and its directory on first use.

    WAL so a long analysis query cannot block the cron run that is trying to
    append to it — the two touch this file on different schedules and neither
    should ever wait on the other."""
    path = Path(db_path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _finite(x) -> Optional[float]:
    """None for inf/-inf/NaN.

    realised_margin_pct is -inf on an unstaked board by design, and SQLite
    stores that as a float that every later comparison silently loses to."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def record_scan(result: Dict, cfg, credits=None, db_path=None) -> Optional[int]:
    """Append one scan and its boards. Returns the scan id, or None on failure.

    Never raises. A logging fault must not stop an alert going out — the money
    is in the alert, and the data is only worth collecting because the alert
    keeps working while it happens. Caller prints the reason and carries on."""
    try:
        opportunities = list(result.get("opportunities") or ())
        playable_ids = {id(o) for o in (result.get("playable") or ())}

        with connect(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO scans (scanned_at, sports_json, events_seen,"
                " markets_seen, credits_spent, credits_remaining, stake,"
                " min_profit, min_margin_pct, stake_increment, books_json,"
                " commission_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (result.get("scanned_at", ""),
                 json.dumps([s.get("key") for s in result.get("sports") or ()]),
                 int(result.get("events_seen") or 0),
                 len(opportunities),
                 getattr(credits, "spent_this_session", None),
                 getattr(credits, "remaining_reported", None),
                 float(cfg.TOTAL_STAKE), float(cfg.MIN_PROFIT),
                 float(cfg.MIN_MARGIN_PCT), float(cfg.STAKE_INCREMENT),
                 json.dumps(list(cfg.BOOKS)),
                 json.dumps(getattr(cfg, "BOOK_COMMISSION_PCT", {}) or {})),
            )
            scan_id = cur.lastrowid

            conn.executemany(
                "INSERT INTO boards (scan_id, event_id, sport_key, sport_title,"
                " home, away, commence_time, minutes_to_start, market_key,"
                " market_label, outcomes_json, odds_json, ages_json,"
                " best_margin_pct, realised_margin_pct, profit, placeable,"
                " playable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [_row(scan_id, o, id(o) in playable_ids) for o in opportunities],
            )
        return scan_id
    except Exception as ex:                       # noqa: BLE001 — see docstring
        print(f"  board log failed ({type(ex).__name__}: {ex}) — scan unaffected")
        return None


def _row(scan_id: int, opp, playable: bool) -> tuple:
    return (
        scan_id,
        opp.event_id, opp.sport_key, opp.sport_title,
        opp.home, opp.away, opp.commence_time,
        _finite(opp.minutes_to_start),
        opp.market_key, opp.market_label or "",
        json.dumps(list(opp.analysis.outcomes)),
        json.dumps(opp.analysis.book_odds),
        json.dumps(opp.ages or {}),
        _finite(opp.best_margin_pct),
        _finite(opp.realised_margin_pct),
        _finite(opp.profit),
        1 if opp.placeable else 0,
        1 if playable else 0,
    )


def summary(db_path=None) -> Dict:
    """Counts, for a one-line progress report while collection runs."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM scans)  AS scans,"
            "       (SELECT MIN(scanned_at) FROM scans) AS first,"
            "       (SELECT MAX(scanned_at) FROM scans) AS last,"
            "       (SELECT COUNT(*) FROM boards) AS boards,"
            "       (SELECT COUNT(*) FROM boards WHERE placeable=1) AS crossed,"
            "       (SELECT COUNT(*) FROM boards WHERE playable=1)  AS alertable"
        ).fetchone()
        return dict(row)
