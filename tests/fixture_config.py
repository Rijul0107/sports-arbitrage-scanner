"""A frozen economic config for tests whose fixtures must clear the gates.

Why this exists
---------------
On 2026-08-16 `config.TOTAL_STAKE` moved 1500 -> 1000 in production. Eighteen
tests in test_alert.py began failing with `IndexError: list index out of range`,
and the reason was not a bug in the code under test: profit scales with stake,
the demo fixture's best opportunity fell from $11.04 to $7.00, `MIN_PROFIT` is
$10, so `playable()` returned an empty list and every test indexing `[0]` blew
up. A pricing decision took out a fifth of the suite and said nothing about
whether the alerting logic was correct.

Tests that need a fixture to be *actionable* must therefore pin the economics
they were written against, rather than reading whatever the live file says
today. The values below are exactly the ones the demo fixtures in watch.py were
authored under.

This does NOT apply to every test that touches config. `test_record.py` asserts
that the recorder writes the live `TOTAL_STAKE` and `BOOKS` into each scan row —
that is the behaviour under test and it is right to read the real module. Use
this only where a threshold stands between a fixture and the assertion.
"""

from __future__ import annotations

import config as _live


class _FrozenConfig:
    """The live config with the economic knobs pinned.

    A proxy rather than a copy so that anything the engine reads which is not
    an economic knob — book names, commissions, market lists, rounding rules —
    still comes from the real file and cannot silently drift out of sync with
    production behaviour.
    """

    def __init__(self, base, **overrides):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name, value):
        raise AttributeError(
            "fixture config is frozen — a test that needs different economics "
            "should build its own _FrozenConfig rather than mutate this one, "
            "or it will leak into every test that runs after it")


#: Stake of 1500 is the figure the demo fixtures were built against; at 1000
#: the best demo opportunity is $7.00 and clears no gate at all.
FIXTURE_CFG = _FrozenConfig(
    _live,
    TOTAL_STAKE=1500.00,
    MIN_PROFIT=10.00,
    MIN_MARGIN_PCT=1.0,
)
