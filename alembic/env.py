"""Alembic environment using the protected application database setting."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from schwab_trader.config import Settings
from schwab_trader.storage.schema import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the migration target: an explicitly injected URL, else ``Settings``.

    ``alembic.ini`` deliberately leaves ``sqlalchemy.url`` unset, so a value here can
    only have been supplied by a caller that meant it — which is what lets the offline
    migration tests run the real chain against a throwaway SQLite file instead of
    reaching for the operator's configured database. Production is unchanged: with no
    injected URL this still reads the protected setting and never logs it.
    """
    injected = (config.get_main_option("sqlalchemy.url") or "").strip()
    raw = injected or Settings().database_url.get_secret_value().strip()
    if not raw:
        raise RuntimeError(
            "SCHWAB_DATABASE_URL is required for schema migration; "
            "configure it locally and do not paste it into chat"
        )
    parsed = make_url(raw)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Generate SQL without connecting while keeping bound values out of output."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=False,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        _database_url(),
        future=True,
        hide_parameters=True,
        pool_pre_ping=True,
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
