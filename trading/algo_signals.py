"""
algo_signals.py — Signal computation using the delta-interpolation algorithm
from algorithms_2026-05-28.py (the Kate dashboard formula).

Fair value formula:
  1. Build {strike: |delta|} call chains from Deribit Redis data.
  2. Time-rescale each delta from its chain horizon to the Polymarket horizon
     using Phi(Phi^{-1}(delta) * sqrt(h_src / h_tgt)).
  3. Band prob  = max(0, delta(K_lo) - delta(K_hi)).
     LessThan p = max(0, 1 - delta(K)).
  4. Calendar-weight across today/tomorrow chains.

Data sources (same as csv_signals.py):
  - Deribit chains: Redis deribit:ticker:* written by deribit_ws_loop
  - Polymarket:     Gamma API

get_latest_signals() returns the same dict format as csv_signals.py so that
algo_trader.py can share downstream logic unchanged.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from trading.algo_math import (
    call_deltas,
    chain_expiry_hours,
    compute_fair,
    calendar_weights,
    interp_delta,
    rescale_delta,
    parse_band,
    next_deribit_expiry_utc,
    MIN_EDGE,
    MIN_FAIR_PROB,
)

logger = logging.getLogger(__name__)

DVOL_MAX = 120.0   # mirror csv_signals constant

# ── In-memory signal store ────────────────────────────────────────────────────
_latest_signals: list[dict[str, Any]] = []
_lock = threading.Lock()


def get_latest_signals() -> list[dict[str, Any]]:
    """Return the most recently computed signal list (thread-safe)."""
    with _lock:
        return list(_latest_signals)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_less_than_question(question: str) -> bool:
    import re
    ql = (question or "").lower()
    return bool(re.search(r"\b(less\s+than|lower\s+than|below|under)\b", ql))


def _extract_single_strike(question: str) -> Optional[float]:
    """Return the single dollar amount from a question like 'less than $64,000'."""
    import re
    q = (question or "").replace(",", "")
    m = re.search(r"\$([\d]+)\b", q)
    return float(m.group(1)) if m else None


def _compute_less_than_fair(
    chain_today: dict[float, float],
    chain_tomorrow: dict[float, float],
    K: float,
    w1: float,
    w2: float,
    h_src1: Optional[float],
    h_src2: Optional[float],
    h_tgt: float,
) -> Optional[float]:
    """
    P(spot < K) = 1 - P(call at K is ITM) = 1 - rescaled_delta(K).
    Calendar-weighted across two chains.
    """
    d1 = interp_delta(chain_today, K) if chain_today else None
    d2 = interp_delta(chain_tomorrow, K) if chain_tomorrow else None

    if h_tgt > 0:
        if d1 is not None and h_src1 and h_src1 > 0:
            d1 = rescale_delta(d1, h_src1, h_tgt)
        if d2 is not None and h_src2 and h_src2 > 0:
            d2 = rescale_delta(d2, h_src2, h_tgt)

    if d1 is not None and d2 is not None:
        prob_above = w1 * d1 + w2 * d2
    elif d1 is not None:
        prob_above = d1
    elif d2 is not None:
        prob_above = d2
    else:
        return None

    return max(0.0, min(1.0, 1.0 - prob_above))


# ── Deribit data loading from Redis ──────────────────────────────────────────

async def _load_deribit_from_redis(redis_client: Any, currency: str) -> tuple[list[dict], list[dict]]:
    """Return (today_rows, tomorrow_rows) from Redis ticker keys."""
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)
    today_rows: list[dict] = []
    tomorrow_rows: list[dict] = []

    keys = await redis_client.keys(f"deribit:ticker:{currency}-*")
    for key in keys:
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        expiry_str = (row.get("expiry_str") or "").upper()
        try:
            exp_date = datetime.strptime(expiry_str, "%d%b%y").date()
        except ValueError:
            continue
        if exp_date == today:
            today_rows.append(row)
        elif exp_date == tomorrow:
            tomorrow_rows.append(row)

    return today_rows, tomorrow_rows


# ── Polymarket Gamma API ──────────────────────────────────────────────────────

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CURRENCY_SLUGS = {"BTC": "bitcoin", "ETH": "ethereum"}


def _build_daily_slug(currency: str, target_date) -> str:
    asset = _CURRENCY_SLUGS.get(currency, currency.lower())
    month = target_date.strftime("%B").lower()
    return f"{asset}-price-on-{month}-{target_date.day}-{target_date.year}"


async def _fetch_polymarket_markets(currency: str) -> list[dict]:
    """Fetch today + tomorrow Polymarket markets for currency from Gamma API."""
    now = datetime.now(timezone.utc)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    rows: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=20) as client:
        for scan_date in (today, tomorrow):
            slug = _build_daily_slug(currency, scan_date)
            try:
                resp = await client.get(f"{_GAMMA_BASE}/events/slug/{slug}")
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                markets = resp.json().get("markets") or []
            except Exception as exc:
                logger.warning("[algo_signals] Gamma fetch failed (slug=%s): %s", slug, exc)
                continue

            for m in markets:
                mid = str(m.get("id") or m.get("conditionId") or "")
                if not mid or mid in seen:
                    continue
                question = m.get("question") or ""

                # Parse end date
                end_raw = m.get("endDate") or m.get("endDateIso") or ""
                try:
                    end_dt = datetime.strptime(str(end_raw)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

                # Resolution at 16:00 UTC on end date
                pm_resolution = end_dt.replace(hour=16, minute=0, second=0, microsecond=0)
                if pm_resolution < now:
                    continue

                # YES price
                prices_raw = m.get("outcomePrices") or []
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except Exception:
                        prices_raw = []
                if not prices_raw:
                    continue
                try:
                    pm_yes_price = float(prices_raw[0])
                except (TypeError, ValueError, IndexError):
                    continue

                seen.add(mid)
                rows.append({
                    "market_id": mid,
                    "question": question,
                    "currency": currency,
                    "pm_yes_price": pm_yes_price,
                    "pm_resolution": pm_resolution,
                })

    logger.info("[algo_signals] Polymarket %s: %d markets", currency, len(rows))
    return rows


# ── Core computation ──────────────────────────────────────────────────────────

def _compute_for_currency(
    *,
    currency: str,
    poly_markets: list[dict],
    today_rows: list[dict],
    tomorrow_rows: list[dict],
    dvol: Optional[float],
    funding_8h: Optional[float],
    now: datetime,
) -> list[dict[str, Any]]:
    if not poly_markets:
        return []

    if dvol is not None and dvol > DVOL_MAX:
        logger.info("[algo_signals] %s DVOL=%.1f > %.1f — skipping (IV unstable)", currency, dvol, DVOL_MAX)
        return []

    chain_today    = call_deltas(today_rows)
    chain_tomorrow = call_deltas(tomorrow_rows)

    if len(chain_today) < 3 and len(chain_tomorrow) < 3:
        logger.warning(
            "[algo_signals] %s chains too thin (today=%d strikes, tomorrow=%d) — skipping",
            currency, len(chain_today), len(chain_tomorrow),
        )
        return []

    h_src1 = chain_expiry_hours(today_rows, now)
    h_src2 = chain_expiry_hours(tomorrow_rows, now)

    exp1 = next_deribit_expiry_utc(now)
    exp2 = exp1 + timedelta(hours=24)
    t1_h = (exp1 - now).total_seconds() / 3600.0
    t2_h = (exp2 - now).total_seconds() / 3600.0

    signals: list[dict[str, Any]] = []

    for m in poly_markets:
        question     = m["question"]
        pm_yes_price = m["pm_yes_price"]
        pm_resolution = m["pm_resolution"]

        h_tgt = (pm_resolution - now).total_seconds() / 3600.0
        if h_tgt <= 0:
            continue

        w1, w2 = calendar_weights(h_tgt, t1_h, t2_h)

        is_lt = _is_less_than_question(question)
        k_lo, k_hi = parse_band(question)

        if is_lt:
            K = _extract_single_strike(question)
            if K is None:
                continue
            fair = _compute_less_than_fair(
                chain_today, chain_tomorrow, K, w1, w2, h_src1, h_src2, h_tgt
            )
        elif k_lo is not None and k_hi is not None:
            fair = compute_fair(
                chain_today, chain_tomorrow,
                float(k_lo), float(k_hi),
                w1, w2,
                h_src1, h_src2,
                h_tgt,
            )
        else:
            continue

        if fair is None:
            continue

        edge_yes = fair - pm_yes_price
        edge_no  = (1.0 - fair) - (1.0 - pm_yes_price)
        abs_edge = max(abs(edge_yes), abs(edge_no))
        has_alpha = (
            (edge_yes >= MIN_EDGE and fair >= MIN_FAIR_PROB) or
            (edge_no  >= MIN_EDGE and (1.0 - fair) >= MIN_FAIR_PROB)
        )

        conf = "high" if (len(chain_today) >= 5 and len(chain_tomorrow) >= 5) else "reduced"

        signals.append({
            "polymarket_market_id":  str(m["market_id"]),
            "polymarket_question":   question,
            "currency":              currency,
            "polymarket_price":      round(pm_yes_price, 4),
            "deribit_prob":          round(fair, 4),
            "edge_yes":              round(edge_yes, 4),
            "edge_no":               round(edge_no, 4),
            "abs_edge_pct":          round(abs_edge * 100, 2),
            "has_alpha":             has_alpha,
            "market_resolution_at":  pm_resolution.isoformat(),
            "dvol":                  round(dvol, 2) if dvol is not None else None,
            "funding_8h":            round(funding_8h, 6) if funding_8h is not None else None,
            "interp_confidence":     conf,
            "chain_today_strikes":   len(chain_today),
            "chain_tomorrow_strikes": len(chain_tomorrow),
            "w1": round(w1, 4),
            "w2": round(w2, 4),
            "h_tgt": round(h_tgt, 3),
        })

    signals.sort(key=lambda s: float(s.get("abs_edge_pct") or 0), reverse=True)
    return signals


# ── Async refresh ─────────────────────────────────────────────────────────────

async def refresh_algo_signals() -> None:
    """
    Recompute signals using the delta-interpolation algorithm and update the
    in-memory cache. Called periodically by algo_refresh_loop().
    """
    global _latest_signals
    import redis.asyncio as aioredis
    from django.conf import settings

    now = datetime.now(timezone.utc)
    redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)

    try:
        all_signals: list[dict] = []

        for currency in ("BTC", "ETH"):
            poly_markets = await _fetch_polymarket_markets(currency)
            today_rows, tomorrow_rows = await _load_deribit_from_redis(r, currency)

            # DVOL
            dvol: Optional[float] = None
            dvol_raw = await r.get(f"deribit:dvol:{currency.lower()}_usd")
            if dvol_raw:
                try:
                    dvol = float(dvol_raw)
                except (TypeError, ValueError):
                    pass

            # Perp funding
            funding_8h: Optional[float] = None
            perp_raw = await r.get(f"deribit:perp:{currency}")
            if perp_raw:
                try:
                    funding_8h = float(json.loads(perp_raw).get("funding_8h") or 0)
                except Exception:
                    pass

            sigs = _compute_for_currency(
                currency=currency,
                poly_markets=poly_markets,
                today_rows=today_rows,
                tomorrow_rows=tomorrow_rows,
                dvol=dvol,
                funding_8h=funding_8h,
                now=now,
            )
            all_signals.extend(sigs)
            logger.info("[algo_signals] %s: %d signals (%d alpha)", currency, len(sigs),
                        sum(1 for s in sigs if s.get("has_alpha")))

        with _lock:
            _latest_signals = all_signals

    finally:
        await r.aclose()


async def algo_refresh_loop(*, interval_seconds: int = 60) -> None:
    """Background loop — refresh algo signals every interval_seconds."""
    import asyncio
    logger.info("[algo_refresh] Starting — refresh every %ds", interval_seconds)
    while True:
        try:
            await refresh_algo_signals()
        except Exception as exc:
            logger.warning("[algo_refresh] error: %s", exc)
        await asyncio.sleep(interval_seconds)
