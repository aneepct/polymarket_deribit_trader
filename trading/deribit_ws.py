"""
Real-time Deribit WebSocket → Redis ticker store.

Subscribes to ``ticker.{instrument}.raw`` for all BTC/ETH options with
today/tomorrow expiry, plus ``deribit_price_index.{btc_usd,eth_usd}``.

Each push is written to Redis:
    deribit:ticker:{INSTRUMENT_NAME}   JSON blob  TTL=300s
    deribit:index:{index_name}         price str  TTL=300s

Daily rollover at 08:05 UTC:
    send public/unsubscribe_all → REST re-fetch instruments → reconnect.

Graceful shutdown (CancelledError / SIGTERM):
    send public/unsubscribe_all before closing the WS.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import websockets
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

WS_URL = "wss://www.deribit.com/ws/api/v2"
REST_BASE = "https://www.deribit.com/api/v2/public"

TICKER_TTL = 300          # 5-minute Redis TTL
ROLLOVER_HOUR = 8         # 08:05 UTC daily rollover
ROLLOVER_MINUTE = 5
RECONNECT_BACKOFF = [5, 15, 30, 60]   # seconds between reconnect attempts


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_redis_url() -> str:
    from django.conf import settings
    return getattr(settings, "REDIS_URL", "redis://localhost:6379/0")


def _next_rollover() -> datetime:
    now = datetime.now(timezone.utc)
    candidate = now.replace(
        hour=ROLLOVER_HOUR, minute=ROLLOVER_MINUTE, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def _fetch_instruments(currency: str) -> list[str]:
    """Return today + tomorrow expiry instrument names for *currency* via REST."""
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    url = f"{REST_BASE}/get_instruments"

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url, params={"currency": currency, "kind": "option", "expired": "false"}
        )
        resp.raise_for_status()
        result = resp.json().get("result", [])

    instruments: list[str] = []
    for inst in result:
        name = inst.get("instrument_name", "")
        parts = name.split("-")
        if len(parts) < 4:
            continue
        try:
            exp_date = datetime.strptime(parts[1].upper(), "%d%b%y").date()
        except ValueError:
            continue
        if exp_date in (today, tomorrow):
            instruments.append(name)

    return instruments


def _build_channels(instruments: list[str]) -> list[str]:
    channels = [f"ticker.{i}.100ms" for i in instruments]
    # Add index, DVOL, and perpetual funding channels per currency
    currencies_seen: set[str] = set()
    for i in instruments:
        currencies_seen.add(i.split("-")[0].lower())
    for cur in currencies_seen:
        channels.append(f"deribit_price_index.{cur}_usd")
        channels.append(f"deribit_volatility_index.{cur}_usd")        # DVOL
        channels.append(f"ticker.{cur.upper()}-PERPETUAL.100ms")       # perp funding
    return channels


async def _send_json(ws: Any, msg: dict) -> None:
    await ws.send(json.dumps(msg))


async def _subscribe(ws: Any, channels: list[str]) -> None:
    await _send_json(ws, {
        "jsonrpc": "2.0",
        "method": "public/subscribe",
        "params": {"channels": channels},
        "id": 1,
    })
    logger.debug("[deribit_ws] subscribe sent (%d channels)", len(channels))


async def _unsubscribe_all(ws: Any) -> None:
    """Send public/unsubscribe_all and wait briefly for the ack."""
    try:
        await _send_json(ws, {
            "jsonrpc": "2.0",
            "method": "public/unsubscribe_all",
            "params": {},
            "id": 2,
        })
        await asyncio.wait_for(ws.recv(), timeout=3.0)
        logger.info("[deribit_ws] unsubscribe_all ack received")
    except Exception as exc:
        logger.debug("[deribit_ws] unsubscribe_all ack not received: %s", exc)


# ── Session ───────────────────────────────────────────────────────────────────

async def _ws_session(redis_client: Any, currencies: list[str]) -> None:
    """
    Open one WebSocket session, subscribe to all channels, and process
    messages until the daily rollover time or an exception.

    Raises CancelledError on shutdown (after sending unsubscribe_all).
    Returns normally on rollover (caller re-connects with fresh instruments).
    """
    all_instruments: list[str] = []
    for cur in currencies:
        insts = await _fetch_instruments(cur)
        all_instruments.extend(insts)
        logger.info("[deribit_ws] %s: %d instruments for today/tomorrow", cur, len(insts))

    channels = _build_channels(all_instruments)
    logger.info("[deribit_ws] Connecting — %d channels total", len(channels))

    rollover_at = _next_rollover()

    async with websockets.connect(
        WS_URL, ping_interval=30, ping_timeout=10
    ) as ws:
        await _subscribe(ws, channels)
        logger.info("[deribit_ws] Subscribed. Rollover at %s UTC", rollover_at.strftime("%H:%M"))

        try:
            while True:
                now = datetime.now(timezone.utc)
                if now >= rollover_at:
                    logger.info("[deribit_ws] Daily rollover — sending unsubscribe_all")
                    await _unsubscribe_all(ws)
                    return  # clean exit; outer loop will reconnect with fresh instruments

                time_to_rollover = (rollover_at - now).total_seconds()
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=min(time_to_rollover, 30.0)
                    )
                except asyncio.TimeoutError:
                    continue  # loop back to rollover check

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("method") != "subscription":
                    continue

                params: dict = msg.get("params", {})
                channel: str = params.get("channel", "")
                data: dict = params.get("data", {})

                if channel.startswith("ticker."):
                    instrument_name = data.get("instrument_name")
                    if not instrument_name:
                        continue
                    greeks: dict = data.get("greeks") or {}

                    # Perpetual contract — store funding rate separately
                    if instrument_name.endswith("-PERPETUAL"):
                        currency = instrument_name.split("-")[0]  # BTC or ETH
                        funding = data.get("current_funding") or data.get("funding_8h")
                        if funding is not None:
                            await redis_client.set(
                                f"deribit:perp:{currency}",
                                json.dumps({"funding_8h": float(funding), "timestamp": data.get("timestamp")}),
                                ex=TICKER_TTL,
                            )
                        continue

                    row: dict[str, Any] = {
                        "instrument_name": instrument_name,
                        "mark_iv":         data.get("mark_iv"),
                        "delta":           greeks.get("delta") or data.get("delta"),
                        "gamma":           greeks.get("gamma"),
                        "vega":            greeks.get("vega"),
                        "theta":           greeks.get("theta"),
                        "best_bid_price":  data.get("best_bid_price"),
                        "best_ask_price":  data.get("best_ask_price"),
                        "mark_price":      data.get("mark_price"),
                        "index_price":     data.get("index_price") or data.get("underlying_price"),
                        "open_interest":   data.get("open_interest"),   # OI filter
                        "timestamp":       data.get("timestamp"),
                    }
                    # Parse fields from instrument name: BTC-27DEC24-100000-C
                    parts = instrument_name.split("-")
                    if len(parts) >= 4:
                        row["expiry_str"]  = parts[1]
                        row["option_type"] = parts[3]
                        try:
                            row["strike"] = float(parts[2])
                        except ValueError:
                            row["strike"] = None

                    await redis_client.set(
                        f"deribit:ticker:{instrument_name}",
                        json.dumps(row),
                        ex=TICKER_TTL,
                    )

                elif channel.startswith("deribit_volatility_index."):
                    index_name = data.get("index_name", "")  # e.g. btc_usd
                    dvol = data.get("volatility")
                    if index_name and dvol is not None:
                        await redis_client.set(
                            f"deribit:dvol:{index_name}",
                            str(dvol),
                            ex=TICKER_TTL,
                        )

                elif channel.startswith("deribit_price_index."):
                    index_name = data.get("index_name", "")
                    price = data.get("price")
                    if index_name and price is not None:
                        await redis_client.set(
                            f"deribit:index:{index_name}",
                            str(price),
                            ex=TICKER_TTL,
                        )

        except asyncio.CancelledError:
            logger.info("[deribit_ws] Shutdown signal — sending unsubscribe_all")
            await _unsubscribe_all(ws)
            raise


# ── Public entry point ────────────────────────────────────────────────────────

async def deribit_ws_loop(currencies: list[str] | None = None) -> None:
    """
    Persistent WebSocket loop.  Run as a concurrent task alongside
    ``auto_trader_loop``.  Reconnects automatically on disconnect, and
    rolls over daily at 08:05 UTC to refresh instrument subscriptions.
    """
    if currencies is None:
        currencies = ["BTC", "ETH"]

    redis_client = aioredis.from_url(_get_redis_url(), decode_responses=True)
    backoff_idx = 0

    try:
        while True:
            try:
                await _ws_session(redis_client, currencies)
                # Clean return means rollover — reconnect immediately
                backoff_idx = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.warning(
                    "[deribit_ws] Session error (%s) — reconnecting in %ds", exc, delay
                )
                backoff_idx += 1
                await asyncio.sleep(delay)
    finally:
        await redis_client.aclose()
        logger.info("[deribit_ws] Redis connection closed")
