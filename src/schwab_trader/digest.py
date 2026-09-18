"""Performance digest over the paper sleeves (notification-layer idea #4).

Formats the sleeve leaderboard - per-sleeve return, value, excess over a chosen
benchmark sleeve, drawdown, Sharpe, and activity - into a
:class:`~schwab_trader.notify.NotifyMessage` (plain text + a clean HTML body) that
can be emailed through the task-1 notification channel (``notify.build_notifier``).
It is a pure formatter over what the
:class:`~schwab_trader.evaluation.EvaluationStore` already records: no network
calls, no order placement. Sent daily after the morning sleeve run.

The digest keeps the project's honest framing front and centre - it states plainly
how many sleeves actually beat the benchmark, because "matches the market with less
drawdown, but does not beat it" has been the recurring truth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from schwab_trader import benchmark_scope, cohort_scope, emailfmt
from schwab_trader.config import Settings
from schwab_trader.notify import NotifyMessage
from schwab_trader.storage import factory as storage_factory


class DigestRow(BaseModel):
    """One sleeve's line in the digest (renderer-agnostic)."""

    name: str
    strategy: str
    cycles: int
    trades: int
    value: Decimal
    return_pct: Decimal
    excess_pct: Decimal | None
    max_drawdown_pct: Decimal
    sharpe: Decimal | None
    is_benchmark: bool


def collect_digest_rows(
    settings: Settings, benchmark: str, *, cohort_id: str | None = None
) -> list[DigestRow]:
    """Gather per-sleeve performance, ranked by total return (local; no network).

    ``cohort_id`` scopes the whole digest, not only the benchmark lookup: the rows are
    that cohort's members and nothing else, matching ``sleeve compare --cohort`` exactly
    so the emailed report and the leaderboard cannot describe different populations.

    ``benchmark`` is resolved through :mod:`schwab_trader.benchmark_scope`, so the name
    every cohort reuses for its control sleeve narrows to the collection still running
    instead of raising. Raises a ``LookupError``:
    :class:`~schwab_trader.benchmark_scope.BenchmarkScopeError` when the benchmark cannot
    be narrowed to one sleeve, :class:`~schwab_trader.cohort_scope.CohortScopeError` when
    the cohort holds no sleeves at all.
    """
    store = storage_factory.sleeve_store(settings)
    configs = store.list()
    # Counted over the whole registry rather than the scope, so a name two cohorts share
    # still identifies which stored record a row is.
    name_counts = {
        name: sum(config.name == name for config in configs)
        for name in {config.name for config in configs}
    }
    scoped = configs if cohort_id is None else cohort_scope.members(configs, cohort_id)
    summaries = {
        cfg.identity: storage_factory.evaluation_store(settings, cfg).summary()
        for cfg in scoped
    }
    ordered = sorted(
        scoped,
        key=lambda cfg: summaries[cfg.identity].total_return_pct,
        reverse=True,
    )

    bench_return: Decimal | None = None
    # Resolved against the full registry so a benchmark belonging to another cohort is
    # refused with a message naming where it does live, rather than silently comparing
    # across cohorts or reporting "no such sleeve" for one that plainly exists.
    benchmark_config = benchmark_scope.resolve(configs, benchmark, cohort_id=cohort_id)
    benchmark_id = (
        benchmark_config.identity if benchmark_config is not None else None
    )
    if benchmark_config is not None:
        bench_return = summaries[benchmark_config.identity].total_return_pct

    rows: list[DigestRow] = []
    for cfg in ordered:
        summary = summaries[cfg.identity]
        value = summary.latest_value if summary.cycles > 0 else cfg.starting_cash
        excess: Decimal | None = None
        if bench_return is not None and cfg.identity != benchmark_id:
            excess = summary.total_return_pct - bench_return
        rows.append(
            DigestRow(
                name=(
                    cfg.name
                    if name_counts[cfg.name] == 1
                    else f"{cfg.name} [{cfg.cohort_id or 'legacy'}]"
                ),
                strategy=cfg.strategy,
                cycles=summary.cycles,
                trades=summary.trades_filled,
                value=value,
                return_pct=summary.total_return_pct,
                excess_pct=excess,
                max_drawdown_pct=summary.max_drawdown_pct,
                sharpe=summary.sharpe,
                is_benchmark=(cfg.identity == benchmark_id),
            )
        )
    return rows


def _verdict(rows: list[DigestRow], benchmark: str) -> str:
    """The honest one-line summary: who leads, and whether anything beats the bench."""
    if not rows:
        return "No sleeves are configured yet."
    non_bench = [r for r in rows if not r.is_benchmark]
    beat = [r for r in non_bench if r.excess_pct is not None and r.excess_pct > 0]
    bench_row = next((r for r in rows if r.is_benchmark), None)
    if bench_row is None or not non_bench:
        return f"Leader: {rows[0].name} at {rows[0].return_pct:+.2f}%."
    if beat:
        names = ", ".join(r.name for r in beat)
        return f"{len(beat)} of {len(non_bench)} sleeves beat {benchmark} ({names})."
    return (
        f"None of {len(non_bench)} sleeves beat {benchmark} this period - "
        "matching the market with less drawdown, not beating it."
    )


def _fmt_excess(row: DigestRow) -> str:
    if row.is_benchmark:
        return "bench"
    if row.excess_pct is None:
        return "-"
    return f"{row.excess_pct:+.2f}%"


#: Smallest name column, and the width every short-named digest keeps using.
_NAME_COLUMN_MIN = 12
#: Upper bound, so one pathological name cannot reflow the whole table.
_NAME_COLUMN_MAX = 44


def _name_column(rows: list[DigestRow]) -> int:
    """Width wide enough to show the longest name in full, within bounds.

    Fixed at 11 usable characters this truncated ``control-cash [challenger-v1-...]``
    to ``control-cas`` — deleting the very qualifier that says which cohort a row
    belongs to, which is the one thing a reader needs when two cohorts share a sleeve
    name. Digests whose names all fit in the old width render exactly as before.
    """
    longest = max((len(row.name) for row in rows), default=0)
    return max(_NAME_COLUMN_MIN, min(_NAME_COLUMN_MAX, longest + 1))


def _table_text(rows: list[DigestRow]) -> str:
    width = _name_column(rows)
    header = (
        f"{'name':<{width}}{'strategy':<16}{'value':>11}{'return':>9}"
        f"{'vs bench':>10}{'maxDD':>8}{'sharpe':>8}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        sharpe = f"{row.sharpe}" if row.sharpe is not None else "-"
        lines.append(
            f"{row.name[: width - 1]:<{width}}{row.strategy[:15]:<16}"
            f"{f'${row.value:,.2f}':>11}{f'{row.return_pct:+.2f}%':>9}"
            f"{_fmt_excess(row):>10}{f'-{row.max_drawdown_pct:.1f}%':>8}{sharpe:>8}"
        )
    return "\n".join(lines)


def render_digest_text(
    rows: list[DigestRow], benchmark: str, *, now: datetime, cohort_id: str | None = None
) -> tuple[str, str]:
    """Return ``(subject, plain_text_body)`` for the digest.

    ``cohort_id`` names the scope in the subject and the body. Two cohorts produce two
    structurally identical tables, so an email that does not say which one it covers is
    unreadable in an inbox that holds both.
    """
    date = now.astimezone().strftime("%Y-%m-%d")
    scope = f" [{cohort_id}]" if cohort_id else ""
    if not rows:
        return (
            f"Sleeve digest {date}{scope} - no sleeves",
            f"Sleeve digest ({date})\n\nNo sleeves are configured yet. "
            "Create one with 'schwab-trader sleeve create'.",
        )
    leader = rows[0]
    total_cycles = sum(r.cycles for r in rows)
    total_trades = sum(r.trades for r in rows)
    subject = f"Sleeve digest {date}{scope} - {leader.name} leads {leader.return_pct:+.2f}%"
    scope_line = f"Cohort: {cohort_id} (this cohort's members only)\n" if cohort_id else ""
    body = (
        f"Sleeve digest ({date})\n"
        f"{scope_line}\n"
        f"{_table_text(rows)}\n\n"
        f"{_verdict(rows, benchmark)}\n"
        f"Activity to date: {total_cycles} recorded cycles, {total_trades} fills across "
        f"{len(rows)} sleeves.\n\n"
        "Paper sleeves only. Not a profit guarantee; forward paper results, not live P&L."
    )
    return subject, body


def render_digest_html(
    rows: list[DigestRow], benchmark: str, *, now: datetime, cohort_id: str | None = None
) -> str:
    """Return the HTML body for the digest email; ``cohort_id`` names the scope."""
    date = now.astimezone().strftime("%Y-%m-%d")
    subheading = f"{date} - cohort {cohort_id}" if cohort_id else date
    if not rows:
        inner = (
            f'<p style="margin:0;color:{emailfmt.MUTED}">No sleeves are configured yet. '
            "Create one with <code>schwab-trader sleeve create</code>.</p>"
        )
        return emailfmt.document(
            heading="Sleeve digest",
            subheading=subheading,
            inner_html=inner,
            footer="Paper sleeves only.",
        )

    headers = ["#", "sleeve", "strategy", "value", "return", f"vs {benchmark}", "maxDD", "sharpe"]
    aligns = ["right", "left", "left", "right", "right", "right", "right", "right"]
    table_rows: list[list[str]] = []
    for rank, row in enumerate(rows, start=1):
        ret = emailfmt.colored(f"{row.return_pct:+.2f}%", emailfmt.tone_for(float(row.return_pct)))
        if row.is_benchmark:
            excess = emailfmt.colored("bench", "muted")
        elif row.excess_pct is None:
            excess = emailfmt.colored("-", "muted")
        else:
            excess = emailfmt.colored(
                f"{row.excess_pct:+.2f}%", emailfmt.tone_for(float(row.excess_pct))
            )
        name = f"<strong>{emailfmt.esc(row.name)}</strong>"
        table_rows.append(
            [
                emailfmt.esc(rank),
                name,
                emailfmt.esc(row.strategy),
                emailfmt.esc(f"${row.value:,.2f}"),
                ret,
                excess,
                emailfmt.esc(f"-{row.max_drawdown_pct:.1f}%"),
                emailfmt.esc(row.sharpe if row.sharpe is not None else "-"),
            ]
        )

    total_cycles = sum(r.cycles for r in rows)
    total_trades = sum(r.trades for r in rows)
    inner = (
        emailfmt.data_table(headers, table_rows, aligns)
        + emailfmt.note(f"<strong>{emailfmt.esc(_verdict(rows, benchmark))}</strong>")
        + emailfmt.note(
            f"Activity to date: {total_cycles} recorded cycles, {total_trades} fills across "
            f"{len(rows)} sleeves."
        )
    )
    return emailfmt.document(
        heading="Sleeve digest",
        subheading=subheading,
        inner_html=inner,
        footer="Paper sleeves only - forward paper results, not live P&L.",
    )


def build_daily_digest(
    settings: Settings,
    *,
    benchmark: str = "bench-spy",
    now: datetime | None = None,
    cohort_id: str | None = None,
) -> NotifyMessage:
    """Build the digest as a :class:`NotifyMessage` (subject + plain text + HTML)."""
    now = now or datetime.now(UTC)
    rows = collect_digest_rows(settings, benchmark, cohort_id=cohort_id)
    subject, body = render_digest_text(rows, benchmark, now=now, cohort_id=cohort_id)
    html = render_digest_html(rows, benchmark, now=now, cohort_id=cohort_id)
    return NotifyMessage(subject=subject, body=body, html_body=html, category="info")
