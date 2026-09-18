"""Read-only view of what is stored in the configured shared database (Neon).

Connects using SCHWAB_DATABASE_URL from the local environment/.env, lists every
table with its row count, and reports the Alembic schema version. Writes nothing
and never prints the connection URL or any secret.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from schwab_trader.config import Settings
from schwab_trader.storage.database import Database


def main() -> None:
    settings = Settings()
    raw = settings.database_url.get_secret_value().strip()
    if not raw:
        print("SCHWAB_DATABASE_URL is not set; nothing to inspect.")
        return
    if raw.startswith("sqlite"):
        print("SCHWAB_DATABASE_URL points at SQLite, not Neon/PostgreSQL.")
        return

    db = Database(raw)
    try:
        inspector = inspect(db.engine)
        tables = sorted(inspector.get_table_names())
        print(f"Connected. dialect={db.dialect}, tables={len(tables)}\n")
        print(f"{'table':40}  {'rows':>12}")
        print(f"{'-' * 40}  {'-' * 12}")
        with db.session() as session:
            for table in tables:
                count = session.execute(
                    text(f'SELECT count(*) FROM "{table}"')
                ).scalar_one()
                print(f"{table:40}  {count:>12,}")
            version = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        print(f"\nalembic schema version: {version}")
    finally:
        db.dispose()


if __name__ == "__main__":
    main()
