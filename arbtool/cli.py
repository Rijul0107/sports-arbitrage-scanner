"""
One entry point for the whole tool: `arb <command>`.

Before this there were eight scripts in the repo root, each with its own
argparse and its own idea of what a flag was called. That is fine when you are
the only person who runs them and you wrote them last week. It is hostile to
anyone else, and it made the README a list of file paths rather than a list of
things the tool does.

Each subcommand delegates to the module that already owns the behaviour rather
than reimplementing it — dispatch only, no logic. Two reasons. The scripts stay
runnable on their own, which matters because the deployed cron calls
`python3 alert.py` directly and should not gain a dependency on this package
being installed. And a second implementation of the staking or scanning path
would be the dual-engine problem the repo already warns about: two code paths
that agree today, disagree after one is edited, and disagree silently.
"""

from __future__ import annotations

import argparse
import importlib
import sys

#: command -> (module, callable, one-line help). Imported lazily inside main()
#: so that `arb --help` does not pay for sqlite, urllib and the config file, and
#: so a broken optional module cannot stop the other commands from listing.
COMMANDS = {
    "scan":     ("watch",    "main", "one-off scan of the live board, printed as a table"),
    "watch":    ("watch",    "main", "poll continuously and print opportunities as they appear"),
    "alert":    ("alert",    "main", "scan once and send any opportunity to Telegram (cron entry point)"),
    "bot":      ("bot",      "main", "long-running Telegram bot that answers replies to alerts"),
    "serve":    ("serve",    "main", "local dashboard on http://127.0.0.1:8787"),
    "books":    ("books",    "main", "list bookmaker titles and price the next poll"),
    "season":   ("season",   "main", "check which sports are in season — costs no credits"),
    "study":    ("study",    "main", "replay logged boards under each candidate book subset"),
    "backtest": ("backtest", "main", "simulate a bankroll over the logged boards"),
    "verify":   ("verify",   "main", "prove the staking maths against known cases"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arb",
        description="Sports betting arbitrage scanner, logger and backtester.",
        epilog="Run `arb <command> --help` for a command's own options.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", metavar="<command>")
    for name, (_, _, help_text) in COMMANDS.items():
        # add_help=False and no arguments: everything after the command name is
        # forwarded untouched to the underlying script's own parser, so this
        # file never has to mirror their flags or go stale when one changes.
        sub.add_parser(name, help=help_text, add_help=False)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0
    if argv[0] in ("-V", "--version"):
        print("arb-desk 1.0.0")
        return 0

    command, rest = argv[0], argv[1:]
    if command not in COMMANDS:
        build_parser().print_help()
        print(f"\nUnknown command: {command!r}", file=sys.stderr)
        return 2

    module_name, func_name, _ = COMMANDS[command]
    module = importlib.import_module(module_name)
    func = getattr(module, func_name)

    # Some of these were written as `main()` with no parameter and read
    # sys.argv themselves. Support both rather than editing six working
    # scripts to satisfy this one.
    try:
        return func(rest) or 0
    except TypeError:
        saved, sys.argv = sys.argv, [module_name] + rest
        try:
            return func() or 0
        finally:
            sys.argv = saved


if __name__ == "__main__":
    sys.exit(main())
