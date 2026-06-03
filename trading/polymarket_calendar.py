"""
Single source for Polymarket endDate filtering (UTC calendar).

Used by:
- `polymarket_markets_export.export_markets` (re-exports `utc_today_tomorrow_dates`; CSV is **UTC today** only)
- `engine.scanner.fetch_crypto_price_markets` (live matrix)

Scanner / `poly_end_dates_today_tomorrow_utc`: Polymarket `endDate` **today or tomorrow** in UTC.
CSV export (`csv_refresh`): Polymarket rows are **today**-only by default (`polymarket_markets_today_utc.csv`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, Set


def utc_today_tomorrow_dates(now: Optional[datetime] = None) -> tuple[date, date]:
    """UTC calendar **today** and **tomorrow** (scanner window for live matching)."""
    n = now or datetime.now(timezone.utc)
    t = n.date()
    return t, t + timedelta(days=1)


def poly_end_dates_today_tomorrow_utc(now: Optional[datetime] = None) -> Set[date]:
    """`{today, tomorrow}` in UTC — use for Polymarket `endDate.date()` inclusion tests."""
    t, t2 = utc_today_tomorrow_dates(now)
    return {t, t2}
