"""Token/cost accounting for LLM calls (local, non-secret state).

Every Anthropic call the agent makes has a token cost; for a system that spends
real money on research and execution, that cost should be visible. This module
turns an API response's ``usage`` into a typed :class:`Usage`, estimates the
dollar cost from per-model pricing, and persists a running tally in SQLite so the
CLI can show per-call and lifetime spend.

Pricing is approximate and local; it is a spend *estimate*, not a bill. This
module imports nothing else from the package (it is a leaf) and performs no
network calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

# Dollars per 1M tokens (input, output). Unknown models fall back to Opus-tier.
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-sonnet-5": (Decimal("3"), Decimal("15")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-opus-4-7": (Decimal("5"), Decimal("25")),
}
_DEFAULT_PRICING = (Decimal("5"), Decimal("25"))
_WEB_SEARCH_COST = Decimal("0.01")  # ~$10 per 1000 searches
_MILLION = Decimal(1_000_000)
_COST_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class Usage:
    """Token counts (and web searches) for a single API call."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0


def estimate_cost(usage: Usage) -> Decimal:
    """Estimate the dollar cost of one call. Not a bill - a local estimate."""
    in_rate, out_rate = _PRICING.get(usage.model, _DEFAULT_PRICING)
    # input_tokens is the uncached remainder; cache reads ~0.1x, writes ~1.25x.
    billed_input = (
        Decimal(usage.input_tokens)
        + Decimal(usage.cache_read_tokens) * Decimal("0.1")
        + Decimal(usage.cache_write_tokens) * Decimal("1.25")
    )
    cost = (billed_input * in_rate + Decimal(usage.output_tokens) * out_rate) / _MILLION
    cost += Decimal(usage.web_searches) * _WEB_SEARCH_COST
    return cost.quantize(_COST_QUANT)


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _count_web_searches(message: object) -> int:
    """Count web_search server-tool invocations in a response (defensive)."""
    count = 0
    for block in getattr(message, "content", None) or []:
        if (
            getattr(block, "type", None) == "server_tool_use"
            and getattr(block, "name", None) == "web_search"
        ):
            count += 1
    raw = getattr(message, "usage", None)
    server_tool_use = getattr(raw, "server_tool_use", None)
    requests = getattr(server_tool_use, "web_search_requests", None)
    if requests:
        count = max(count, _int(requests))
    return count


def usage_from_message(message: object, model: str) -> Usage:
    """Extract a :class:`Usage` from an Anthropic response message (defensive)."""
    raw = getattr(message, "usage", None)
    return Usage(
        model=model,
        input_tokens=_int(getattr(raw, "input_tokens", 0)),
        output_tokens=_int(getattr(raw, "output_tokens", 0)),
        cache_read_tokens=_int(getattr(raw, "cache_read_input_tokens", 0)),
        cache_write_tokens=_int(getattr(raw, "cache_creation_input_tokens", 0)),
        web_searches=_count_web_searches(message),
    )


class UsageSummary(BaseModel):
    calls: int
    total_cost: Decimal
    execution_cost: Decimal
    research_cost: Decimal
    input_tokens: int
    output_tokens: int
    web_searches: int
    first_ts: datetime | None
    last_ts: datetime | None


class UsageStore:
    """SQLite store recording every LLM call's tokens and estimated cost."""

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
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cache_read_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER NOT NULL,
                    web_searches INTEGER NOT NULL,
                    cost TEXT NOT NULL
                )
                """
            )

    def record(self, kind: str, usage: Usage) -> Decimal:
        """Persist one call's usage; return its estimated cost."""
        cost = estimate_cost(usage)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_events (ts, kind, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, web_searches, cost) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    kind,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.web_searches,
                    str(cost),
                ),
            )
        return cost

    def summary(self) -> UsageSummary:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "COALESCE(SUM(web_searches), 0) AS web_searches, "
                "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM usage_events"
            ).fetchone()
            rows = conn.execute("SELECT kind, cost FROM usage_events").fetchall()
        execution = sum((Decimal(r["cost"]) for r in rows if r["kind"] == "execution"), Decimal(0))
        research = sum((Decimal(r["cost"]) for r in rows if r["kind"] == "research"), Decimal(0))
        return UsageSummary(
            calls=int(row["calls"]),
            total_cost=execution + research,
            execution_cost=execution,
            research_cost=research,
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            web_searches=int(row["web_searches"]),
            first_ts=datetime.fromisoformat(row["first_ts"]) if row["first_ts"] else None,
            last_ts=datetime.fromisoformat(row["last_ts"]) if row["last_ts"] else None,
        )
