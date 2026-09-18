"""Pure, restart-safe scheduling primitives for official paper-sleeve runs.

This module answers one question deterministically: *given the current Eastern time,
a cohort, and the set of run keys already completed, should an official run execute
now, and for which exchange session?* It performs **no** network, broker, database, or
clock side effects. The caller supplies the current time and the durable completed-run
set, so the same inputs always yield the same decision and a restarted process reaches
the identical conclusion.

Design notes:

- An :class:`ExchangeSession` is the unit of official identity. Its ``session_id`` is
  stable (``"XNYS:2026-07-20"``), and its official decision/valuation timestamps are
  the session close (mark-to-close), exposed both as naive Eastern and aware UTC.
- The :func:`run_key` derived from ``(cohort, session)`` is the idempotency token. A
  durable runner records completed keys; passing that set back in makes duplicate
  requests and restarts converge on :attr:`RunStatus.ALREADY_COMPLETED`.
- :class:`SchedulingPolicy` separates the *official* close timestamp from the
  *operational* window: an optional post-close delay before a run becomes due, a grace
  period, and a hard deadline after which a missed session is not run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from schwab_trader import market_calendar as mc


@dataclass(frozen=True)
class ExchangeSession:
    """Identity and official timestamps for one exchange trading session.

    For a closed date (weekend or holiday) ``is_trading_day`` is ``False`` and the
    timestamp fields are ``None``.
    """

    exchange: str
    session_date: date
    is_trading_day: bool
    is_early_close: bool
    open_et: datetime | None
    close_et: datetime | None

    @property
    def session_id(self) -> str:
        """Stable exchange-session identity, e.g. ``"XNYS:2026-07-20"``."""
        return f"{self.exchange}:{self.session_date.isoformat()}"

    @property
    def decision_et(self) -> datetime | None:
        """Official decision instant (mark-to-close) as naive Eastern time."""
        return self.close_et

    @property
    def valuation_et(self) -> datetime | None:
        """Official valuation instant (mark-to-close) as naive Eastern time."""
        return self.close_et

    @property
    def decision_utc(self) -> datetime | None:
        """Official decision instant as an aware UTC ``datetime``."""
        return None if self.close_et is None else mc.eastern_to_utc(self.close_et)

    @property
    def valuation_utc(self) -> datetime | None:
        """Official valuation instant as an aware UTC ``datetime``."""
        return None if self.close_et is None else mc.eastern_to_utc(self.close_et)


def session_for_date(session_date: date, exchange: str = mc.EXCHANGE_MIC) -> ExchangeSession:
    """Build the :class:`ExchangeSession` for a calendar date without any I/O."""
    trading = mc.is_trading_day(session_date)
    if not trading:
        return ExchangeSession(
            exchange=exchange,
            session_date=session_date,
            is_trading_day=False,
            is_early_close=False,
            open_et=None,
            close_et=None,
        )
    return ExchangeSession(
        exchange=exchange,
        session_date=session_date,
        is_trading_day=True,
        is_early_close=mc.is_early_close(session_date),
        open_et=datetime.combine(session_date, mc.MARKET_OPEN),
        close_et=datetime.combine(session_date, mc.session_close(session_date)),
    )


def run_key(cohort_id: str, session: ExchangeSession | str) -> str:
    """Deterministic idempotency key from a cohort and its scheduled session.

    ``session`` may be an :class:`ExchangeSession` or a raw ``session_id`` string. The
    key is human-readable (``"cohort@XNYS:2026-07-20"``); use :func:`run_fingerprint`
    for a fixed-width hash suitable as a database primary key.
    """
    session_id = session if isinstance(session, str) else session.session_id
    return f"{cohort_id}@{session_id}"


def run_fingerprint(cohort_id: str, session: ExchangeSession | str) -> str:
    """Stable SHA-256 hex fingerprint of :func:`run_key`."""
    return hashlib.sha256(run_key(cohort_id, session).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SchedulingPolicy:
    """Operational timing around a session's official close.

    - ``decision_delay``: how long after the official close a run becomes due (0 = at
      close). Useful when marks settle slightly after the close print.
    - ``grace_period``: how long a due run stays :attr:`RunStatus.DUE` before it is
      flagged :attr:`RunStatus.LATE`.
    - ``max_catchup``: optional cap on how long after the due time a run may still
      execute late. When ``None`` (default) the deadline is the *next* trading
      session's due time, so a catch-up run is superseded only once a fresher session
      is itself due. A session is :attr:`RunStatus.MISSED` past the deadline.
    - ``lookback_days``: bounded search horizon for the most recent due session.
    """

    decision_delay: timedelta = timedelta(0)
    grace_period: timedelta = timedelta(hours=2)
    max_catchup: timedelta | None = None
    lookback_days: int = 15


DEFAULT_POLICY = SchedulingPolicy()


def execution_deadline_et(
    session_date: date,
    *,
    policy: SchedulingPolicy = DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> datetime | None:
    """Return the authoritative late-catchup deadline for one exchange session."""
    session = session_for_date(session_date, exchange)
    if not session.is_trading_day:
        return None
    assert session.decision_et is not None
    due_et = session.decision_et + policy.decision_delay
    next_session = session_for_date(mc.next_trading_day(session_date), exchange)
    assert next_session.decision_et is not None
    deadline = next_session.decision_et + policy.decision_delay
    if policy.max_catchup is not None:
        deadline = min(deadline, due_et + policy.max_catchup)
    return deadline


def human_duration(value: timedelta) -> str:
    """Format elapsed scheduler time without raw microseconds."""
    total = max(0, int(value.total_seconds()))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


class RunStatus(StrEnum):
    """The scheduling verdict for one ``(cohort, session)`` pair at a moment in time."""

    SKIPPED_CLOSED_SESSION = "skipped-closed-session"
    PENDING = "pending"
    DUE = "due"
    LATE = "late"
    MISSED = "missed"
    ALREADY_COMPLETED = "already-completed"


@dataclass(frozen=True)
class RunDecision:
    """The outcome of evaluating whether a cohort run should execute now."""

    status: RunStatus
    session: ExchangeSession
    cohort_id: str
    reason: str
    run_key: str | None = None
    due_et: datetime | None = None
    late_by: timedelta | None = None

    @property
    def should_run(self) -> bool:
        """True only when the run is due or eligible for a flagged late execution."""
        return self.status in (RunStatus.DUE, RunStatus.LATE)


def evaluate_session(
    cohort_id: str,
    session_date: date,
    now_et: datetime,
    completed_run_keys: frozenset[str] | set[str] = frozenset(),
    policy: SchedulingPolicy = DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> RunDecision:
    """Evaluate one specific calendar session for a cohort at ``now_et``.

    ``now_et`` and all comparisons are naive Eastern wall-clock time (see
    :func:`schwab_trader.market_calendar.eastern_now`). ``completed_run_keys`` is the
    durable set of finished run keys; membership makes the decision idempotent across
    duplicate requests and restarts.
    """
    session = session_for_date(session_date, exchange)

    if not session.is_trading_day:
        label = mc.holiday_name(session_date)
        why = f"holiday: {label}" if label else "weekend"
        return RunDecision(
            status=RunStatus.SKIPPED_CLOSED_SESSION,
            session=session,
            cohort_id=cohort_id,
            reason=f"{session_date.isoformat()} is not a trading session ({why}).",
        )

    key = run_key(cohort_id, session)
    assert session.decision_et is not None  # trading day always has a close
    due_et = session.decision_et + policy.decision_delay

    if key in completed_run_keys:
        return RunDecision(
            status=RunStatus.ALREADY_COMPLETED,
            session=session,
            cohort_id=cohort_id,
            reason=f"Run key {key} is already recorded as completed.",
            run_key=key,
            due_et=due_et,
        )

    if now_et < due_et:
        return RunDecision(
            status=RunStatus.PENDING,
            session=session,
            cohort_id=cohort_id,
            reason=f"Session decision time {due_et.isoformat()} has not been reached.",
            run_key=key,
            due_et=due_et,
        )

    deadline = execution_deadline_et(session_date, policy=policy, exchange=exchange)
    assert deadline is not None
    elapsed = now_et - due_et
    elapsed_label = human_duration(elapsed)
    if elapsed <= policy.grace_period:
        status, reason = RunStatus.DUE, f"Run is due ({elapsed_label} elapsed within grace)."
    elif now_et < deadline:
        status, reason = (
            RunStatus.LATE,
            f"Run is late by {elapsed_label} but the next session is not yet due.",
        )
    else:
        status, reason = (
            RunStatus.MISSED,
            f"Run is missed: superseded at {deadline.isoformat()} by a fresher session.",
        )

    return RunDecision(
        status=status,
        session=session,
        cohort_id=cohort_id,
        reason=reason,
        run_key=key,
        due_et=due_et,
        late_by=elapsed,
    )


def most_recent_due_session_date(
    now_et: datetime,
    policy: SchedulingPolicy = DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> date | None:
    """The latest trading date whose due time is at or before ``now_et``.

    Before today's close this is the previous trading day; on a weekend it is Friday.
    Returns ``None`` only if no trading session is found within ``policy.lookback_days``.
    """
    day = now_et.date()
    for _ in range(policy.lookback_days + 1):
        session = session_for_date(day, exchange)
        if session.is_trading_day:
            assert session.decision_et is not None
            due_et = session.decision_et + policy.decision_delay
            if due_et <= now_et:
                return day
        day -= timedelta(days=1)
    return None


def plan_run(
    cohort_id: str,
    now_et: datetime,
    completed_run_keys: frozenset[str] | set[str] = frozenset(),
    policy: SchedulingPolicy = DEFAULT_POLICY,
    exchange: str = mc.EXCHANGE_MIC,
) -> RunDecision:
    """Decide whether the cohort's most recent due session should run now.

    This resolves the scheduled session from ``now_et`` (never a closed date) and then
    delegates to :func:`evaluate_session`, so the result is one of
    :attr:`RunStatus.DUE`, :attr:`RunStatus.LATE`, :attr:`RunStatus.MISSED`, or
    :attr:`RunStatus.ALREADY_COMPLETED`.
    """
    session_date = most_recent_due_session_date(now_et, policy, exchange)
    if session_date is None:
        closed = session_for_date(now_et.date(), exchange)
        return RunDecision(
            status=RunStatus.SKIPPED_CLOSED_SESSION,
            session=closed,
            cohort_id=cohort_id,
            reason=(
                "No trading session found within "
                f"{policy.lookback_days} days at or before {now_et.isoformat()}."
            ),
        )
    return evaluate_session(cohort_id, session_date, now_et, completed_run_keys, policy, exchange)
