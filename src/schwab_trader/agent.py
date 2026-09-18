"""Agent decision loop: strategies that propose orders, run against paper.

A :class:`Strategy` looks at a :class:`MarketContext` (cash, positions, and live
quotes for its universe) and returns typed :class:`OrderProposal` objects. The
:class:`AgentRunner` gathers the context, calls the strategy, and routes each
proposal to the paper engine - so the same order path is exercised as live
trading, but with simulated money.

Strategies are deliberately just an interface: a hand-written rule-based strategy
and an LLM-backed one would both implement :meth:`Strategy.decide`. This module is
network-free; the runner receives a ``quote_source`` callable.

For now the agent trades **paper only**. Routing an agent to live orders would go
through the existing gated submission flow and is intentionally not wired here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from pydantic import BaseModel, ConfigDict

from schwab_trader import fundamentals, pead, regime_allocator, signals
from schwab_trader.market_data import Candle, Quote, QuoteError
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.paper import PaperEngine, PaperOrder, PaperValuation, fill_reference
from schwab_trader.sec_store import SecStore
from schwab_trader.strategies import sizing

DEFAULT_UNIVERSE = ["SOFI", "F", "T", "INTC", "AMD", "PLTR", "NVDA", "AAPL"]


@dataclass(frozen=True)
class MarketContext:
    """Everything a strategy needs to make a decision for one cycle."""

    now: datetime
    cash: Decimal
    positions: dict[str, int]
    quotes: dict[str, Quote]
    # Total sleeve value (cash + marked positions); used for %-of-portfolio sizing.
    equity: Decimal = Decimal(0)
    # Cash deployable into buys. None means "unset" and falls back to `cash` (a cash
    # account); the runner sets it from the engine (larger than cash on margin).
    buying_power: Decimal | None = None
    # Buying-power multiplier (1 = cash account, 2 = Reg T margin).
    leverage: Decimal = Decimal(1)

    @property
    def spendable(self) -> Decimal:
        """Cash a strategy may deploy into buys; equals ``cash`` unless margin set it."""
        return self.cash if self.buying_power is None else self.buying_power


class OrderProposal(BaseModel):
    """A strategy's proposed order plus its rationale (for audit/evaluation)."""

    model_config = ConfigDict(frozen=True)

    request: OrderRequest
    rationale: str


class Strategy(ABC):
    """Base class for strategies. Subclasses set ``name`` and implement ``decide``."""

    name: str = "base"

    def __init__(self, universe: list[str]) -> None:
        self.universe = [symbol.strip().upper() for symbol in universe if symbol.strip()]

    @abstractmethod
    def decide(self, context: MarketContext) -> list[OrderProposal]:
        """Return zero or more proposed orders for this cycle."""


class HoldStrategy(Strategy):
    """Baseline: never trades. Useful as a control."""

    name = "hold"

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        return []


class DipBuyerStrategy(Strategy):
    """Example strategy: buy the biggest intraday dips vs. previous close.

    Not investment advice - it exists to exercise the loop end to end. Buys up to
    ``max_positions`` names that are down at least ``dip_pct`` on the day, sizing
    each to ``per_trade_cash`` in whole shares.
    """

    name = "dip-buyer"

    def __init__(
        self,
        universe: list[str],
        *,
        dip_pct: Decimal = Decimal("0.01"),
        max_positions: int = 3,
        per_trade_cash: Decimal = Decimal("400.00"),
    ) -> None:
        super().__init__(universe)
        self.dip_pct = dip_pct
        self.max_positions = max_positions
        self.per_trade_cash = per_trade_cash

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        held = {symbol for symbol, qty in context.positions.items() if qty > 0}
        room = self.max_positions - len(held)
        if room <= 0:
            return []

        candidates: list[tuple[Decimal, str, Quote]] = []
        for symbol in self.universe:
            if symbol in held:
                continue
            quote = context.quotes.get(symbol)
            if quote is None or quote.last is None or quote.previous_close is None:
                continue
            if quote.ask is None or quote.previous_close <= 0:
                continue
            change = (quote.last - quote.previous_close) / quote.previous_close
            if change <= -self.dip_pct:
                candidates.append((change, symbol, quote))

        candidates.sort(key=lambda item: item[0])  # biggest dip first
        proposals: list[OrderProposal] = []
        for change, symbol, quote in candidates[:room]:
            assert quote.ask is not None  # guarded above
            limit = quote.ask.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            budget = min(self.per_trade_cash, context.spendable)
            quantity = int(budget // limit)
            if quantity < 1:
                continue
            request = OrderRequest(
                side=OrderSide.BUY, symbol=symbol, quantity=quantity, limit_price=limit
            )
            proposals.append(
                OrderProposal(
                    request=request,
                    rationale=f"down {change * 100:.2f}% vs prev close; buy {quantity} @ {limit}",
                )
            )
        return proposals


class IntradayReversionStrategy(Strategy):
    """High-turnover intraday mean-reversion: buy dips vs prior close, sell the bounce.

    Built specifically to *stress settlement*. Run several times during a session it
    rapidly sells names that have reverted (a small scalp) or stopped out, and rotates
    into fresh intraday dips - so a cash account's T+1 lock on sale proceeds actually
    bites (it can't redeploy same-day), while an instant-settle margin sleeve recycles
    capital immediately. That contrast is invisible for the low-turnover daily
    strategies; this one exists to make it measurable.

    Not investment advice - a deliberately churny probe. Uses only the live snapshot
    (last vs previous close), so it needs no daily history and prices off the quote.
    """

    name = "intraday"

    def __init__(
        self,
        universe: list[str],
        *,
        entry_dip: Decimal = Decimal("0.005"),  # buy when >= 0.5% below prior close
        exit_recover: Decimal = Decimal("0.001"),  # sell once back within 0.1% of it
        stop_pct: Decimal = Decimal("0.02"),  # or cut a loser 2% below prior close
        max_positions: int = 5,
    ) -> None:
        super().__init__(universe)
        self.entry_dip = entry_dip
        self.exit_recover = exit_recover
        self.stop_pct = stop_pct
        self.max_positions = max_positions

    @staticmethod
    def _change_vs_prev_close(quote: Quote | None) -> Decimal | None:
        """Fractional move of ``last`` vs the previous close, or None if unavailable."""
        if quote is None or quote.last is None or quote.previous_close is None:
            return None
        if quote.previous_close <= 0:
            return None
        return (quote.last - quote.previous_close) / quote.previous_close

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        held = {symbol for symbol, qty in context.positions.items() if qty > 0}
        proposals: list[OrderProposal] = []

        # SELL leg: exit names that reverted toward prior close (scalp) or stopped out.
        for symbol in sorted(held):
            quote = context.quotes.get(symbol)
            change = self._change_vs_prev_close(quote)
            if change is None:
                continue
            reverted = change >= -self.exit_recover
            stopped = change <= -self.stop_pct
            if not (reverted or stopped):
                continue
            limit = _sell_limit(quote)
            quantity = context.positions[symbol]
            if limit is None or quantity <= 0:
                continue
            reason = "reverted to prior close (scalp exit)" if reverted else "stop: dip extended"
            proposals.append(
                OrderProposal(
                    request=OrderRequest(
                        side=OrderSide.SELL, symbol=symbol, quantity=quantity, limit_price=limit
                    ),
                    rationale=reason,
                )
            )

        # BUY leg: rotate into the biggest fresh intraday dips with available cash.
        room = self.max_positions - len(held)
        if room <= 0 or context.spendable <= 0:
            return proposals

        candidates: list[tuple[Decimal, str, Quote]] = []
        for symbol in self.universe:
            if symbol in held:
                continue
            quote = context.quotes.get(symbol)
            change = self._change_vs_prev_close(quote)
            if change is not None and change <= -self.entry_dip:
                assert quote is not None  # guaranteed when change is not None
                candidates.append((change, symbol, quote))

        candidates.sort(key=lambda item: item[0])  # biggest dip first
        per_trade = context.spendable / room
        for change, symbol, quote in candidates[:room]:
            limit = _buy_limit(quote)
            if limit is None:
                continue
            quantity = int(per_trade // limit)
            if quantity < 1:
                continue
            proposals.append(
                OrderProposal(
                    request=OrderRequest(
                        side=OrderSide.BUY, symbol=symbol, quantity=quantity, limit_price=limit
                    ),
                    rationale=f"down {change * 100:.2f}% vs prior close; intraday dip buy",
                )
            )
        return proposals


class BuyHoldStrategy(Strategy):
    """Buys an equal-weight basket of the universe once, then holds.

    Deterministic and LLM-free - the natural baseline for backtesting a basket
    thesis ("bought these names, held N days") and a control for the agent.
    """

    name = "buy-hold"

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        held = {symbol for symbol, qty in context.positions.items() if qty > 0}
        candidates = [symbol for symbol in self.universe if symbol not in held]
        if not candidates or context.spendable <= 0 or not self.universe:
            return []

        per_name = context.spendable / len(self.universe)
        proposals: list[OrderProposal] = []
        for symbol in candidates:
            quote = context.quotes.get(symbol)
            if quote is None or quote.ask is None or quote.ask <= 0:
                continue
            limit = quote.ask.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            quantity = int(per_name // limit)
            if quantity < 1:
                continue
            request = OrderRequest(
                side=OrderSide.BUY, symbol=symbol, quantity=quantity, limit_price=limit
            )
            proposals.append(OrderProposal(request=request, rationale="equal-weight buy-and-hold"))
        return proposals


# The pure sizing helpers now live in ``strategies.sizing`` so a challenger
# strategy in its own module can reuse them without editing this file. These are
# the same functions under their historical private names.
_buy_limit = sizing.buy_limit
_sell_limit = sizing.sell_limit
_asof = sizing.as_of_history


def _forced_request(order: PaperOrder) -> OrderRequest:
    """Rebuild the (always-SELL) order request from a margin-call liquidation fill."""
    return OrderRequest(
        side=OrderSide.SELL,
        symbol=order.symbol,
        quantity=order.quantity,
        limit_price=order.limit_price,
    )


def _rebalance(
    context: MarketContext,
    targets: list[str],
    gross_cap: Decimal,
    max_position_fraction: Decimal,
    *,
    exit_reason: str,
    enter_reason: str,
) -> list[OrderProposal]:
    """Sell held names not in ``targets``; buy target names not held.

    Shared by the history-based strategies (momentum, trend, mean-reversion). Buys
    are equal-weighted across targets, scaled by the regime ``gross_cap``, and
    capped so no single position exceeds ``max_position_fraction`` of the sleeve.

    The construction itself lives in :func:`schwab_trader.strategies.sizing.plan_rebalance`
    so challenger strategies share it verbatim; this wrapper only adapts the
    ``MarketContext`` in and the ``OrderProposal`` out.
    """
    planned = sizing.plan_rebalance(
        targets=targets,
        positions=context.positions,
        quotes=context.quotes,
        equity=context.equity,
        cash=context.cash,
        spendable=context.spendable,
        leverage=context.leverage,
        gross_cap=gross_cap,
        max_position_fraction=max_position_fraction,
        exit_reason=exit_reason,
        enter_reason=enter_reason,
    )
    return [
        OrderProposal(request=order.request, rationale=order.rationale) for order in planned
    ]


class MomentumStrategy(Strategy):
    """Rank the universe by momentum, hold the top names, scale by market regime.

    The rule-based version of the trading-strategy research's primary pick:
    cross-sectional momentum plus a transparent regime filter. Medium-horizon and
    low-turnover by design - momentum's 6-12 month lookbacks barely move day to
    day - so it establishes positions then mostly holds, rotating only when the
    rankings change. Needs daily price history (passed in at construction); prices
    orders from the live quotes in the context.
    """

    name = "momentum"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        max_positions: int = 8,
        max_position_fraction: Decimal = Decimal("0.10"),
        min_rank: float = 0.5,
    ) -> None:
        super().__init__(universe)
        self._history = history
        self._benchmark_history = benchmark_history
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.min_rank = min_rank

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        # Only use candles up to the decision date (prevents backtest look-ahead).
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]

        features = {
            symbol: signals.momentum_features(history[symbol])
            for symbol in self.universe
            if symbol in history
        }
        ranks = signals.momentum_composite(features)
        if not ranks:
            return []

        gross_cap = Decimal("1.0")
        if benchmark:
            gross_cap = signals.regime_signal(benchmark, history).gross_exposure_cap

        # Small epsilon so a genuinely-median name isn't dropped by float rounding.
        targets = sorted(
            (symbol for symbol in ranks if ranks[symbol] >= self.min_rank - 1e-9),
            key=lambda symbol: ranks[symbol],
            reverse=True,
        )[: self.max_positions]
        return _rebalance(
            context,
            targets,
            gross_cap,
            self.max_position_fraction,
            exit_reason=f"momentum rank fell out of top {self.max_positions}",
            enter_reason=f"top-{self.max_positions} momentum; regime cap {gross_cap:.0%}",
        )


class TrendStrategy(Strategy):
    """Trend-following: hold names in an uptrend (above their long SMA), else cash.

    The trading-strategy research's core risk-control / simple-backup strategy
    (absolute-momentum trend following). Holds the names trading furthest above
    their long moving average, scaled by the regime cap, and exits a name when it
    falls below that average - so it naturally de-risks into a bear market (few
    names stay above their long average). Needs daily history at construction.
    """

    name = "trend"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        max_positions: int = 10,
        max_position_fraction: Decimal = Decimal("0.10"),
        long_ma: int = 200,
    ) -> None:
        super().__init__(universe)
        self._history = history
        self._benchmark_history = benchmark_history
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.long_ma = long_ma

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]

        scored: list[tuple[float, str]] = []
        for symbol in self.universe:
            candles = history.get(symbol)
            if not candles:
                continue
            closes = [float(c.close) for c in candles]
            average = signals.sma(closes, self.long_ma)
            if average is None or average <= 0:
                continue
            distance = closes[-1] / average - 1
            if distance > 0:  # price above its long average = uptrend
                scored.append((distance, symbol))
        if not scored and not context.positions:
            return []

        scored.sort(reverse=True)  # strongest uptrend first
        targets = [symbol for _, symbol in scored[: self.max_positions]]
        gross_cap = Decimal("1.0")
        if benchmark:
            gross_cap = signals.regime_signal(benchmark, history).gross_exposure_cap
        return _rebalance(
            context,
            targets,
            gross_cap,
            self.max_position_fraction,
            exit_reason=f"fell below its {self.long_ma}-day average (trend broke)",
            enter_reason=f"uptrend (above {self.long_ma}-day avg); regime cap {gross_cap:.0%}",
        )


class MeanReversionStrategy(Strategy):
    """Buy oversold names that are still in a longer uptrend; sell as they revert.

    A disciplined mean-reversion strategy (unlike the toy :class:`DipBuyerStrategy`,
    which triggers off one day's move). Buys names trading at least ``dip`` below
    their short average *while still above* their long average - dips within an
    uptrend, not falling knives - and exits once a name recovers toward its short
    average. Needs daily history at construction.
    """

    name = "mean-reversion"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        max_positions: int = 5,
        max_position_fraction: Decimal = Decimal("0.10"),
        short_ma: int = 20,
        long_ma: int = 200,
        dip: float = 0.05,
    ) -> None:
        super().__init__(universe)
        self._history = history
        self._benchmark_history = benchmark_history
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.short_ma = short_ma
        self.long_ma = long_ma
        self.dip = dip

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]

        oversold: list[tuple[float, str]] = []
        for symbol in self.universe:
            candles = history.get(symbol)
            if not candles:
                continue
            closes = [float(c.close) for c in candles]
            short = signals.sma(closes, self.short_ma)
            long = signals.sma(closes, self.long_ma)
            if short is None or long is None or short <= 0:
                continue
            price = closes[-1]
            below = price / short - 1  # negative when below the short average
            if price > long and below <= -self.dip:  # oversold, but in an uptrend
                oversold.append((below, symbol))
        if not oversold and not context.positions:
            return []

        oversold.sort()  # most oversold (most negative) first
        targets = [symbol for _, symbol in oversold[: self.max_positions]]
        gross_cap = Decimal("1.0")
        if benchmark:
            gross_cap = signals.regime_signal(benchmark, history).gross_exposure_cap
        return _rebalance(
            context,
            targets,
            gross_cap,
            self.max_position_fraction,
            exit_reason="recovered toward its short average (reverted)",
            enter_reason=f"oversold >{self.dip:.0%} below {self.short_ma}-day avg, in uptrend",
        )


class LowVolatilityStrategy(Strategy):
    """Hold the calmest names in the universe: lowest trailing volatility wins.

    The low-volatility anomaly - low-risk stocks have historically delivered
    equity-like returns with smaller drawdowns - is one of the most robust and
    least-crowded-by-us equity factors, and the purest expression of this project's
    actual validated edge (match the market with less drawdown). It is also weakly
    correlated with the momentum/trend/value sleeves, so it genuinely diversifies
    the set rather than re-expressing it. Ranks the universe by trailing realized
    volatility (lowest first), holds the calmest ``max_positions`` equal-weighted,
    and scales by the same market-regime cap as the other strategies. Needs daily
    history at construction; prices orders from the live quotes in the context.
    """

    name = "low-vol"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        max_positions: int = 10,
        max_position_fraction: Decimal = Decimal("0.10"),
        vol_window: int = 120,
    ) -> None:
        super().__init__(universe)
        self._history = history
        self._benchmark_history = benchmark_history
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.vol_window = vol_window

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]

        scored: list[tuple[float, str]] = []
        for symbol in self.universe:
            candles = history.get(symbol)
            if not candles:
                continue
            closes = [float(c.close) for c in candles]
            vol = signals.realized_vol(closes, self.vol_window)
            if vol is None or vol <= 0:
                continue
            scored.append((vol, symbol))
        if not scored and not context.positions:
            return []

        scored.sort()  # lowest volatility (calmest) first
        targets = [symbol for _, symbol in scored[: self.max_positions]]
        gross_cap = Decimal("1.0")
        if benchmark:
            gross_cap = signals.regime_signal(benchmark, history).gross_exposure_cap
        return _rebalance(
            context,
            targets,
            gross_cap,
            self.max_position_fraction,
            exit_reason=f"no longer among the {self.max_positions} lowest-volatility names",
            enter_reason=(
                f"calmest {self.max_positions} by {self.vol_window}-day volatility; "
                f"regime cap {gross_cap:.0%}"
            ),
        )


class FundamentalStrategy(Strategy):
    """Rank the universe by a fundamental factor and hold the top names (live twin
    of ``backtest fundamental``).

    Uses point-in-time fundamentals from the SEC EDGAR store (trailing-twelve-month
    earnings by default) and the live quote price to score each name by a value or
    quality factor, then rebalances to the top ``max_positions`` equal-weighted. This
    is the piece that connects the fundamentals data stack to live paper trading;
    like momentum it is low-turnover (fundamentals move quarterly), so it establishes
    positions then mostly holds, rotating as rankings change. Needs the EDGAR store
    populated (``edgar fetch``); it prices off the live quotes in the context.
    """

    name = "fundamental"

    def __init__(
        self,
        universe: list[str],
        *,
        store: SecStore,
        factor: str = "book-to-market",
        max_positions: int = 10,
        max_position_fraction: Decimal = Decimal("0.10"),
        use_ttm: bool = True,
    ) -> None:
        super().__init__(universe)
        if factor not in fundamentals.FACTORS:
            msg = f"Unknown factor '{factor}'. Choose: {', '.join(fundamentals.FACTORS)}."
            raise ValueError(msg)
        self._store = store
        self.factor = factor
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.use_ttm = use_ttm

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        as_of = context.now.date()
        scored: list[tuple[Decimal, str]] = []
        for symbol in self.universe:
            quote = context.quotes.get(symbol)
            if quote is None:
                continue
            price = quote.mark or quote.last
            if price is None or price <= 0:
                continue
            value = fundamentals.factor_score(
                self._store, self.factor, symbol, as_of, price, use_ttm=self.use_ttm
            )
            if value is not None:
                scored.append((value, symbol))
        if not scored and not context.positions:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)  # higher factor = better
        targets = [symbol for _, symbol in scored[: self.max_positions]]
        return _rebalance(
            context,
            targets,
            Decimal("1.0"),
            self.max_position_fraction,
            exit_reason=f"fell out of the top {self.max_positions} by {self.factor}",
            enter_reason=f"top-{self.max_positions} by {self.factor}",
        )


def _percentile_ranks(scores: dict[str, Decimal]) -> dict[str, float]:
    """Cross-sectional percentile of each score in [0, 1] (highest score -> 1.0)."""
    if not scores:
        return {}
    ordered = sorted(scores.items(), key=lambda item: item[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    return {symbol: index / (n - 1) for index, (symbol, _) in enumerate(ordered)}


class ValueMomentumStrategy(Strategy):
    """Hold names that rank well on BOTH momentum and a fundamental value factor.

    Value and momentum are the two most-documented equity factors and are weakly (even
    negatively) correlated, so a blend of the two is more robust than either alone (the
    classic AQR value+momentum combo) and is the natural shot at beating a plain index.
    Each name gets a momentum percentile (from price history) and a value percentile
    (from a point-in-time EDGAR factor - default earnings yield - priced off the live
    quote); the blended rank picks the top names, scaled by the market regime. Needs
    daily history AND the SEC EDGAR store; both price and fundamentals are point-in-time
    (no look-ahead), so it is walk-forward-validatable.
    """

    name = "value-momentum"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        store: SecStore,
        factor: str = "earnings-yield",
        max_positions: int = 10,
        max_position_fraction: Decimal = Decimal("0.10"),
        min_rank: float = 0.5,
        value_weight: float = 0.5,
    ) -> None:
        super().__init__(universe)
        # ``factor`` may be one factor or a comma-separated blend (e.g.
        # "earnings-yield,roe" = value + quality); the fundamental ranks are averaged.
        factors = [f.strip() for f in factor.split(",") if f.strip()]
        for name in factors:
            if name not in fundamentals.FACTORS:
                msg = f"Unknown factor '{name}'. Choose: {', '.join(fundamentals.FACTORS)}."
                raise ValueError(msg)
        self._factors = factors or ["earnings-yield"]
        self.factor = ",".join(self._factors)
        self._history = history
        self._benchmark_history = benchmark_history
        self._store = store
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.min_rank = min_rank
        self.value_weight = value_weight

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]

        features = {
            symbol: signals.momentum_features(history[symbol])
            for symbol in self.universe
            if symbol in history
        }
        mom_ranks = signals.momentum_composite(features)
        if not mom_ranks:
            return []

        as_of = context.now.date()
        prices: dict[str, Decimal] = {}
        for symbol in self.universe:
            quote = context.quotes.get(symbol)
            if quote is None:
                continue
            price = quote.mark or quote.last
            if price is not None and price > 0:
                prices[symbol] = price

        # Rank each fundamental factor cross-sectionally, then average the ranks so
        # value and quality contribute equally (a multi-factor blend).
        factor_rank_maps: list[dict[str, float]] = []
        for factor_name in self._factors:
            scores: dict[str, Decimal] = {}
            for symbol, price in prices.items():
                score = fundamentals.factor_score(self._store, factor_name, symbol, as_of, price)
                if score is not None:
                    scores[symbol] = score
            factor_rank_maps.append(_percentile_ranks(scores))
        value_ranks: dict[str, float] = {}
        if factor_rank_maps:
            common = set.intersection(*(set(m) for m in factor_rank_maps))
            for symbol in common:
                value_ranks[symbol] = sum(m[symbol] for m in factor_rank_maps) / len(
                    factor_rank_maps
                )

        # Blend the two percentile ranks; only names scored on both are eligible.
        blended = {
            symbol: (1 - self.value_weight) * mom_ranks[symbol]
            + self.value_weight * value_ranks[symbol]
            for symbol in self.universe
            if symbol in mom_ranks and symbol in value_ranks
        }
        if not blended:
            return []

        gross_cap = Decimal("1.0")
        if benchmark:
            gross_cap = signals.regime_signal(benchmark, history).gross_exposure_cap
        targets = sorted(
            (symbol for symbol in blended if blended[symbol] >= self.min_rank - 1e-9),
            key=lambda symbol: blended[symbol],
            reverse=True,
        )[: self.max_positions]
        return _rebalance(
            context,
            targets,
            gross_cap,
            self.max_position_fraction,
            exit_reason=f"fell out of the value+momentum top {self.max_positions}",
            enter_reason=f"top-{self.max_positions} by value+momentum blend ({self.factor})",
        )


class TacticalRegimeStrategy(Strategy):
    """Paper-only three-state SPY/cash tactical allocator.

    The strategy evaluates Friday after the close and the backtester executes no
    earlier than the next session's open.  A stateless one-week confirmation rule
    maps the transparent trend/breadth/volatility signal to 100% SPY (risk-on),
    50% SPY (neutral), or cash (risk-off).  The target is only traded when actual
    exposure drifts outside ``drift_band``.

    This first slice intentionally excludes sector rotation, defensive ETFs, and
    live routing.  It is research scaffolding for a long-history, total-return
    comparison against SPY, not an assertion that market timing adds alpha.
    """

    name = "tactical"

    def __init__(
        self,
        universe: list[str],
        *,
        history: dict[str, list[Candle]],
        benchmark_history: list[Candle],
        drift_band: Decimal = Decimal("0.05"),
    ) -> None:
        if not universe:
            msg = "TacticalRegimeStrategy needs one risk asset (use SPY for version 0)."
            raise ValueError(msg)
        # Version 0 routes only the first explicitly supplied asset.  The CLI
        # defaults this to the benchmark (SPY); sector selection is a later layer.
        super().__init__([universe[0]])
        self._history = history
        self._benchmark_history = benchmark_history
        if not Decimal("0") <= drift_band < Decimal("1"):
            msg = "drift_band must be in [0, 1)."
            raise ValueError(msg)
        self.drift_band = drift_band

    def _rebalance_to_weight(
        self,
        context: MarketContext,
        target_weight: Decimal,
        *,
        rationale: str,
    ) -> list[OrderProposal]:
        symbol = self.universe[0]
        quote = context.quotes.get(symbol)
        if quote is None:
            return []
        mark = quote.mark or quote.last
        if mark is None or mark <= 0:
            return []

        equity = context.equity if context.equity > 0 else context.cash
        if equity <= 0:
            return []
        held = max(0, context.positions.get(symbol, 0))
        current_value = Decimal(held) * mark
        target_value = target_weight * equity
        if abs(current_value - target_value) / equity < self.drift_band:
            return []

        if current_value < target_value:
            limit = _buy_limit(quote)
            if limit is None:
                return []
            desired = int(target_value // limit)
            affordable = int(context.spendable // limit)
            quantity = min(max(0, desired - held), affordable)
            side = OrderSide.BUY
        else:
            limit = _sell_limit(quote)
            if limit is None:
                return []
            desired = int(target_value // limit)
            quantity = min(held, max(0, held - desired))
            side = OrderSide.SELL

        if quantity < 1:
            return []
        return [
            OrderProposal(
                request=OrderRequest(
                    side=side,
                    symbol=symbol,
                    quantity=quantity,
                    limit_price=limit,
                ),
                rationale=rationale,
            )
        ]

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        if not regime_allocator.is_weekly_evaluation_session(context.now):
            return []
        history = _asof(self._history, context.now)
        benchmark = [c for c in self._benchmark_history if c.date <= context.now]
        decision = regime_allocator.decide_regime(benchmark, history)
        if decision is None:
            return []
        confirmation = "confirmed" if decision.confirmed else "transition"
        rationale = (
            f"weekly regime {decision.state.value} ({confirmation}; current score "
            f"{decision.current_signal.score}/4, prior "
            f"{decision.confirmation_signal.score}/4): target "
            f"{decision.target_equity_weight:.0%} {self.universe[0]}"
        )
        return self._rebalance_to_weight(
            context,
            decision.target_equity_weight,
            rationale=rationale,
        )


class PostEarningsDriftStrategy(Strategy):
    """Ride post-earnings-announcement drift: hold names with the strongest recent
    positive earnings surprise for the weeks the drift persists.

    For each name it computes a time-series standardized unexpected earnings (SUE) from
    the point-in-time EDGAR store (see :mod:`schwab_trader.pead`), keeps only names that
    (a) reported within the last ``drift_window_days`` - the drift is a *post*-announcement
    effect, so a stale surprise is not actionable - and (b) have a positive surprise
    (long-only), then holds the top ``max_positions`` by SUE, equal-weighted. Event-driven
    and low-turnover (positions roll as fresh earnings arrive), and - unlike momentum and
    value - largely uncorrelated with them, which is what makes it useful in a multi-sleeve
    blend. Needs the EDGAR store populated (``edgar fetch``); prices off the live quotes.
    """

    name = "post-earnings-drift"

    def __init__(
        self,
        universe: list[str],
        *,
        store: SecStore,
        max_positions: int = 10,
        max_position_fraction: Decimal = Decimal("0.10"),
        drift_window_days: int = 65,
        min_sue: float = 0.0,
    ) -> None:
        super().__init__(universe)
        self._store = store
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction
        self.drift_window_days = drift_window_days
        self.min_sue = min_sue

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        as_of = context.now.date()
        scored: list[tuple[float, str]] = []
        for symbol in self.universe:
            if context.quotes.get(symbol) is None:
                continue  # need a live quote to size/price an order
            signal = pead.pead_signal(self._store, symbol, as_of)
            if signal is None:
                continue
            if signal.days_since_filed > self.drift_window_days:
                continue  # the drift window has passed; the surprise is stale
            if signal.sue <= self.min_sue:
                continue  # long-only: skip non-positive surprises
            scored.append((signal.sue, symbol))
        if not scored and not context.positions:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)  # strongest surprise first
        targets = [symbol for _, symbol in scored[: self.max_positions]]
        return _rebalance(
            context,
            targets,
            Decimal("1.0"),
            self.max_position_fraction,
            exit_reason=f"outside the top {self.max_positions} by recent earnings surprise",
            enter_reason=f"top-{self.max_positions} by post-earnings drift (SUE)",
        )


# Strategies that require daily price history (built specially, not via the
# simple registry below); see the CLI's history-strategy builder.
HISTORY_STRATEGY_NAMES = (
    MomentumStrategy.name,
    TrendStrategy.name,
    MeanReversionStrategy.name,
    LowVolatilityStrategy.name,
    TacticalRegimeStrategy.name,
)

# Needs the SEC EDGAR store injected at construction (built specially like history).
FUNDAMENTAL_STRATEGY_NAME = FundamentalStrategy.name
POST_EARNINGS_DRIFT_NAME = PostEarningsDriftStrategy.name
# Needs BOTH daily history and the EDGAR store.
VALUE_MOMENTUM_NAME = ValueMomentumStrategy.name


def available_strategies() -> list[str]:
    """Names of the universe-only strategies (``strategy_registry`` is the source).

    Imported lazily so ``strategy_registry`` (which imports this module for the
    strategy classes) can own the authoritative table without an import cycle.
    """
    from schwab_trader import strategy_registry

    return strategy_registry.simple_strategy_names()


def build_strategy(name: str, universe: list[str]) -> Strategy:
    """Construct a universe-only strategy by name.

    Raises:
        KeyError: if the name is not a registered simple strategy.
    """
    from schwab_trader import strategy_registry

    return strategy_registry.build_simple(name, universe)


QuoteSource = Callable[[str], Quote]


@dataclass(frozen=True)
class DecisionOutcome:
    proposal: OrderProposal
    status: str
    fill_price: Decimal | None
    detail: str


@dataclass(frozen=True)
class CycleReport:
    now: datetime
    strategy: str
    outcomes: list[DecisionOutcome]
    starting_value: Decimal
    ending_value: Decimal
    valuation: PaperValuation
    missing_quotes: list[str]

    @property
    def num_filled(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == "FILLED")

    @property
    def num_rejected(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status != "FILLED")


class AgentRunner:
    """Runs one decision cycle of a strategy against the paper engine."""

    def __init__(self, strategy: Strategy, engine: PaperEngine, quote_source: QuoteSource) -> None:
        self._strategy = strategy
        self._engine = engine
        self._quote_source = quote_source

    def run_cycle(
        self,
        *,
        now: datetime | None = None,
        fill_source: QuoteSource | None = None,
        mark_source: QuoteSource | None = None,
    ) -> CycleReport:
        """Run one decision cycle.

        With ``fill_source`` the strategy decides on the ``quote_source`` prices but
        orders fill at ``fill_source`` prices (market-on-open) - used by the backtester
        to avoid deciding and filling on the same bar (look-ahead). Without it, orders
        fill at the decision quotes (live/paper).

        With ``mark_source`` the sleeve is *valued* at those prices instead of the
        decision quotes, so a cycle that decides at one instant and executes at a later
        one reports its equity at the instant it actually traded. It is deliberately
        strict: a symbol ``mark_source`` cannot price raises rather than falling back to
        the decision quote, because silently marking at the earlier price is exactly the
        substitution a separated decision/execution model exists to prevent. The
        strategy's starting equity still uses decision quotes; letting T+1 marks leak
        into its context would itself be look-ahead.
        """
        now = now or datetime.now(UTC)

        # Advance time-based bookkeeping (T+1 settlement, margin interest) to now.
        self._engine.accrue(now)

        quotes: dict[str, Quote] = {}
        missing: list[str] = []
        for symbol in self._strategy.universe:
            try:
                quotes[symbol] = self._quote_source(symbol)
            except QuoteError:
                missing.append(symbol)

        decision_marks = {symbol: quote.mark for symbol, quote in quotes.items()}
        valuation_marks = decision_marks
        valuation_time = now
        if mark_source is not None:
            mark_quotes = {symbol: mark_source(symbol) for symbol in quotes}
            valuation_marks = {symbol: quote.mark for symbol, quote in mark_quotes.items()}
            mark_times = {quote.trade_time or quote.quote_time for quote in mark_quotes.values()}
            if len(mark_times) > 1:
                raise QuoteError("mark_source timestamps disagree across symbols")
            valuation_time = next(iter(mark_times), now)
            if valuation_time.tzinfo is None or valuation_time.utcoffset() is None:
                raise QuoteError("mark_source timestamps must include a timezone")
            if valuation_time < now:
                raise QuoteError("mark_source timestamp precedes the decision time")

        starting_value = self._engine.value(decision_marks).total_value

        positions = {position.symbol: position.quantity for position in self._engine.positions()}
        context = MarketContext(
            now=now,
            cash=self._engine.account().cash,
            positions=positions,
            quotes=quotes,
            equity=starting_value,
            buying_power=self._engine.buying_power(),
            leverage=self._engine.leverage,
        )

        outcomes: list[DecisionOutcome] = []
        for proposal in self._strategy.decide(context):
            request = proposal.request
            if request.symbol not in quotes:
                outcomes.append(
                    DecisionOutcome(proposal, "ERROR", None, "no quote for proposed symbol")
                )
                continue
            fill_quote = quotes[request.symbol]
            fill_time = now
            if fill_source is not None:
                # Market-on-open: fill at the next bar's price, repricing the limit so it
                # is marketable there (the strategy sized/priced on the decision bar).
                try:
                    fill_quote = fill_source(request.symbol)
                except QuoteError:
                    outcomes.append(
                        DecisionOutcome(proposal, "REJECTED", None, "no next-bar fill price")
                    )
                    continue
                ref = fill_reference(request.side, fill_quote)
                if ref is None:
                    outcomes.append(
                        DecisionOutcome(proposal, "REJECTED", None, "no next-bar fill price")
                    )
                    continue
                request = request.model_copy(update={"limit_price": ref})
                fill_time = fill_quote.trade_time or fill_quote.quote_time
                if fill_time.tzinfo is None or fill_time.utcoffset() is None:
                    outcomes.append(
                        DecisionOutcome(proposal, "REJECTED", None, "invalid next-bar fill time")
                    )
                    continue
                if fill_time < now:
                    outcomes.append(
                        DecisionOutcome(proposal, "REJECTED", None, "next-bar precedes decision")
                    )
                    continue
            order = self._engine.place_order(request, fill_quote, now=fill_time)
            outcomes.append(
                DecisionOutcome(proposal, order.status, order.fill_price, order.reason or "")
            )

        # Valuation and maintenance happen at the later mark instant. This also
        # advances T+1 cash settlement for a no-order cycle before it is recorded.
        self._engine.accrue(valuation_time)
        for forced in self._engine.enforce_maintenance(valuation_marks, now=valuation_time):
            outcomes.append(
                DecisionOutcome(
                    OrderProposal(
                        request=_forced_request(forced), rationale="margin-call liquidation"
                    ),
                    forced.status,
                    forced.fill_price,
                    forced.reason or "",
                )
            )

        valuation = self._engine.value(valuation_marks)
        return CycleReport(
            now=now,
            strategy=self._strategy.name,
            outcomes=outcomes,
            starting_value=starting_value,
            ending_value=valuation.total_value,
            valuation=valuation,
            missing_quotes=missing,
        )
