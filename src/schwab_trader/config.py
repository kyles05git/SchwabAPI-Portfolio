"""Load and validate environment-based configuration.

Configuration is read from environment variables (prefixed ``SCHWAB_``) and an
optional local ``.env`` file. Secrets are wrapped in :class:`~pydantic.SecretStr`
so they are not accidentally rendered in logs or ``repr`` output.

Risk-related settings are intentionally conservative by default and are designed
to *fail closed*: live trading is disabled unless every gate is explicitly and
correctly set elsewhere (see :mod:`schwab_trader.risk`).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration.

    Values are populated from ``SCHWAB_``-prefixed environment variables or a
    local ``.env`` file. Unknown variables are ignored.
    """

    model_config = SettingsConfigDict(
        env_prefix="SCHWAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # --- Credentials (secret) -------------------------------------------------
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    callback_url: str = "https://127.0.0.1:8182/callback"

    # --- Local storage paths --------------------------------------------------
    token_path: Path = Path("./data/schwab_tokens.json")
    database_url: SecretStr = SecretStr("")
    state_db_path: Path = Path("./data/state.sqlite3")
    paper_db_path: Path = Path("./data/paper.sqlite3")
    agent_eval_db_path: Path = Path("./data/agent_eval.sqlite3")
    research_db_path: Path = Path("./data/research.sqlite3")
    usage_db_path: Path = Path("./data/usage.sqlite3")
    # Persistent long-history daily price panel (for backtests / ML feature building).
    price_panel_db_path: Path = Path("./data/prices.sqlite3")
    # Accumulating intraday (minute-bar) panel for day-trading research.
    intraday_panel_db_path: Path = Path("./data/intraday.sqlite3")
    # Local SEC EDGAR fundamentals store (point-in-time XBRL facts).
    sec_db_path: Path = Path("./data/sec.sqlite3")
    # Directory holding named comparison sleeves (each its own paper + eval store).
    sleeves_dir: Path = Path("./data/sleeves")
    # On-disk daily price-history cache (re-fetched at most once per day).
    history_cache_dir: Path = Path("./data/history_cache")
    # Exact constituents and derived daily evidence used by official paper cohorts.
    market_data_evidence_db_path: Path = Path("./data/market_data_evidence.sqlite3")
    # Historical-replay RESEARCH evidence. Deliberately a separate file from the
    # official evidence store above: replay records are never official cohort evidence.
    historical_replay_db_path: Path = Path("./data/historical_replay.sqlite3")
    # Append-only accounting reviews, operator notes, and keep/modify/pause/retire
    # decisions for official cohorts. Used only when no shared database is configured;
    # with one, these records live beside the cohort runs they describe.
    cohort_review_db_path: Path = Path("./data/cohort_reviews.sqlite3")
    log_path: Path = Path("./logs/schwab_trader.log")
    migration_backup_dir: Path = Path("./data/migration-backups")

    # --- Official cohort operations (paper-only scheduling) --------------------
    # Exactly one machine may run the scheduled official cohort job; every other
    # machine is a read-only dashboard client. This defaults to False so an
    # unconfigured checkout is never mistaken for the writer, and the readiness
    # check fails closed rather than guessing.
    cohort_writer: bool = False
    # Cohort whose daily run the operations commands report on. Empty means the
    # command requires an explicit --cohort argument (no guessing between cohorts).
    cohort_id: str = ""

    # --- SEC EDGAR (free; no key, but their fair-access policy wants a contact) --
    # Set to "Your Name your-email@example.com" so SEC can identify your traffic.
    sec_user_agent: str = ""

    # --- Paper-trading sleeve (simulated money only) --------------------------
    paper_starting_cash: Decimal = Field(default=Decimal("5000.00"), ge=0)

    # --- AI agent (Anthropic API; optional, for the 'llm' strategy) -----------
    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-haiku-4-5"  # per-cycle execution (cheap, fast)
    llm_research_model: str = "claude-opus-4-8"  # infrequent, high-stakes research
    # Free FRED API key for macro context (fredaccount.stlouisfed.org/apikeys).
    fred_api_key: SecretStr = SecretStr("")
    # Trading universe for the agent (comma-separated). Empty = built-in default.
    agent_universe: str = ""
    # Agent sizing used when no research spec is active (a spec sets its own).
    # The model chooses how much to buy of each name; this is only a
    # diversification backstop: no single position may exceed this fraction of
    # the sleeve's total value. 1.0 disables the cap.
    agent_max_positions: int = Field(default=20, ge=1)
    agent_max_position_fraction: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)

    # --- Autonomous safety layer (bounded-autonomy gate; CLAUDE.md rule 14) ----
    # A present kill-switch file halts all autonomous loops immediately.
    kill_switch_path: Path = Path("./data/KILL_SWITCH")
    agent_activity_db_path: Path = Path("./data/agent_activity.sqlite3")
    # Strategy validation registry (walk-forward promotion verdicts).
    promotion_db_path: Path = Path("./data/promotion.sqlite3")
    # A strategy is "validated" when at least this fraction of walk-forward folds
    # clear the promotion gates (Sharpe/Calmar/drawdown).
    promotion_min_pass_rate: float = Field(default=0.6, ge=0, le=1)
    # A statistically passing verdict stops authorizing live/propose when either
    # the verdict or its latest tested market session is older than this many days.
    promotion_max_age_days: int = Field(default=45, ge=1, le=365)
    # Total capital the autonomous agent sleeve may deploy (0 = unset).
    agent_capital_cap: Decimal = Field(default=Decimal("0"), ge=0)
    # Stop autonomous trading for the day after this realized loss (0 = off).
    agent_daily_loss_limit: Decimal = Field(default=Decimal("0"), ge=0)
    # Max autonomous trades per day (0 = off).
    agent_max_trades_per_day: int = Field(default=0, ge=0)

    # --- Notifications (SMTP email; optional, for the notify-and-approve runner) --
    # Used to push proposed orders and alerts out-of-band. The password is a secret
    # and is registered for log redaction. Leave smtp_host/notify_to empty to
    # disable the channel (a NullNotifier is used and nothing is sent).
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)  # 587 = STARTTLS submission
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_use_tls: bool = True  # issue STARTTLS on the configured port before login
    notify_from: str = ""  # From: address (defaults to smtp_username when empty)
    notify_to: str = ""  # recipient address(es), comma-separated

    # --- Notify-and-approve runner (idea #1) ---------------------------------
    # Pending live-order approvals live here; each token is single-use, bound to
    # the exact order, and valid for approval_ttl_minutes before it expires.
    approval_db_path: Path = Path("./data/approvals.sqlite3")
    approval_ttl_minutes: int = Field(default=60, ge=1)

    # --- Tax-aware execution (idea #6): local tax-lot ledger ------------------
    # Open/closed tax lots and realized sales for after-tax awareness on the real
    # taxable account. Populated from live fills; used for holding-period and
    # short-/long-term gain estimates in the order preview and wash-sale checks.
    tax_lots_db_path: Path = Path("./data/taxlots.sqlite3")
    # Lot-relief method for realized-gain estimates. FIFO matches the common broker
    # default; other methods (HIFO/specific-lot) can be added later.
    tax_lot_method: str = "FIFO"
    # Wash-sale window: a loss sale is disallowed if the same security was (re)bought
    # within this many calendar days before or after the sale (IRS rule: 30 days).
    wash_sale_window_days: int = Field(default=30, ge=0)

    # --- Account selection (secret; chosen in a later phase) ------------------
    account_hash: SecretStr = SecretStr("")

    # --- Safety gates ---------------------------------------------------------
    dry_run: bool = True
    trading_enabled: bool = False
    require_confirmation: bool = True

    # --- Risk limits ----------------------------------------------------------
    max_order_quantity: int = Field(default=1, ge=0)
    max_order_notional: Decimal = Field(default=Decimal("100.00"), ge=0)
    allowed_symbols: str = ""
    rate_limit_per_minute: int = Field(default=100, ge=1, le=120)

    # Reject quotes older than this many seconds for any check that uses a quote.
    quote_max_age_seconds: int = Field(default=60, ge=1)

    # Treat an identical order within this many seconds as a duplicate.
    duplicate_window_seconds: int = Field(default=300, ge=1)

    @field_validator("callback_url")
    @classmethod
    def _validate_callback_url(cls, value: str) -> str:
        """The callback URL must be an ``https://127.0.0.1`` URL per the portal.

        We do not silently rewrite the protocol, host, port, or path; we only
        reject values that are obviously wrong so misconfiguration fails early.
        """
        if not value.startswith("https://"):
            msg = "SCHWAB_CALLBACK_URL must use https://"
            raise ValueError(msg)
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, value: object) -> object:
        """Accept SQLite or psycopg PostgreSQL URLs and require remote TLS.

        The value remains a :class:`SecretStr`; validation errors intentionally
        describe only the required shape and never echo the configured URL.
        """
        if value is None:
            return ""
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if not raw.strip():
            return ""
        parsed = urlsplit(raw)
        if parsed.scheme.startswith("sqlite"):
            return raw
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError(
                "SCHWAB_DATABASE_URL must use sqlite, postgresql, or postgresql+psycopg"
            )
        host = (parsed.hostname or "").casefold()
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if host not in local_hosts:
            sslmode = parse_qs(parsed.query).get("sslmode", [""])[-1].casefold()
            if sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError(
                    "non-local SCHWAB_DATABASE_URL must require TLS with "
                    "sslmode=require, verify-ca, or verify-full"
                )
        return raw

    @property
    def has_credentials(self) -> bool:
        """True when both a client id and client secret are configured."""
        return bool(self.client_id) and bool(self.client_secret.get_secret_value())

    @property
    def has_shared_database(self) -> bool:
        """True when provider-neutral shared persistence is configured."""
        return bool(self.database_url.get_secret_value().strip())

    @property
    def has_account_selected(self) -> bool:
        """True when an account hash has been explicitly configured."""
        return bool(self.account_hash.get_secret_value())

    @property
    def has_llm_key(self) -> bool:
        """True when an Anthropic API key is configured for the LLM strategy."""
        return bool(self.anthropic_api_key.get_secret_value())

    @property
    def has_fred_key(self) -> bool:
        """True when a FRED API key is configured for macro context."""
        return bool(self.fred_api_key.get_secret_value())

    @property
    def has_smtp(self) -> bool:
        """True when enough SMTP settings are present to deliver a notification."""
        return bool(self.smtp_host and self.notify_to_list)

    @property
    def effective_notify_from(self) -> str:
        """The From: address for notifications (falls back to the SMTP username)."""
        return self.notify_from.strip() or self.smtp_username.strip()

    @property
    def notify_to_list(self) -> list[str]:
        """Configured notification recipients (empty if unset)."""
        return [addr.strip() for addr in self.notify_to.split(",") if addr.strip()]

    @property
    def effective_sec_user_agent(self) -> str:
        """User-Agent for SEC EDGAR requests (their policy wants a contact string)."""
        configured = self.sec_user_agent.strip()
        return configured or "schwab-trader personal-use (set SCHWAB_SEC_USER_AGENT)"

    @property
    def agent_universe_list(self) -> list[str]:
        """Configured agent universe as an upper-cased list (empty if unset).

        Accepts a preset name (e.g. ``large-cap``) or comma-separated tickers.
        """
        from schwab_trader import universes

        raw = self.agent_universe.strip()
        if not raw:
            return []
        preset = universes.get_preset(raw)
        if preset is not None:
            return preset
        return [t.strip().upper() for t in raw.split(",") if t.strip()]

    @property
    def quote_max_age(self) -> timedelta:
        """Maximum acceptable age of a quote before it is treated as stale."""
        return timedelta(seconds=self.quote_max_age_seconds)

    @property
    def duplicate_window(self) -> timedelta:
        """Window within which an identical order is treated as a duplicate."""
        return timedelta(seconds=self.duplicate_window_seconds)

    @property
    def allowed_symbol_set(self) -> frozenset[str]:
        """The configured allow-list of symbols, normalized to upper case.

        An empty set means "no explicit allow-list configured"; symbol
        allow-list enforcement is applied by :mod:`schwab_trader.risk` only when
        this set is non-empty.
        """
        return frozenset(
            token.strip().upper() for token in self.allowed_symbols.split(",") if token.strip()
        )

    def masked_account_tail(self) -> str:
        """Return a redacted account identifier suitable for display.

        Never returns the full account hash. Shows only the last four characters.
        """
        raw = self.account_hash.get_secret_value()
        if not raw:
            return "(none selected)"
        return f"****{raw[-4:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance loaded from the environment."""
    return Settings()


def set_env_value(key: str, value: str, *, env_path: Path = Path(".env")) -> None:
    """Set ``KEY=value`` in the local ``.env`` file, preserving other lines.

    Replaces an existing assignment for ``key`` or appends a new one. Writes
    atomically and restricts the file to the owner (0600, best-effort on Windows).
    This is used to persist the selected account hash into the protected local
    ``.env`` - never into source control (``.env`` is git-ignored).
    """
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    prefix = f"{key}="
    replaced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")

    content = "\n".join(lines) + "\n"
    parent = env_path.parent if str(env_path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".env-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):  # permissions best-effort on Windows
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, env_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
