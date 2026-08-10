#!/usr/bin/env python3
"""
serve.py — run the dashboard.

    python3 serve.py            # live, opens the dashboard in your browser
    python3 serve.py --demo     # fabricated odds, no API key, no credits
    python3 serve.py --port 9000

Why a server rather than just opening the HTML file: a browser will not let a
web page call a third-party API directly unless that API opts in with CORS
headers, and The Odds API does not. This process does the fetching in Python,
where no such restriction exists, and hands the result to the page. It also
means your API key never enters the browser.

Cost: the page refreshes every 60 seconds and each refresh costs 1 credit per
sport in season. On the free tier's 500/month that is roughly four hours of
having this open. The server enforces --budget and stops fetching when reached,
serving the last good snapshot instead of burning through your month.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from arbtool.api import BudgetExceeded, OddsAPI, OddsAPIError, QuotaExhausted
from arbtool.scan import commission_map, scan

STATIC = Path(__file__).parent / "static"

# Shared between the fetch thread and the request handlers.
_state = {
    "payload": None,          # last good snapshot
    "error": None,
    "fetched_at": None,
    "stopped": False,
    "api": None,              # set in main(); needed by the on-demand poll route
    "cfg": None,
    "auto": False,            # True only when --auto was passed
}
_lock = threading.Lock()

# Held for the duration of an upstream poll. Serving the cached snapshot is free
# and must never block on it; spending credits must never happen twice at once.
_poll_lock = threading.Lock()


def build_payload(result, api, cfg) -> dict:
    return {
        "books": list(cfg.BOOKS),
        # Commission rates, not margins. This is an input the page needs to do
        # the same arithmetic the server does — without it the dashboard would
        # rank Betfair on its raw price and overstate the profit, which is the
        # exact divergence CLAUDE.md §7 exists to prevent. Sending a derived
        # figure would still be forbidden; sending the rate is what makes the
        # page able to derive it.
        "commission": commission_map(cfg),
        "credits_remaining": (api.credits.remaining_reported
                              if api else None),
        "credits_spent": api.credits.spent_this_session if api else 0,
        "fetched_at": result["scanned_at"],
        # What the next poll will cost, so the page can price the button rather
        # than making the user guess. One credit per active sport key.
        "poll_cost": len(result.get("sports") or []),
        "auto": bool(_state.get("auto")),
        "games": [o.to_dict() for o in result["opportunities"]],
    }


def poll_once(api, cfg) -> dict:
    """Spend credits: one upstream scan, storing the result as the new snapshot.

    Returns {"ok": bool, "error": str|None, "spent": int, ...} rather than
    raising, because this is called straight from a request handler and a
    failed poll must not take the server down or blank an existing snapshot.

    Serialised on _poll_lock: two browser tabs pressing the button together
    would otherwise each spend a full poll's worth of credits.
    """
    with _poll_lock:
        before = api.credits.spent_this_session
        try:
            result = scan(api, cfg)
        except BudgetExceeded as e:
            with _lock:
                _state["error"] = "budget"
            return {"ok": False, "error": f"session budget reached: {e}", "spent": 0}
        except QuotaExhausted as e:
            with _lock:
                _state["error"] = "quota"
            return {"ok": False, "error": f"plan quota exhausted: {e}", "spent": 0}
        except OddsAPIError as e:
            with _lock:
                _state["error"] = str(e)
            return {"ok": False, "error": f"{e.kind}: {e}", "spent": 0}

        payload = build_payload(result, api, cfg)
        with _lock:
            _state["payload"] = payload
            _state["error"] = None
            _state["fetched_at"] = datetime.now(timezone.utc)
        spent = api.credits.spent_this_session - before
        print(f"  [{datetime.now():%H:%M:%S}] polled on request · "
              f"{len(result['opportunities'])} games · {len(result['playable'])} playable "
              f"· {spent} credit(s) · {api.credits.summary()}")
        return {"ok": True, "error": None, "spent": spent,
                "games": len(result["opportunities"]),
                "playable": len(result["playable"])}


def fetch_loop(api, cfg, interval: int, once: bool = False):
    """Poll in the background so the page never waits on the network."""
    while not _state["stopped"]:
        try:
            result = scan(api, cfg)
            payload = build_payload(result, api, cfg)
            with _lock:
                _state["payload"] = payload
                _state["error"] = None
                _state["fetched_at"] = datetime.now(timezone.utc)
            n = len(result["playable"])
            print(f"  [{datetime.now():%H:%M:%S}] {len(result['opportunities'])} games · "
                  f"{n} playable · {api.credits.summary()}")
        except BudgetExceeded as e:
            print(f"\n  Credit budget reached. {e}")
            print("  Serving the last snapshot; restart with a higher --budget to resume.")
            with _lock:
                _state["error"] = "budget"
            return
        except QuotaExhausted as e:
            print(f"\n  Plan quota exhausted: {e}")
            with _lock:
                _state["error"] = "quota"
            return
        except OddsAPIError as e:
            print(f"  fetch failed ({e.kind}): {e}")
            with _lock:
                _state["error"] = str(e)
        if once:
            return
        for _ in range(interval):
            if _state["stopped"]:
                return
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                    # keep the console for our own output

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Spends credits. Only ever reached by an explicit press of the fetch
        # button, never by the page's own refresh timer — that reads the cached
        # snapshot below, which is free.
        if self.path.startswith("/api/poll"):
            api, cfg = _state["api"], _state["cfg"]
            if api is None:
                self._send(400, json.dumps(
                    {"ok": False, "error": "demo mode — no API key, nothing to poll"}
                ).encode(), "application/json")
                return
            res = poll_once(api, cfg)
            with _lock:
                payload = _state["payload"]
            res["payload"] = payload
            self._send(200 if res["ok"] else 502,
                       json.dumps(res).encode(), "application/json")
            return

        if self.path.startswith("/api/opportunities"):
            with _lock:
                payload = _state["payload"]
                err = _state["error"]
            if payload is None:
                self._send(503, json.dumps({"error": err or "warming up"}).encode(),
                           "application/json")
                return
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        path = "/dashboard.html" if self.path in ("/", "") else self.path
        f = (STATIC / path.lstrip("/")).resolve()
        # never serve outside static/
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css",
                 "js": "application/javascript"}.get(f.suffix.lstrip("."), "text/plain")
        self._send(200, f.read_bytes(), ctype)


def demo_payload() -> dict:
    """The dashboard already ships a demo dataset; serving no API payload makes
    it fall back to that. This just keeps the endpoint honest."""
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demo", action="store_true",
                   help="serve the page with its built-in fabricated data")
    p.add_argument("--port", type=int, default=config.PORT)
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--auto", action="store_true",
                   help="poll on a timer. Off by default: without it nothing "
                        "reaches the API until you press FETCH ODDS")
    p.add_argument("--interval", type=int, default=config.POLL_SECONDS,
                   help="seconds between polls, --auto only")
    p.add_argument("--budget", type=int, default=config.SESSION_CREDIT_BUDGET)
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = p.parse_args()

    url = f"http://{args.host}:{args.port}/"

    api = None
    if not args.demo:
        if not config.API_KEY:
            print("No API key. Either:")
            print("    export ODDS_API_KEY='your_key'   (free at the-odds-api.com)")
            print("  or run the dashboard on fabricated data:")
            print("    python3 serve.py --demo")
            return 2
        api = OddsAPI(config.API_KEY, session_budget=args.budget,
                      min_plan_credits=config.MIN_PLAN_CREDITS)
        _state["api"] = api
        _state["cfg"] = config
        _state["auto"] = args.auto
        print(f"Arb Desk — live")
        print(f"  books     {', '.join(config.BOOKS)}")
        print(f"  stake     ${config.TOTAL_STAKE:,.0f} · min profit ${config.MIN_PROFIT:,.0f}")
        print(f"  budget    {args.budget} credits this session")
        print(f"  pre-match only (online in-play betting is prohibited in Australia)")
        if args.auto:
            print(f"  AUTO polling every {args.interval}s — this spends credits "
                  f"on a timer, whether or not anyone is watching")
            t = threading.Thread(target=fetch_loop,
                                 args=(api, config, args.interval), daemon=True)
            t.start()
        else:
            # Default. Nothing reaches the upstream API until the fetch button is
            # pressed, so leaving this open overnight costs nothing.
            print(f"  MANUAL — no credits are spent until you press FETCH ODDS")
            print(f"           (pass --auto to poll on a timer instead)")
    else:
        print("Arb Desk — demo mode. Fabricated odds, no API key, no credits spent.")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  {url}\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        _state["stopped"] = True
        if api:
            print(f"  {api.credits.summary()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
