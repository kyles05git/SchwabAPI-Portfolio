"""Pure portfolio-construction helpers shared by history-based strategies.

These four functions were previously private to :mod:`schwab_trader.agent`
(``_asof``, ``_buy_limit``, ``_sell_limit``, ``_rebalance``) and already shared by
the momentum, trend, mean-reversion, low-volatility, fundamental,
value-momentum, and post-earnings-drift strategies. They are extracted here
unchanged so that a new challenger strategy in its own module can reuse the exact
same long-only whole-share portfolio construction without editing ``agent.py``.

The module is deliberately a leaf: it imports only :mod:`schwab_trader.models` and
:mod:`schwab_trader.market_data`, never :mod:`schwab_trader.agent`. That keeps the
dependency one-directional (``agent`` -> ``sizing``) and avoids an import cycle,
which is why :func:`plan_rebalance` takes explicit scalars rather than a
``MarketContext`` and returns :class:`PlannedOrder` rather than ``OrderProposal``.

Everything here is pure: no I/O, no clock, no randomness, no mutable state. The
same inputs always produce the same ordered output.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from schwab_trader.market_data import Candle, Quote
from schwab_trader.models import OrderRequest, OrderSide


@dataclass(frozen=True)
class PlannedOrder:
    """One proposed order plus the rationale that justifies it.

    The transport-neutral twin of ``agent.OrderProposal``: this module cannot
    import ``agent`` without creating a cycle, so callers in ``agent`` map this
    onto an ``OrderProposal`` and callers elsewhere use it directly.
    """

    request: OrderRequest
    rationale: str


def buy_limit(quote: Quote | None) -> Decimal | None:
    """A marketable buy limit (round the ask up), or None if no usable price."""
    if quote is None:
        return None
    reference = quote.ask or quote.mark or quote.last
    if reference is None or reference <= 0:
        return None
    return reference.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def sell_limit(quote: Quote | None) -> Decimal | None:
    """A marketable sell limit (round the bid down), or None if no usable price."""
    if quote is None:
        return None
    reference = quote.bid or quote.mark or quote.last
    if reference is None or reference <= 0:
        return None
    return reference.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def as_of_history(
    history: Mapping[str, list[Candle]], now: datetime
) -> dict[str, list[Candle]]:
    """Slice each symbol's candles to those on or before ``now`` (prevents look-ahead)."""
    return {symbol: [c for c in candles if c.date <= now] for symbol, candles in history.items()}


def plan_rebalance(
    *,
    targets: Sequence[str],
    positions: Mapping[str, int],
    quotes: Mapping[str, Quote],
    equity: Decimal,
    cash: Decimal,
    spendable: Decimal,
    leverage: Decimal,
    gross_cap: Decimal,
    max_position_fraction: Decimal,
    exit_reason: str,
    enter_reason: str,
) -> list[PlannedOrder]:
    """Sell held names not in ``targets``; buy target names not held.

    Buys are equal-weighted across ``targets``, scaled by ``gross_cap``, and capped
    so no single position exceeds ``max_position_fraction`` of the sleeve. Sizing
    uses ``equity`` when positive and otherwise ``cash``. Quantities are whole
    shares (fractional remainders stay in cash) and a name that cannot be priced is
    skipped rather than guessed at.

    Sells are emitted before buys so the freed cash is available to the buy side,
    and held names are iterated in sorted order so the output is deterministic.
    """
    target_set = set(targets)
    held = {symbol for symbol, qty in positions.items() if qty > 0}
    base = equity if equity > 0 else cash

    planned: list[PlannedOrder] = []
    # Exit held names no longer targeted (sell first to free cash for buys).
    for symbol in sorted(held):
        if symbol in target_set:
            continue
        limit = sell_limit(quotes.get(symbol))
        quantity = positions[symbol]
        if limit is not None and quantity > 0:
            planned.append(
                PlannedOrder(
                    request=OrderRequest(
                        side=OrderSide.SELL, symbol=symbol, quantity=quantity, limit_price=limit
                    ),
                    rationale=exit_reason,
                )
            )

    if targets:
        # Leverage scales both the gross exposure and the per-position cap, so a
        # margin sleeve can deploy more than 100% of equity (no-op at leverage 1).
        per_name = leverage * gross_cap * base / len(targets)
        cap = leverage * max_position_fraction * base
        for symbol in targets:
            if symbol in held:
                continue
            limit = buy_limit(quotes.get(symbol))
            if limit is None:
                continue
            budget = min(per_name, cap, spendable)
            quantity = int(budget // limit) if budget > 0 else 0
            if quantity < 1:
                continue
            planned.append(
                PlannedOrder(
                    request=OrderRequest(
                        side=OrderSide.BUY, symbol=symbol, quantity=quantity, limit_price=limit
                    ),
                    rationale=enter_reason,
                )
            )
    return planned


def eligible_by_history(
    history: Mapping[str, list[Candle]], universe: Iterable[str], minimum_closes: int
) -> tuple[list[str], list[str]]:
    """Split ``universe`` into symbols with enough history and those without.

    Returns ``(eligible, ineligible)``, both in the caller's universe order so the
    result is deterministic. Challenger strategies must report the ineligible list
    rather than silently dropping names, and must fail closed when the eligible
    fraction falls below the contract's coverage floor.
    """
    eligible: list[str] = []
    ineligible: list[str] = []
    for symbol in universe:
        candles = history.get(symbol)
        if candles is not None and len(candles) >= minimum_closes:
            eligible.append(symbol)
        else:
            ineligible.append(symbol)
    return eligible, ineligible


def rank_desc(scores: Mapping[str, float], order: Sequence[str]) -> list[str]:
    """Rank symbols by score, highest first, breaking ties by frozen ``order``.

    Float scores tie more often than they look like they should, and dictionary
    iteration order is an accident of insertion. Ranking every challenger through
    this function makes the selected set reproducible: the tie-break is the
    symbol's index in the strategy's frozen universe, never its hash or the order
    a data provider happened to return it in.
    """
    index = {symbol: position for position, symbol in enumerate(order)}
    return sorted(
        scores,
        key=lambda symbol: (-scores[symbol], index.get(symbol, len(index)), symbol),
    )
