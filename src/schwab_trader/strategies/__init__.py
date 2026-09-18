"""Shared, parallel-safe seam for challenger paper strategies.

This package exists so that several strategy implementations can be developed
independently without editing the same central files. It holds only two kinds of
thing:

* :mod:`schwab_trader.strategies.contract` - the frozen ``challenger-v1``
  experiment specification as typed, hashable data. Every challenger strategy
  reads its capital, universe, cadence, limits, and coverage floor from here
  instead of re-typing constants, so the code and
  ``docs/architecture/challenger-v1-contract.md`` cannot drift apart.
* :mod:`schwab_trader.strategies.sizing` - pure portfolio-construction helpers
  (look-ahead-safe history slicing, marketable limit prices, and the long-only
  whole-share rebalance planner) shared by every history-based strategy.

Deliberate non-goals: this package does no I/O, holds no mutable state, performs
no plugin discovery or dynamic import, and knows nothing about the strategy
registry, the CLI, cohort bootstrap, the dashboard, or live authorization.
Registration and cohort assembly belong to the integration issue, not here.

Submodules are imported explicitly (``from schwab_trader.strategies import
sizing``) rather than re-exported here, so that adding a strategy module in a
later issue never edits this file and two strategy agents can never conflict in
it.
"""

from __future__ import annotations
