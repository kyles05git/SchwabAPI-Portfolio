"""Strategy validation registry: record walk-forward verdicts and gate promotion.

The backtester's promotion gates (Sharpe / Calmar / max-drawdown) have been
*advisory* - nothing stopped an unvalidated strategy from being trusted. This
turns them into a recorded, enforceable verdict: run a multi-window walk-forward,
and a strategy clears the **quality** bar only when enough folds pass and it beats
the benchmark. Live authorization additionally requires fresh, compatible provenance.

:func:`require_validated` is the enforcement primitive a live/autonomous order path
must call before trading a strategy with real money; today it also lets the CLI
surface which strategies are trustworthy. Verdicts are keyed by strategy + universe
(a strategy validated on one universe is not automatically trusted on another).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import BaseModel, computed_field

from schwab_trader.backtest import WalkForwardResult

DEFAULT_MAX_AGE_DAYS = 45
MANIFEST_SCHEMA_VERSION = 1


def _normalized_symbols(symbols: list[str]) -> list[str]:
    return sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})


def universe_hash(symbols: list[str]) -> str:
    """Stable identity for the exact symbols used by a validation run."""
    payload = "\n".join(_normalized_symbols(symbols)).encode()
    return hashlib.sha256(payload).hexdigest()


def configuration_fingerprint(
    *,
    strategy: str,
    symbols: list[str],
    factor: str,
    max_positions: int,
    max_position_fraction: Decimal,
    benchmark: str,
    settlement_t1: bool,
    leverage: Decimal,
) -> str:
    """Hash the runtime inputs that can change a strategy's decisions."""
    payload = {
        "factor": factor.strip().lower(),
        "max_position_fraction": str(max_position_fraction.normalize()),
        "max_positions": max_positions,
        "benchmark": benchmark.strip().upper(),
        "leverage": str(leverage.normalize()),
        "settlement_t1": settlement_t1,
        "strategy": strategy.strip().lower(),
        "symbols": _normalized_symbols(symbols),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def current_code_revision(repo_root: Path | None = None) -> str:
    """Best available code identity, preferring Git and falling back to package version.

    A dirty Git checkout is marked explicitly. Dirty validations are recorded for
    auditability but never authorize live/propose because the exact source cannot be
    reconstructed from a commit.
    """
    # Anchor Git discovery to this package, not the caller's working directory.
    # An installed wheel normally has no enclosing repository and will fall back
    # to package metadata below; a source checkout resolves to its own Git worktree.
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if revision:
            return f"git:{revision}{'+dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return f"package:{version('schwab-trader')}"
    except PackageNotFoundError:
        return "package:unknown"


class ValidationManifest(BaseModel):
    """Reproducibility and compatibility contract attached to a verdict."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    strategy: str
    universe_symbols: list[str]
    universe_hash: str
    factor: str = ""
    max_positions: int
    max_position_fraction: Decimal
    configuration_fingerprint: str
    benchmark: str
    window: int
    step: int
    requested_folds: int
    observed_folds: int
    cost_bps: float
    settlement_t1: bool
    leverage: Decimal
    dividends: bool
    next_bar_fill: bool
    point_in_time: bool
    first_fold_end: datetime | None
    last_data_date: datetime | None
    code_revision: str


class ValidationAssessment(BaseModel):
    """Explainable authorization result for live/propose gating."""

    authorized: bool
    reasons: list[str]

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "validated and compatible"


class PromotionVerdict(BaseModel):
    strategy: str
    universe: str
    created_at: datetime
    folds: int
    passing_folds: int
    pass_rate: float
    mean_return_pct: Decimal
    worst_return_pct: Decimal
    mean_excess_pct: Decimal | None
    min_pass_rate: float
    manifest: ValidationManifest | None = None

    @property
    def beats_benchmark(self) -> bool:
        """True when the strategy's average excess return over the benchmark is positive."""
        return self.mean_excess_pct is not None and self.mean_excess_pct > 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def validated(self) -> bool:
        """Whether this verdict clears the statistical strategy-quality bars.

        Computed from the stored components (not a frozen flag), so the current rule
        always applies - including to verdicts recorded before the rule tightened.
        Requires clearing the risk-gate consistency bar **and** beating the benchmark;
        fails closed when there is no benchmark comparison.
        """
        return (self.pass_rate >= self.min_pass_rate) and self.beats_benchmark


def verdict_from_walkforward(
    result: WalkForwardResult,
    *,
    strategy: str,
    universe: str,
    min_pass_rate: float,
    manifest: ValidationManifest | None = None,
    now: datetime | None = None,
) -> PromotionVerdict:
    """Build a promotion verdict from a walk-forward result.

    A strategy is **validated** only when it clears *both* bars: enough folds pass the
    risk gates (``pass_rate >= min_pass_rate``) **and** it beats the benchmark on
    average (positive mean excess return). A strategy that clears the risk gates but
    only matches or lags the benchmark is a low-drawdown market-matcher, not something
    worth risking real money on over simply buying the index - so it is not validated.
    Fails closed: when no benchmark was compared (mean excess is ``None``), the
    benchmark bar cannot be satisfied, so the strategy fails the quality bar.
    """
    total = len(result.folds)
    passing = sum(1 for fold in result.folds if fold.passed)
    return PromotionVerdict(
        strategy=strategy,
        universe=universe,
        created_at=now or datetime.now(UTC),
        folds=total,
        passing_folds=passing,
        pass_rate=result.pass_rate,
        mean_return_pct=result.mean_return_pct,
        worst_return_pct=result.worst_return_pct,
        mean_excess_pct=result.mean_excess_pct,
        min_pass_rate=min_pass_rate,
        manifest=manifest,
    )


def manifest_from_walkforward(
    result: WalkForwardResult,
    *,
    strategy: str,
    universe_symbols: list[str],
    factor: str,
    max_positions: int,
    max_position_fraction: Decimal,
    benchmark: str,
    window: int,
    step: int,
    requested_folds: int,
    cost_bps: float,
    settlement_t1: bool,
    leverage: Decimal,
    dividends: bool,
    next_bar_fill: bool = True,
    point_in_time: bool = True,
    code_revision: str | None = None,
) -> ValidationManifest:
    """Capture the full reproducibility contract for one walk-forward result."""
    symbols = _normalized_symbols(universe_symbols)
    fold_ends = [fold.end_day for fold in result.folds]
    return ValidationManifest(
        strategy=strategy.strip().lower(),
        universe_symbols=symbols,
        universe_hash=universe_hash(symbols),
        factor=factor.strip().lower(),
        max_positions=max_positions,
        max_position_fraction=max_position_fraction,
        configuration_fingerprint=configuration_fingerprint(
            strategy=strategy,
            symbols=symbols,
            factor=factor,
            max_positions=max_positions,
            max_position_fraction=max_position_fraction,
            benchmark=benchmark,
            settlement_t1=settlement_t1,
            leverage=leverage,
        ),
        benchmark=benchmark.strip().upper(),
        window=window,
        step=step,
        requested_folds=requested_folds,
        observed_folds=len(result.folds),
        cost_bps=cost_bps,
        settlement_t1=settlement_t1,
        leverage=leverage,
        dividends=dividends,
        next_bar_fill=next_bar_fill,
        point_in_time=point_in_time,
        first_fold_end=min(fold_ends) if fold_ends else None,
        last_data_date=max(fold_ends) if fold_ends else None,
        code_revision=code_revision or current_code_revision(),
    )


class PromotionStore:
    """SQLite history of promotion verdicts; exposes the latest per strategy+universe."""

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
                CREATE TABLE IF NOT EXISTS promotion_verdicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    verdict_json TEXT NOT NULL
                )
                """
            )

    def record(self, verdict: PromotionVerdict) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO promotion_verdicts (strategy, universe, created_at, verdict_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    verdict.strategy,
                    verdict.universe,
                    verdict.created_at.isoformat(),
                    verdict.model_dump_json(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def latest(self, strategy: str, universe: str) -> PromotionVerdict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT verdict_json FROM promotion_verdicts WHERE strategy = ? AND universe = ? "
                "ORDER BY id DESC LIMIT 1",
                (strategy, universe),
            ).fetchone()
        return PromotionVerdict.model_validate_json(row["verdict_json"]) if row else None

    def all_latest(self) -> list[PromotionVerdict]:
        """The most recent verdict for each (strategy, universe) pair."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT verdict_json FROM promotion_verdicts WHERE id IN "
                "(SELECT MAX(id) FROM promotion_verdicts GROUP BY strategy, universe) "
                "ORDER BY strategy, universe"
            ).fetchall()
        return [PromotionVerdict.model_validate_json(row["verdict_json"]) for row in rows]


def _quality_reason(verdict: PromotionVerdict) -> str | None:
    if verdict.validated:
        return None
    if verdict.pass_rate < verdict.min_pass_rate:
        return (
            f"'{verdict.strategy}' failed validation on '{verdict.universe}' "
            f"({verdict.pass_rate:.0%} of folds passed, need {verdict.min_pass_rate:.0%})"
        )
    if verdict.mean_excess_pct is None:
        return (
            f"'{verdict.strategy}' on '{verdict.universe}' has no benchmark comparison; "
            "cannot confirm it beats the benchmark"
        )
    return (
        f"'{verdict.strategy}' on '{verdict.universe}' clears the risk gates but does not beat "
        f"the benchmark (mean excess {verdict.mean_excess_pct:+.2f}%)"
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_days(value: datetime, now: datetime) -> int:
    return max(0, (_aware(now) - _aware(value)).days)


def assess_verdict(
    verdict: PromotionVerdict,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    expected_configuration_fingerprint: str | None = None,
    active_code_revision: str | None = None,
    now: datetime | None = None,
) -> ValidationAssessment:
    """Assess quality, provenance, freshness, data recency, and compatibility."""
    checked_at = now or datetime.now(UTC)
    quality_reason = _quality_reason(verdict)
    if quality_reason is not None:
        return ValidationAssessment(authorized=False, reasons=[quality_reason])

    manifest = verdict.manifest
    if manifest is None:
        return ValidationAssessment(
            authorized=False,
            reasons=["legacy validation has no provenance manifest; revalidate"],
        )

    reasons: list[str] = []
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        reasons.append(
            f"unsupported validation manifest schema {manifest.schema_version}; revalidate"
        )
    if manifest.universe_hash != universe_hash(manifest.universe_symbols):
        reasons.append("validation universe hash does not match its symbol list")
    recorded_fingerprint = configuration_fingerprint(
        strategy=manifest.strategy,
        symbols=manifest.universe_symbols,
        factor=manifest.factor,
        max_positions=manifest.max_positions,
        max_position_fraction=manifest.max_position_fraction,
        benchmark=manifest.benchmark,
        settlement_t1=manifest.settlement_t1,
        leverage=manifest.leverage,
    )
    if manifest.configuration_fingerprint != recorded_fingerprint:
        reasons.append("validation configuration fingerprint is internally inconsistent")
    if manifest.observed_folds != manifest.requested_folds:
        reasons.append(
            f"validation completed {manifest.observed_folds}/{manifest.requested_folds} "
            "requested folds"
        )
    verdict_age = _age_days(verdict.created_at, checked_at)
    if verdict_age > max_age_days:
        reasons.append(
            f"validation is {verdict_age} days old (maximum {max_age_days}); revalidate"
        )

    if manifest.last_data_date is None:
        reasons.append("validation manifest has no tested data end date; revalidate")
    else:
        data_age = _age_days(manifest.last_data_date, checked_at)
        if data_age > max_age_days:
            reasons.append(
                f"tested market data is {data_age} days old (maximum {max_age_days}); revalidate"
            )

    if manifest.code_revision.endswith("+dirty"):
        reasons.append("validation used a dirty working tree and is not reproducible")
    if active_code_revision is not None and manifest.code_revision != active_code_revision:
        reasons.append(
            f"code revision changed ({manifest.code_revision} -> {active_code_revision})"
        )
    if (
        expected_configuration_fingerprint is not None
        and manifest.configuration_fingerprint != expected_configuration_fingerprint
    ):
        reasons.append("strategy settings, factor, or exact universe changed")

    return ValidationAssessment(authorized=not reasons, reasons=reasons)


def require_validated(
    store: PromotionStore,
    strategy: str,
    universe: str,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    expected_configuration_fingerprint: str | None = None,
    active_code_revision: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Gate primitive: may this exact strategy runtime trade real money?

    Fails closed on missing/failed/legacy/stale/incompatible records. Returns an
    explainable ``(ok, reason)`` tuple for CLI and audit output.
    """
    verdict = store.latest(strategy, universe)
    if verdict is None:
        return False, f"no validation on record for '{strategy}' on '{universe}'"
    assessment = assess_verdict(
        verdict,
        max_age_days=max_age_days,
        expected_configuration_fingerprint=expected_configuration_fingerprint,
        active_code_revision=active_code_revision,
        now=now,
    )
    return assessment.authorized, assessment.reason
