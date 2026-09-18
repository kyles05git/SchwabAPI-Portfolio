"""Secure SQLAlchemy engine construction for SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from schwab_trader.storage.schema import Base


class Database:
    """A small provider-neutral transaction boundary.

    The URL is kept in ``SecretStr`` and is never included in application output.
    SQL parameters are hidden at the engine layer as a second defense against
    accidental record disclosure.
    """

    def __init__(self, url: SecretStr | str, *, create_schema: bool = False) -> None:
        raw = url.get_secret_value() if isinstance(url, SecretStr) else url
        parsed = make_url(raw)
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        self._engine = create_engine(
            parsed,
            future=True,
            hide_parameters=True,
            pool_pre_ping=parsed.get_backend_name() == "postgresql",
        )
        if parsed.get_backend_name() == "sqlite":
            event.listen(self._engine, "connect", self._enable_sqlite_foreign_keys)
        self._sessions = sessionmaker(
            bind=self._engine,
            class_=Session,
            expire_on_commit=False,
            future=True,
        )
        if create_schema:
            Base.metadata.create_all(self._engine)

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit one unit of work or roll it back completely."""
        with self._sessions() as session, session.begin():
            yield session

    def dispose(self) -> None:
        self._engine.dispose()
