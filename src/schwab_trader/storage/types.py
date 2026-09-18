"""Cross-dialect SQLAlchemy types with lossless Decimal behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy.types import TypeDecorator


class ExactNumeric(TypeDecorator[Decimal]):
    """Store exact decimals as PostgreSQL NUMERIC and SQLite text.

    SQLite's numeric affinity can round through a binary float.  Text is therefore
    the only lossless representation for the offline backend, while PostgreSQL's
    unbounded ``NUMERIC`` is used natively in the shared backend.
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(asdecimal=True))
        return dialect.type_descriptor(String(256))

    def process_bind_param(self, value: object, dialect: Dialect) -> object:
        if value is None:
            return None
        exact = value if isinstance(value, Decimal) else Decimal(str(value))
        return exact if dialect.name == "postgresql" else str(exact)

    def process_result_value(self, value: object, dialect: Dialect) -> Decimal | None:
        del dialect
        return None if value is None else Decimal(str(value))


class AwareTimestamp(TypeDecorator[datetime]):
    """Preserve aware timestamps on both supported backends.

    PostgreSQL stores native ``TIMESTAMP WITH TIME ZONE`` values. SQLite stores
    a normalized ISO-8601 string because its datetime adapter otherwise drops
    the UTC offset. Legacy timestamps with unknown timezone meaning are kept in
    dedicated ``source_*`` text columns instead of being passed to this type.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: object, dialect: Dialect) -> object:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("timestamp values must be datetime instances")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp values must include a timezone")
        normalized = value.astimezone(UTC)
        return normalized if dialect.name == "postgresql" else normalized.isoformat()

    def process_result_value(
        self, value: object, dialect: Dialect
    ) -> datetime | None:
        del dialect
        if value is None:
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("stored timestamp has no timezone")
        return parsed.astimezone(UTC)
