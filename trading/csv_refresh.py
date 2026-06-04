"""
Signal refresh loop for polymarket_deribit_trader.

Calls refresh_latest_signals() every interval_seconds (default 60).
Polymarket data is fetched live from the Gamma API inside
refresh_latest_signals(), and Deribit data comes from Redis
(written in real-time by deribit_ws_loop).

No subprocesses, no CSV files.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def csv_refresh_loop(*, interval_seconds: int = 60) -> None:
    logger.info("[csv_refresh] Starting — signal refresh every %ds", interval_seconds)
    while True:
        try:
            from trading.csv_signals import refresh_latest_signals
            await refresh_latest_signals()
            logger.info("[csv_refresh] Signals refreshed.")
        except Exception as exc:
            logger.warning("[csv_refresh] refresh error: %s", exc)
        await asyncio.sleep(interval_seconds)

