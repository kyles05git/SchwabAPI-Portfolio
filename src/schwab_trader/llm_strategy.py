"""LLM-backed strategy: Claude proposes paper orders; our code disposes.

This is the "brain". :class:`LLMStrategy` implements the same
:class:`~schwab_trader.agent.Strategy` interface as the rule-based strategies,
but its :meth:`decide` sends the current market context to Claude and parses the
reply into typed :class:`~schwab_trader.agent.OrderProposal` objects.

The model is only ever an **advisor**: it returns structured data (which symbols
to buy or sell, and why). It never touches the account, never sees a token, and
has no path to Schwab. Every proposal it returns is routed through the same
:class:`~schwab_trader.paper.PaperEngine` (and, for live trading, the risk gates)
as any other strategy. The model proposes; our code disposes.

Network isolation: the *only* place that calls the Anthropic API is
:func:`build_anthropic_decider`. :class:`LLMStrategy` depends on an injected
``DecisionSource`` callable, so it is fully offline-testable with a stub and this
module's tests require neither the ``anthropic`` package nor an API key.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel

from schwab_trader.agent import MarketContext, OrderProposal, Strategy
from schwab_trader.market_data import Quote
from schwab_trader.models import OrderRequest, OrderSide
from schwab_trader.usage import Usage, usage_from_message

UsageSink = Callable[[Usage], None]

LLM_STRATEGY_NAME = "llm"

# Keep the system prompt static and stable (readable, and cache-friendly).
SYSTEM_PROMPT = """You are a cautious short-term trading assistant operating a \
small SIMULATED (paper) cash sleeve for evaluation. Real money is not involved, \
but trade as if it were: preserve capital and only act when you see a reason.

Rules you must follow:
- You may only propose BUY or SELL of US-listed equities/ETFs from the universe \
you are given. Never propose a symbol outside that list.
- Whole shares only. Positive quantities only.
- Do not propose spending more cash than is available.
- It is entirely acceptable to propose zero orders when nothing looks attractive; \
doing nothing is a valid, often correct, decision.
- For each order, give a short, concrete reason grounded in the data provided.

You are given cash, current positions, and a quote for each symbol (bid, ask, \
last, previous close, and the day's percent change). Return your overall \
market_view and a list of proposed orders (which may be empty)."""


class LLMOrderIdea(BaseModel):
    """A single order the model proposes. Prices are chosen by us, not the model."""

    action: Literal["BUY", "SELL"]
    symbol: str
    quantity: int
    reason: str


class LLMDecision(BaseModel):
    """The structured reply we require from the model each cycle."""

    market_view: str
    orders: list[LLMOrderIdea]


# The network boundary: given a market context, return the model's decision.
DecisionSource = Callable[[MarketContext], LLMDecision]


class LLMError(RuntimeError):
    """Base class for LLM strategy failures."""


class AnthropicUnavailable(LLMError):
    """The Anthropic client could not be constructed (missing key or package)."""


class LLMRequestError(LLMError):
    """The Anthropic API call failed."""


def anthropic_client(api_key: str) -> object:
    """Construct a real ``anthropic.Anthropic`` client, or raise if unavailable.

    Shared by the executor and the research step. Imported lazily so that offline
    tests (which inject a fake client) need neither the package nor a key.

    Raises:
        AnthropicUnavailable: if no API key is configured or the SDK is missing.
    """
    if not api_key:
        raise AnthropicUnavailable(
            "No Anthropic API key configured. Set SCHWAB_ANTHROPIC_API_KEY in .env."
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise AnthropicUnavailable("The 'anthropic' package is not installed.") from exc
    return anthropic.Anthropic(api_key=api_key)


def _reference_price(side: OrderSide, quote: Quote) -> Decimal | None:
    """The price a marketable order would fill at (ask for buys, bid for sells)."""
    candidates = (
        (quote.ask, quote.mark, quote.last)
        if side is OrderSide.BUY
        else (quote.bid, quote.mark, quote.last)
    )
    for candidate in candidates:
        if candidate is not None and candidate > 0:
            return candidate
    return None


def _marketable_limit(side: OrderSide, quote: Quote) -> Decimal | None:
    """A limit price that is marketable now: round buys up to the ask, sells down."""
    reference = _reference_price(side, quote)
    if reference is None:
        return None
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    limit = reference.quantize(Decimal("0.01"), rounding=rounding)
    return limit if limit > 0 else None


class LLMStrategy(Strategy):
    """A strategy whose decisions come from Claude, sized and priced by us.

    The model returns *ideas* (buy/sell a symbol, how many shares, and why) - so
    the model decides how much to put in each name. We turn each into a concrete,
    validated :class:`OrderRequest`: we compute a marketable limit from the live
    quote, honor the model's quantity but cap it so no single position exceeds
    ``max_position_fraction`` of the sleeve (a diversification backstop) and never
    exceeds available cash, cap sells to shares actually held, and drop anything
    outside the universe. The number of *distinct* holdings is capped at
    ``max_positions``.
    """

    name = LLM_STRATEGY_NAME

    def __init__(
        self,
        universe: list[str],
        *,
        decider: DecisionSource,
        max_positions: int = 20,
        max_position_fraction: Decimal = Decimal("0.10"),
    ) -> None:
        super().__init__(universe)
        self._decider = decider
        self.max_positions = max_positions
        self.max_position_fraction = max_position_fraction

    def decide(self, context: MarketContext) -> list[OrderProposal]:
        if not context.quotes:
            return []

        decision = self._decider(context)
        universe = set(self.universe)
        held = {symbol for symbol, qty in context.positions.items() if qty > 0}
        buy_room = self.max_positions - len(held)

        proposals: list[OrderProposal] = []
        seen: set[str] = set()
        for idea in decision.orders:
            symbol = idea.symbol.strip().upper()
            if symbol in seen or symbol not in universe or idea.quantity < 1:
                continue
            quote = context.quotes.get(symbol)
            if quote is None:
                continue
            side = OrderSide.BUY if idea.action == "BUY" else OrderSide.SELL
            limit = _marketable_limit(side, quote)
            if limit is None:
                continue

            if side is OrderSide.BUY:
                is_new = symbol not in held
                if is_new and buy_room <= 0:
                    continue
                # The model sizes the buy; we only cap it so the *total* value of
                # this position stays within max_position_fraction of the sleeve.
                base = context.equity if context.equity > 0 else context.cash
                held_value = context.positions.get(symbol, 0) * limit
                room = self.max_position_fraction * base - held_value
                budget = min(room, context.cash)
                affordable = int(budget // limit) if budget > 0 else 0
                quantity = min(idea.quantity, affordable)
                if quantity < 1:
                    continue
                if is_new:
                    buy_room -= 1
            else:
                current = context.positions.get(symbol, 0)
                quantity = min(idea.quantity, current)
                if quantity < 1:
                    continue

            request = OrderRequest(side=side, symbol=symbol, quantity=quantity, limit_price=limit)
            proposals.append(OrderProposal(request=request, rationale=idea.reason.strip()))
            seen.add(symbol)

        return proposals


def _render_context(context: MarketContext) -> str:
    """Render the market context as a compact, readable prompt for the model."""
    lines = [
        f"Time (UTC): {context.now.isoformat()}",
        f"Cash available: ${context.cash:.2f}",
    ]
    if context.positions:
        held = ", ".join(f"{sym} x{qty}" for sym, qty in sorted(context.positions.items()) if qty)
        lines.append(f"Current positions: {held or 'none'}")
    else:
        lines.append("Current positions: none")

    lines.append("")
    lines.append("Universe quotes:")
    for symbol in sorted(context.quotes):
        quote = context.quotes[symbol]
        change = ""
        if quote.last is not None and quote.previous_close and quote.previous_close > 0:
            pct = (quote.last - quote.previous_close) / quote.previous_close * 100
            change = f", change {pct:+.2f}%"
        lines.append(
            f"- {symbol}: bid {_fmt(quote.bid)} ask {_fmt(quote.ask)} "
            f"last {_fmt(quote.last)} prev_close {_fmt(quote.previous_close)}{change}"
        )

    lines.append("")
    lines.append("Propose orders (BUY/SELL from the universe) or an empty list.")
    return "\n".join(lines)


def _fmt(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def build_anthropic_decider(
    *,
    api_key: str,
    model: str,
    spec_guidance: str | None = None,
    max_tokens: int = 1024,
    on_usage: UsageSink | None = None,
    client: object | None = None,
) -> DecisionSource:
    """Return a :data:`DecisionSource` backed by the Anthropic Messages API.

    This is the only function in the module that touches the network. ``client``
    may be injected for testing; otherwise a real ``anthropic.Anthropic`` client
    is constructed from ``api_key``.

    ``spec_guidance`` is an optional strategy-spec block (produced by the research
    step) appended to the system prompt so the per-cycle executor follows a
    researched plan rather than reasoning from scratch each time.

    Raises:
        AnthropicUnavailable: if no API key is configured or the SDK is missing.
    """
    if client is None:
        client = anthropic_client(api_key)

    system = SYSTEM_PROMPT
    if spec_guidance:
        system = (
            f"{SYSTEM_PROMPT}\n\nFollow this current strategy spec, produced by a "
            f"separate research step. Apply it to today's data; do not contradict it "
            f"without the data clearly warranting it:\n\n{spec_guidance}"
        )

    def decide(context: MarketContext) -> LLMDecision:
        try:
            message = client.messages.parse(  # type: ignore[attr-defined]
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": _render_context(context)}],
                output_format=LLMDecision,
            )
        except Exception as exc:  # surface any API failure uniformly
            raise LLMRequestError(f"Anthropic request failed: {exc}") from exc
        if on_usage is not None:
            on_usage(usage_from_message(message, model))
        decision = message.parsed_output
        if not isinstance(decision, LLMDecision):
            return LLMDecision(market_view="(no structured output returned)", orders=[])
        return decision

    return decide


def build_llm_strategy(
    universe: list[str],
    *,
    api_key: str,
    model: str,
    spec_guidance: str | None = None,
    max_position_fraction: Decimal = Decimal("0.10"),
    max_positions: int = 20,
    on_usage: UsageSink | None = None,
) -> LLMStrategy:
    """Construct an :class:`LLMStrategy` wired to the real Anthropic API.

    When a research-produced ``spec_guidance`` block and its sizing are supplied,
    the executor follows that plan; otherwise it uses the conservative defaults.
    ``on_usage`` (if given) is called with the token usage of each API call.
    """
    decider = build_anthropic_decider(
        api_key=api_key, model=model, spec_guidance=spec_guidance, on_usage=on_usage
    )
    return LLMStrategy(
        universe,
        decider=decider,
        max_position_fraction=max_position_fraction,
        max_positions=max_positions,
    )
