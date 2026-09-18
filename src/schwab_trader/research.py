"""Research step: an infrequent, higher-intelligence pass that writes a plan.

This is the other half of the "brain". Where :mod:`schwab_trader.llm_strategy` is
the fast per-cycle executor (Haiku), the research step runs rarely (once, or once
a day) on the stronger model (Opus) and produces a :class:`StrategySpec`: a read
of the current market regime, a thesis, focus/avoid lists, concrete rules, and
risk sizing. That spec is persisted and injected into the executor's prompt, so
the loop follows a researched plan instead of reasoning from scratch each cycle.

Like the executor, the model is only an advisor: it returns a structured plan.
The single network call lives in :func:`build_anthropic_researcher`; everything
else is offline-testable, and the sizing the model proposes is clamped to sane
bounds before it can influence any (simulated) order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from schwab_trader.fundamentals import Ratios
from schwab_trader.llm_strategy import (
    AnthropicUnavailable,  # re-exported for callers/tests
    LLMRequestError,
    UsageSink,
    anthropic_client,
)
from schwab_trader.market_data import Fundamentals, Quote
from schwab_trader.usage import usage_from_message

__all__ = [
    "AnthropicUnavailable",
    "LLMRequestError",
    "ResearchContext",
    "ResearchSource",
    "ResearchStore",
    "StrategyPlan",
    "StrategySpec",
    "build_anthropic_researcher",
]

RESEARCH_SYSTEM_PROMPT = """You are a disciplined trading research analyst. You \
are given a trading universe, current quotes, the state of a small SIMULATED \
(paper) cash sleeve, and possibly source research/strategy documents. Produce a \
concrete strategy spec that a faster execution agent will follow on each later \
cycle.

Be specific and actionable, not generic:
- Read the current market regime in a few words (e.g. "risk-on momentum", \
"choppy/range-bound", "defensive").
- State a short thesis for how to trade this universe.
- Pick focus_symbols (a subset of the universe to prioritize) and avoid_symbols.
- Write rules as concrete, checkable instructions the executor can apply to live \
quotes each cycle (entry/exit conditions, how to weight names, what to skip, when \
to hold). The executor chooses exact share counts, so express sizing intent here \
(e.g. which names to weight heavier vs. keep as small positions).
- Set risk sizing: max_positions (how many distinct names to hold at once - \
prefer a diversified 8-12) and max_position_fraction (the largest share of the \
whole portfolio allowed in any single name, a fraction between 0 and 1, e.g. \
0.15 for 15%). Keep the portfolio diversified.

When fundamentals are provided (P/E, PEG, ROE, margins, growth, leverage), use \
them to judge valuation and quality - prefer names that are reasonably valued for \
their growth and profitability, and be wary of expensive or deteriorating ones.

When point-in-time SEC fundamentals (TTM) are provided (P/E, earnings yield E/P, \
book-to-market B/M, ROE, net margin), treat them as a historically consistent \
valuation/quality read: a high earnings yield or book-to-market flags a value \
candidate, while a high ROE and net margin flag quality. Prefer names that are \
both reasonably valued and profitable; reconcile them with the current \
fundamentals and news rather than trusting either in isolation.

When a macro read is provided (VIX, yield curve, credit spread), factor it into \
your regime view: a high or rising VIX, an inverted yield curve, or a widening \
credit spread argue for more caution and a larger cash buffer.

When prior-strategy performance is provided, learn from it: if the previous \
approach did well, keep what worked; if it lagged or drew down, change the thesis, \
focus names, or sizing rather than repeating it. State briefly what you changed \
and why in the thesis.

You may use the web_search tool to check for recent, price-moving news on these \
names (earnings, guidance, analyst moves, legal/regulatory events) before forming \
your view - the provided quotes and documents may be stale. Prefer recent, \
reputable sources.

If source documents are provided, ground your thesis, focus list, and rules in \
them. Only reference symbols from the provided universe."""

# Web search server-tool (Opus 4.8 native; dynamic filtering). Bounded per call.
_WEB_SEARCH_TYPE = "web_search_20260209"

# Defensive bounds applied to model-proposed sizing before it can affect orders.
_MIN_POSITION_FRACTION = Decimal("0.02")
_MAX_POSITION_FRACTION = Decimal("1.0")


class StrategyPlan(BaseModel):
    """The structured plan the research model fills in (its raw output)."""

    market_regime: str
    thesis: str
    focus_symbols: list[str]
    avoid_symbols: list[str]
    rules: list[str]
    max_positions: int
    max_position_fraction: float


class StrategySpec(BaseModel):
    """A persisted, sanitized strategy spec the executor follows.

    Built from a :class:`StrategyPlan` plus our metadata, with sizing clamped to
    sane bounds. :meth:`guidance_text` renders the block injected into the
    executor's system prompt.
    """

    created_at: datetime
    model: str
    universe: list[str]
    market_regime: str
    thesis: str
    focus_symbols: list[str]
    avoid_symbols: list[str]
    rules: list[str]
    max_positions: int
    max_position_fraction: Decimal

    def guidance_text(self) -> str:
        """Render the spec as a prompt block for the per-cycle executor."""
        lines = [
            f"Market regime: {self.market_regime}",
            f"Thesis: {self.thesis}",
        ]
        if self.focus_symbols:
            lines.append(f"Focus symbols: {', '.join(self.focus_symbols)}")
        if self.avoid_symbols:
            lines.append(f"Avoid symbols: {', '.join(self.avoid_symbols)}")
        lines.append(
            f"Risk sizing: hold at most {self.max_positions} distinct names; keep any "
            f"single position under {self.max_position_fraction * 100:.0f}% of the sleeve."
        )
        if self.rules:
            lines.append("Rules:")
            lines.extend(f"  {i}. {rule}" for i, rule in enumerate(self.rules, start=1))
        return "\n".join(lines)


@dataclass(frozen=True)
class ResearchContext:
    """Everything the research model sees for one research pass."""

    now: datetime
    cash: Decimal
    positions: dict[str, int]
    quotes: dict[str, Quote]
    universe: list[str]
    # Optional source research/strategy documents (markdown) to ground the spec.
    source_documents: list[str] = field(default_factory=list)
    # Optional current fundamentals per symbol (valuation/quality context).
    fundamentals: dict[str, Fundamentals] = field(default_factory=dict)
    # Optional point-in-time SEC EDGAR ratios (TTM) per symbol - historically
    # consistent valuation/quality metrics (P/E, earnings yield, book/market, ROE).
    ratios: dict[str, Ratios] = field(default_factory=dict)
    # Optional one-line macro read (VIX, yield curve, credit spread) from FRED.
    macro: str | None = None
    # Optional feedback on how the previous strategy spec has performed (adaptive).
    performance_feedback: str | None = None


ResearchSource = Callable[[ResearchContext], StrategySpec]


def _fmt(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _fmt_fundamentals(f: Fundamentals) -> str:
    """One compact line of the value/quality metrics that matter to the model."""
    parts: list[str] = []
    if f.pe_ratio is not None:
        parts.append(f"P/E {f.pe_ratio:.1f}")
    if f.peg_ratio is not None:
        parts.append(f"PEG {f.peg_ratio:.2f}")
    if f.return_on_equity is not None:
        parts.append(f"ROE {f.return_on_equity:.0f}%")
    if f.operating_margin_ttm is not None:
        parts.append(f"op-margin {f.operating_margin_ttm:.0f}%")
    if f.eps_change_pct_ttm is not None:
        parts.append(f"EPS-chg {f.eps_change_pct_ttm:+.0f}%")
    if f.rev_change_ttm is not None:
        parts.append(f"rev-chg {f.rev_change_ttm:+.0f}%")
    if f.total_debt_to_equity is not None:
        parts.append(f"D/E {f.total_debt_to_equity:.0f}")
    if f.market_cap is not None:
        parts.append(f"mktcap ${f.market_cap / 1e9:.0f}B")
    return ", ".join(parts) if parts else "n/a"


def _fmt_ratios(r: Ratios) -> str:
    """One compact line of the point-in-time SEC valuation/quality ratios (TTM)."""
    parts: list[str] = []
    if r.pe_ttm is not None:
        parts.append(f"P/E {r.pe_ttm:.1f}")
    if r.earnings_yield_ttm is not None:
        parts.append(f"E/P {r.earnings_yield_ttm * 100:.1f}%")
    if r.book_to_market is not None:
        parts.append(f"B/M {r.book_to_market:.2f}")
    if r.roe_ttm is not None:
        parts.append(f"ROE {r.roe_ttm * 100:.0f}%")
    if r.net_margin_ttm is not None:
        parts.append(f"net-margin {r.net_margin_ttm * 100:.0f}%")
    if r.market_cap is not None:
        parts.append(f"mktcap ${float(r.market_cap) / 1e9:.0f}B")
    return ", ".join(parts) if parts else "n/a"


def _render_research_context(context: ResearchContext) -> str:
    lines = [
        f"Time (UTC): {context.now.isoformat()}",
        f"Universe: {', '.join(context.universe)}",
        f"Paper cash available: ${context.cash:.2f}",
    ]
    held = ", ".join(f"{sym} x{qty}" for sym, qty in sorted(context.positions.items()) if qty)
    lines.append(f"Current positions: {held or 'none'}")
    if context.macro:
        lines.append(f"Macro: {context.macro}")
    if context.performance_feedback:
        lines.append("")
        lines.append(f"Prior-strategy performance: {context.performance_feedback}")
    lines.append("")
    lines.append("Current quotes:")
    for symbol in context.universe:
        quote = context.quotes.get(symbol)
        if quote is None:
            lines.append(f"- {symbol}: (no quote)")
            continue
        change = ""
        if quote.last is not None and quote.previous_close and quote.previous_close > 0:
            pct = (quote.last - quote.previous_close) / quote.previous_close * 100
            change = f", change {pct:+.2f}%"
        lines.append(
            f"- {symbol}: bid {_fmt(quote.bid)} ask {_fmt(quote.ask)} "
            f"last {_fmt(quote.last)} prev_close {_fmt(quote.previous_close)}{change}"
        )

    if context.fundamentals:
        lines.append("")
        lines.append("Fundamentals (current):")
        for symbol in context.universe:
            fundamentals = context.fundamentals.get(symbol)
            if fundamentals is not None:
                lines.append(f"- {symbol}: {_fmt_fundamentals(fundamentals)}")

    if context.ratios:
        lines.append("")
        lines.append("Point-in-time fundamentals (SEC EDGAR, TTM):")
        for symbol in context.universe:
            ratio = context.ratios.get(symbol)
            if ratio is not None:
                lines.append(f"- {symbol}: {_fmt_ratios(ratio)}")

    for i, document in enumerate(context.source_documents, start=1):
        lines.append("")
        lines.append(f"=== Source document {i} ===")
        lines.append(document.strip())
        lines.append("=== End source document ===")
    lines.append("")
    lines.append("Produce the strategy spec.")
    return "\n".join(lines)


def _clamp_sizing(plan: StrategyPlan, context: ResearchContext) -> tuple[int, Decimal]:
    """Clamp model-proposed sizing to sane bounds before it can affect orders."""
    universe_size = max(1, len(context.universe))
    max_positions = min(max(1, plan.max_positions), universe_size)

    proposed = Decimal(str(plan.max_position_fraction)).quantize(Decimal("0.0001"))
    fraction = min(max(proposed, _MIN_POSITION_FRACTION), _MAX_POSITION_FRACTION)
    return max_positions, fraction


def spec_from_plan(plan: StrategyPlan, context: ResearchContext, *, model: str) -> StrategySpec:
    """Wrap a model :class:`StrategyPlan` into a sanitized :class:`StrategySpec`.

    Symbols are normalized and filtered to the universe; sizing is clamped.
    """
    universe = {s.upper() for s in context.universe}

    def _in_universe(symbols: list[str]) -> list[str]:
        seen: list[str] = []
        for raw in symbols:
            sym = raw.strip().upper()
            if sym in universe and sym not in seen:
                seen.append(sym)
        return seen

    max_positions, max_position_fraction = _clamp_sizing(plan, context)
    return StrategySpec(
        created_at=context.now,
        model=model,
        universe=list(context.universe),
        market_regime=plan.market_regime.strip(),
        thesis=plan.thesis.strip(),
        focus_symbols=_in_universe(plan.focus_symbols),
        avoid_symbols=_in_universe(plan.avoid_symbols),
        rules=[r.strip() for r in plan.rules if r.strip()],
        max_positions=max_positions,
        max_position_fraction=max_position_fraction,
    )


def build_anthropic_researcher(
    *,
    api_key: str,
    model: str,
    use_web_search: bool = True,
    max_searches: int = 6,
    max_tokens: int = 8192,
    on_usage: UsageSink | None = None,
    client: object | None = None,
) -> ResearchSource:
    """Return a :data:`ResearchSource` backed by the Anthropic Messages API.

    The only function in this module that touches the network. Uses adaptive
    thinking (a reasoning task) and structured outputs, and - when
    ``use_web_search`` is set - lets the model search the web for recent news
    (bounded by ``max_searches`` to cap cost). ``client`` may be injected for
    testing.

    Raises:
        AnthropicUnavailable: if no API key is configured or the SDK is missing.
    """
    if client is None:
        client = anthropic_client(api_key)

    tools: list[dict[str, object]] = []
    if use_web_search:
        tools.append({"type": _WEB_SEARCH_TYPE, "name": "web_search", "max_uses": max_searches})

    def research(context: ResearchContext) -> StrategySpec:
        kwargs: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "system": RESEARCH_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _render_research_context(context)}],
            "output_format": StrategyPlan,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            message = client.messages.parse(**kwargs)  # type: ignore[attr-defined]
        except Exception as exc:  # surface any API failure uniformly
            raise LLMRequestError(f"Anthropic research request failed: {exc}") from exc
        if on_usage is not None:
            on_usage(usage_from_message(message, model))
        plan = message.parsed_output
        if not isinstance(plan, StrategyPlan):
            raise LLMRequestError("Research model returned no structured plan.")
        return spec_from_plan(plan, context, model=model)

    return research


class ResearchStore:
    """SQLite store for strategy specs (keeps history; exposes the latest)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_specs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    spec_json TEXT NOT NULL
                )
                """
            )

    def record(self, spec: StrategySpec) -> int:
        """Persist a spec and return its id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO strategy_specs (created_at, model, spec_json) VALUES (?, ?, ?)",
                (spec.created_at.isoformat(), spec.model, spec.model_dump_json()),
            )
            return int(cursor.lastrowid or 0)

    def latest(self) -> StrategySpec | None:
        """Return the most recently recorded spec, or ``None`` if there is none."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT spec_json FROM strategy_specs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return StrategySpec.model_validate_json(row["spec_json"])

    def recent(self, limit: int = 10) -> list[StrategySpec]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT spec_json FROM strategy_specs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [StrategySpec.model_validate_json(row["spec_json"]) for row in rows]
