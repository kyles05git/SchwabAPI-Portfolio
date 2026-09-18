"""On-disk cache for daily price history.

Momentum and backtests fetch daily candles for many symbols (e.g. 74 large caps),
which is slow to repeat. Since daily bars only gain one new point per day, this
cache re-fetches a symbol at most once per calendar day and serves slices from
the stored series - making repeated sleeve runs and backtest iterations fast.

There are two freshness modes, and the difference matters:

- **Convenience** (``settled_through`` unset). One fetch per UTC calendar day. Fine for
  research, backtest iteration, and ad-hoc reads.
- **Settled evidence** (``settled_through=<session>``). A cached payload may be served
  only if it *covers* that session **and** was retrieved after that session's official
  close. Anything else is re-fetched.

The second mode exists because the first is unsound for an official run. Keying on the
fetch date alone means a response captured at 10:00 ET is reused verbatim at 16:30 ET
the same day, so a post-close cohort run is handed a payload that could not possibly
contain the day's settled bar - and then reports it as stale. A pre-close response is
not end-of-day evidence, and this cache refuses to pretend otherwise. A payload written
by an older version carries no retrieval timestamp, so it fails that test closed.

Cache files are plain JSON under a local directory; this module wraps
:func:`schwab_trader.market_data.get_price_history`.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from schwab_trader import client as api
from schwab_trader import market_calendar as mc
from schwab_trader import market_data
from schwab_trader.market_data import Candle


def _candle_to_dict(c: Candle) -> dict[str, object]:
    return {
        "d": c.date.isoformat(),
        "o": str(c.open) if c.open is not None else None,
        "h": str(c.high) if c.high is not None else None,
        "l": str(c.low) if c.low is not None else None,
        "c": str(c.close),
        "v": c.volume,
        "s": c.source,
    }


def _candle_from_dict(symbol: str, d: dict[str, object]) -> Candle:
    def _dec(key: str) -> Decimal | None:
        value = d.get(key)
        return Decimal(str(value)) if value is not None else None

    return Candle(
        symbol=symbol,
        date=datetime.fromisoformat(str(d["d"])),
        open=_dec("o"),
        high=_dec("h"),
        low=_dec("l"),
        close=Decimal(str(d["c"])),
        volume=int(str(d.get("v") or 0)),
        source=str(d.get("s") or market_data.SCHWAB_DAILY_HISTORY_SOURCE),
    )


def session_close_utc(session: date) -> datetime:
    """The official close of ``session`` as an aware UTC instant (13:00 ET early)."""
    return mc.eastern_to_utc(datetime.combine(session, mc.session_close(session)))


def _covered_session(payload: dict[str, Any]) -> date | None:
    """The latest session a cached payload holds a candle for."""
    stored = payload.get("covers_session")
    if isinstance(stored, str):
        with contextlib.suppress(ValueError):
            return date.fromisoformat(stored)
    candles = payload.get("candles")
    if not isinstance(candles, list) or not candles:
        return None
    latest: date | None = None
    for item in candles:
        if not isinstance(item, dict) or not isinstance(item.get("d"), str):
            continue
        try:
            when = datetime.fromisoformat(item["d"]).date()
        except ValueError:
            continue
        if latest is None or when > latest:
            latest = when
    return latest


def _retrieved_at(payload: dict[str, Any]) -> datetime | None:
    """When a cached payload was actually retrieved, or ``None`` for a legacy file."""
    stored = payload.get("fetched_at")
    if not isinstance(stored, str):
        return None
    try:
        stamp = datetime.fromisoformat(stored)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


class HistoryCache:
    """Per-day on-disk cache for :func:`market_data.get_price_history`."""

    def __init__(self, cache_dir: Path) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.dir / f"{symbol.upper()}.json"

    @staticmethod
    def _is_settled_evidence(payload: dict[str, Any], session: date) -> bool:
        """Whether a cached payload is usable as ``session``'s settled end-of-day data.

        Both conditions are necessary. Coverage alone would accept a payload fetched
        mid-session, whose last bar is still forming; a retrieval time alone would
        accept a post-close fetch that the provider had not yet published the settled
        candle into. A legacy payload with no recorded retrieval time fails closed.
        """
        covered = _covered_session(payload)
        if covered is None or covered < session:
            return False
        retrieved = _retrieved_at(payload)
        return retrieved is not None and retrieved > session_close_utc(session)

    def get(
        self,
        client: api.SchwabClient,
        symbol: str,
        *,
        days: int = 180,
        settled_through: date | None = None,
        now: datetime | None = None,
    ) -> list[Candle]:
        """Return daily candles for ``symbol``, using the cache when it is valid.

        With ``settled_through`` set the cache is only reused when it holds genuine
        settled end-of-day evidence for that exchange session (see
        :meth:`_is_settled_evidence`); otherwise the provider is queried again, so a
        retry after the session's bar is published picks it up.
        """
        symbol = symbol.strip().upper()
        stamp = now or datetime.now(UTC)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        today = stamp.date().isoformat()
        path = self._path(symbol)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            payload = raw if isinstance(raw, dict) else None
            count = payload.get("count", 0) if payload is not None else 0
            count = count if isinstance(count, int) else 0
            if payload is not None and count >= days:
                usable = (
                    self._is_settled_evidence(payload, settled_through)
                    if settled_through is not None
                    else payload.get("fetched") == today
                )
                if usable:
                    candles = [_candle_from_dict(symbol, d) for d in payload["candles"]]
                    return candles[-days:] if days > 0 else candles

        candles = market_data.get_price_history(client, symbol, days=days)
        covers = max((c.date.date() for c in candles), default=None)
        fresh: dict[str, Any] = {
            "fetched": today,
            "fetched_at": stamp.isoformat(),
            "covers_session": None if covers is None else covers.isoformat(),
            "count": len(candles),
            "candles": [_candle_to_dict(c) for c in candles],
        }
        # Cache is best-effort; a write failure must not break the fetch.
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(fresh), encoding="utf-8")
        return candles

    def clear(self) -> int:
        """Delete all cached files; return how many were removed."""
        removed = 0
        for file in self.dir.glob("*.json"):
            try:
                file.unlink()
                removed += 1
            except OSError:
                pass
        return removed
