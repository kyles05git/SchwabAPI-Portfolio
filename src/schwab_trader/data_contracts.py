"""Minimal provider seam: source capabilities and provenance envelopes.

This module defines the *thin contract* between the paper-sleeve workflow and any
future market/fundamental/macro data provider, without building the institutional
data platform (no FirstRate/FMP/ALFRED adapters, no Parquet/DuckDB, no network).

Two ideas live here:

- **Capability protocols** (:class:`DailyBarSource`, :class:`FundamentalSource`,
  :class:`MacroSource`): the smallest interface a provider must satisfy. Each
  carries a ``name`` and an ``enabled`` flag so a disabled or missing capability
  produces a *clear unready result* downstream rather than a silent gap.
- **Provenance envelopes** (:class:`BarBatch`, :class:`FundamentalBatch`,
  :class:`MacroBatch`): immutable batches that stamp every pull with its source,
  a stable ``snapshot_id``, when it was retrieved, the effective data timestamp,
  the *point-in-time availability* policy, and whether it is safe for
  vintage-correct backtesting.

Vintage safety is the crux. Predictive inputs (fundamentals, macro) must declare an
explicit *available-at* time - the earliest moment their contents were knowable in
the real world - or they cannot be trusted for point-in-time backtests. In
particular, **current latest-revised FRED data is not vintage-safe** (see
:data:`FRED_LATEST_REVISED_VINTAGE_SAFE` and :class:`TimingPolicy`): FRED serves the
most recently *revised* values, not what was published on a given historical date.

Nothing here reads secrets, tokens, trading databases, or vendor data. The offline
fakes at the bottom exist so contract tests and paper mode run with deterministic
data and no network.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Timing / vintage policy
# ---------------------------------------------------------------------------


class TimingPolicy(StrEnum):
    """How a batch's contents are timestamped relative to real-world knowability.

    - ``SETTLED_EOD``: daily bars finalized at the official close. The close date
      is both the effective data time and the availability time.
    - ``POINT_IN_TIME``: values carry the real release/knowledge time, so a
      backtest can ask "what was known as of date X" without look-ahead. This is
      the only vintage-safe policy for predictive inputs.
    - ``LATEST_REVISED``: values are the provider's most recent revision (e.g. the
      default FRED observations endpoint). Convenient for a live read, but it
      leaks future revisions into the past and is **not** vintage-safe.
    """

    SETTLED_EOD = "settled_eod"
    POINT_IN_TIME = "point_in_time"
    LATEST_REVISED = "latest_revised"


# The default FRED observations feed returns latest-revised values. Recorded here
# as an explicit, importable marker so callers cannot accidentally treat it as
# point-in-time. See TimingPolicy.LATEST_REVISED. Vintage-correct macro requires an
# ALFRED-style vintage adapter, which is intentionally out of scope for this seam.
FRED_LATEST_REVISED_VINTAGE_SAFE = False


# ---------------------------------------------------------------------------
# Provenance envelope shared by every batch
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Immutable provenance stamped on every data batch.

    ``snapshot_id`` is the stable identity of one immutable pull; it is required and
    non-empty so downstream runs can pin, deduplicate, and audit exactly which data
    they saw. ``as_of`` is the effective data timestamp (the latest observation the
    batch covers). ``available_at`` is the earliest real-world time the snapshot's
    contents were knowable - required for predictive batches and left ``None`` only
    where availability equals the data time (settled end-of-day bars).
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    retrieved_at: datetime
    as_of: datetime
    available_at: datetime | None = None
    timing: TimingPolicy
    vintage_safe: bool

    @model_validator(mode="after")
    def _check_consistency(self) -> Provenance:
        if self.timing is TimingPolicy.LATEST_REVISED and self.vintage_safe:
            raise ValueError("latest-revised data cannot be marked vintage_safe")
        if self.timing is TimingPolicy.POINT_IN_TIME and self.available_at is None:
            raise ValueError("point-in-time data requires an explicit available_at")
        return self


@runtime_checkable
class DataBatch(Protocol):
    """The common shape every provenance envelope exposes to readiness checks."""

    @property
    def provenance(self) -> Provenance: ...

    @property
    def covered_keys(self) -> frozenset[str]: ...

    def is_empty(self) -> bool: ...


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class BarObservation(BaseModel):
    """One official daily OHLCV bar for a symbol."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(ge=0)


class FundamentalObservation(BaseModel):
    """One point-in-time fundamental record for a symbol.

    ``reported_at`` is the date the figures were first publicly reported (the basis
    for point-in-time availability). ``values`` is an open map of metric name to
    value so this seam stays provider-agnostic without inventing a full schema.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    fiscal_period: str = Field(min_length=1)
    reported_at: date
    values: dict[str, Decimal]


class MacroObservation(BaseModel):
    """One macro series observation."""

    model_config = ConfigDict(frozen=True)

    series_id: str = Field(min_length=1)
    observation_date: date
    value: Decimal


# ---------------------------------------------------------------------------
# Provenance envelopes
# ---------------------------------------------------------------------------


class BarBatch(BaseModel):
    """Daily bars plus provenance.

    Bars are settled end-of-day facts, so an explicit ``available_at`` is optional
    (availability equals the close). They may still be marked vintage-safe.
    """

    model_config = ConfigDict(frozen=True)

    provenance: Provenance
    bars: tuple[BarObservation, ...] = ()

    @property
    def covered_keys(self) -> frozenset[str]:
        return frozenset(bar.symbol for bar in self.bars)

    def is_empty(self) -> bool:
        return not self.bars


class FundamentalBatch(BaseModel):
    """Fundamental records plus provenance.

    Fundamentals feed predictions, so this envelope requires an explicit
    ``available_at`` policy and a snapshot identity: without a known real-world
    availability time the data cannot be used point-in-time.
    """

    model_config = ConfigDict(frozen=True)

    provenance: Provenance
    records: tuple[FundamentalObservation, ...] = ()

    @model_validator(mode="after")
    def _require_available_at(self) -> FundamentalBatch:
        if self.provenance.available_at is None:
            raise ValueError("fundamental batches require an explicit available_at policy")
        return self

    @property
    def covered_keys(self) -> frozenset[str]:
        return frozenset(record.symbol for record in self.records)

    def is_empty(self) -> bool:
        return not self.records


class MacroBatch(BaseModel):
    """Macro observations plus provenance.

    Like fundamentals, macro inputs are predictive and require an explicit
    ``available_at`` policy and a snapshot identity. Latest-revised feeds (FRED's
    default) must set ``timing=LATEST_REVISED`` / ``vintage_safe=False``.
    """

    model_config = ConfigDict(frozen=True)

    provenance: Provenance
    observations: tuple[MacroObservation, ...] = ()

    @model_validator(mode="after")
    def _require_available_at(self) -> MacroBatch:
        if self.provenance.available_at is None:
            raise ValueError("macro batches require an explicit available_at policy")
        return self

    @property
    def covered_keys(self) -> frozenset[str]:
        return frozenset(obs.series_id for obs in self.observations)

    def is_empty(self) -> bool:
        return not self.observations


# ---------------------------------------------------------------------------
# Capability protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class DailyBarSource(Protocol):
    """A provider that can return official daily bars for symbols in a date range.

    ``enabled`` lets a configured-but-off source report cleanly as unavailable
    instead of raising or silently returning nothing.
    """

    name: str
    enabled: bool

    def fetch_daily_bars(self, symbols: tuple[str, ...], start: date, end: date) -> BarBatch: ...


@runtime_checkable
class FundamentalSource(Protocol):
    """A provider that can return point-in-time fundamentals for symbols."""

    name: str
    enabled: bool

    def fetch_fundamentals(self, symbols: tuple[str, ...], as_of: date) -> FundamentalBatch: ...


@runtime_checkable
class MacroSource(Protocol):
    """A provider that can return macro observations for series."""

    name: str
    enabled: bool

    def fetch_macro(self, series: tuple[str, ...], as_of: date) -> MacroBatch: ...


# ---------------------------------------------------------------------------
# Offline fakes (deterministic, no network) for contract tests and paper mode
# ---------------------------------------------------------------------------


def _snapshot_id(prefix: str, *parts: object) -> str:
    """A stable, human-readable snapshot id from its inputs (no randomness)."""
    joined = "-".join(str(p) for p in parts)
    return f"{prefix}:{joined}" if joined else prefix


class FakeDailyBarSource:
    """Deterministic in-memory daily bar source for tests.

    Returns a flat synthetic bar per requested symbol that has data. Bars are
    settled end-of-day and vintage-safe. Set ``enabled=False`` to exercise the
    disabled-capability path.
    """

    def __init__(
        self,
        *,
        symbols: dict[str, Decimal] | None = None,
        as_of: datetime,
        name: str = "fake-daily-bars",
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._prices = symbols or {}
        self._as_of = as_of

    def fetch_daily_bars(self, symbols: tuple[str, ...], start: date, end: date) -> BarBatch:
        bars = tuple(
            BarObservation(
                symbol=sym,
                session_date=end,
                open=self._prices[sym],
                high=self._prices[sym],
                low=self._prices[sym],
                close=self._prices[sym],
                volume=1_000,
            )
            for sym in symbols
            if sym in self._prices
        )
        provenance = Provenance(
            source=self.name,
            snapshot_id=_snapshot_id("bars", self.name, end.isoformat()),
            retrieved_at=self._as_of,
            as_of=self._as_of,
            timing=TimingPolicy.SETTLED_EOD,
            vintage_safe=True,
        )
        return BarBatch(provenance=provenance, bars=bars)


class FakeFundamentalSource:
    """Deterministic in-memory point-in-time fundamental source for tests."""

    def __init__(
        self,
        *,
        records: dict[str, dict[str, Decimal]] | None = None,
        as_of: datetime,
        available_at: datetime,
        name: str = "fake-fundamentals",
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._records = records or {}
        self._as_of = as_of
        self._available_at = available_at

    def fetch_fundamentals(self, symbols: tuple[str, ...], as_of: date) -> FundamentalBatch:
        records = tuple(
            FundamentalObservation(
                symbol=sym,
                fiscal_period="FY",
                reported_at=as_of,
                values=self._records[sym],
            )
            for sym in symbols
            if sym in self._records
        )
        provenance = Provenance(
            source=self.name,
            snapshot_id=_snapshot_id("fund", self.name, as_of.isoformat()),
            retrieved_at=self._as_of,
            as_of=self._as_of,
            available_at=self._available_at,
            timing=TimingPolicy.POINT_IN_TIME,
            vintage_safe=True,
        )
        return FundamentalBatch(provenance=provenance, records=records)


class FakeMacroSource:
    """Deterministic in-memory macro source for tests.

    Defaults to ``LATEST_REVISED`` timing to mirror FRED's default feed, so it is
    **not** vintage-safe. Pass ``vintage_safe=True`` (with point-in-time timing) to
    simulate an ALFRED-style vintage source.
    """

    def __init__(
        self,
        *,
        series: dict[str, Decimal] | None = None,
        as_of: datetime,
        available_at: datetime,
        name: str = "fake-macro",
        enabled: bool = True,
        vintage_safe: bool = False,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._series = series or {}
        self._as_of = as_of
        self._available_at = available_at
        self._vintage_safe = vintage_safe

    def fetch_macro(self, series: tuple[str, ...], as_of: date) -> MacroBatch:
        observations = tuple(
            MacroObservation(
                series_id=sid,
                observation_date=as_of,
                value=self._series[sid],
            )
            for sid in series
            if sid in self._series
        )
        timing = TimingPolicy.POINT_IN_TIME if self._vintage_safe else TimingPolicy.LATEST_REVISED
        provenance = Provenance(
            source=self.name,
            snapshot_id=_snapshot_id("macro", self.name, as_of.isoformat()),
            retrieved_at=self._as_of,
            as_of=self._as_of,
            available_at=self._available_at,
            timing=timing,
            vintage_safe=self._vintage_safe,
        )
        return MacroBatch(provenance=provenance, observations=observations)


__all__ = [
    "FRED_LATEST_REVISED_VINTAGE_SAFE",
    "BarBatch",
    "BarObservation",
    "DailyBarSource",
    "DataBatch",
    "FakeDailyBarSource",
    "FakeFundamentalSource",
    "FakeMacroSource",
    "FundamentalBatch",
    "FundamentalObservation",
    "FundamentalSource",
    "MacroBatch",
    "MacroObservation",
    "MacroSource",
    "Provenance",
    "TimingPolicy",
]
