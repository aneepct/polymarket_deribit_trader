"""
Self-contained signal computation module.

Fetches live Polymarket markets directly from the Gamma API and reads
real-time Deribit IV from Redis (written by deribit_ws_loop).
Computes BSM-based probability estimates and exposes get_latest_signals()
for the trading loop.

No openclaw-specific dependencies. No CSV files written or read (except
Deribit CSV as a one-time fallback on first startup before Redis is warm).
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constants (mirror openclaw/backend/config.py) ─────────────────────────────
STRIKE_TOLERANCE_PCT = 5.0
DERIBIT_DEPTH = 1
MIN_EDGE_PCT = 3.0
MIN_OI_FILTER = 0           # set > 0 to require open interest on matched instrument
DVOL_MAX = 120.0            # skip signals if DVOL > this (IV too unstable for BSM)
FUNDING_STRONG_THRESHOLD = 0.0003  # 0.03%/hr — above this, crowd is strongly directional

# ── In-memory signal store ────────────────────────────────────────────────────
_latest_signals: list[dict[str, Any]] = []
_lock = threading.Lock()


# ── Helpers inlined from engine/scanner.py ───────────────────────────────────

def extract_price_from_question(question: str) -> Optional[float]:
    q = (question or "").replace(",", "")
    m = re.search(r"\$([\d.]+)\s*[kK]\b", q, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1000
    m = re.search(r"\$([\d]{4,})\b", q)
    if m:
        return float(m.group(1))
    m = re.search(r"\b([\d.]+)\s*[kK]\b", q, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1000
    m = re.search(r"\b([\d]{5,})\b", q)
    if m:
        return float(m.group(1))
    return None


def extract_price_range_from_question(question: str) -> tuple[Optional[float], Optional[float]]:
    q = (question or "").replace(",", "")

    def _parse_amount(digits: str, k_suffix: Optional[str]) -> float:
        v = float(digits)
        if k_suffix:
            v *= 1000
        return v

    m = re.search(
        r"between\s+\$?([\d.]+)\s*([kK])?\s+and\s+\$?([\d.]+)\s*([kK])?",
        q, re.IGNORECASE,
    )
    if m:
        low = _parse_amount(m.group(1), m.group(2))
        high = _parse_amount(m.group(3), m.group(4))
        return low, high

    return extract_price_from_question(question), None


def is_less_than_question(question: str) -> bool:
    ql = (question or "").lower()
    return bool(re.search(r"\b(less\s+than|lower\s+than|below|under)\b", ql))


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bsm_call_prob(spot: float, strike: float, sigma: float, t_years: float) -> float:
    if t_years <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    d1 = (math.log(spot / strike) + 0.5 * sigma ** 2 * t_years) / (sigma * math.sqrt(t_years))
    return _norm_cdf(d1)


# ── CSV helpers ───────────────────────────────────────────────────────────────

def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _utc_from_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _expiry_str_to_datetime(expiry_str: str) -> Optional[datetime]:
    try:
        dt = datetime.strptime(expiry_str.strip().upper(), "%d%b%y")
        return dt.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    except ValueError:
        return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _days_until(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 86400.0


def poly_resolution_time(end_date: datetime) -> datetime:
    return end_date.replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


def _load_deribit_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def _load_poly_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


async def _load_deribit_from_redis(
    redis_client: Any, currency: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (today_rows, tomorrow_rows) parsed from Redis ticker keys."""
    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)

    keys = await redis_client.keys(f"deribit:ticker:{currency}-*")
    today_rows: list[dict[str, Any]] = []
    tomorrow_rows: list[dict[str, Any]] = []

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


def _find_closest_in_strike(
    candidates: list[dict[str, Any]],
    *,
    strike: float,
    strike_tol_pct: float,
) -> Optional[dict[str, Any]]:
    tol = strike_tol_pct / 100.0
    best: Optional[dict[str, Any]] = None
    best_diff = float("inf")
    for c in candidates:
        c_strike = _to_float(c.get("strike"))
        if c_strike is None or strike == 0:
            continue
        strike_diff = abs(c_strike - strike) / strike
        if strike_diff > tol:
            continue
        if strike_diff < best_diff:
            best_diff = strike_diff
            best = c
    return best


def _book_from_der_row(r: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not r:
        return None
    return {
        "mark_iv": r.get("mark_iv"),
        "delta": r.get("delta"),
        "bid_price": r.get("best_bid_price"),
        "ask_price": r.get("best_ask_price"),
        "mark_price": r.get("mark_price"),
    }


# ── Polymarket Gamma API fetch ────────────────────────────────────────────────

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CURRENCY_SLUGS = {"BTC": "bitcoin", "ETH": "ethereum"}
_PUT_KEYWORDS = ["dip", "fall", "drop", "below", "under", "crash", "decline", "sink"]


def _parse_maybe_json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            loaded = json.loads(v)
            if isinstance(loaded, list):
                return loaded
        except json.JSONDecodeError:
            pass
    return []


def _extract_end_date(market: dict) -> Optional[datetime]:
    end = market.get("endDate") or market.get("endDateIso")
    if not end:
        return None
    try:
        return datetime.strptime(str(end)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_outcome_prices0_scaled(market: dict) -> Optional[float]:
    prices = _parse_maybe_json_list(market.get("outcomePrices"))
    if not prices:
        return None
    try:
        return float(prices[0]) * 100
    except (TypeError, ValueError, IndexError):
        return None


def _extract_liquidity(market: dict) -> float:
    for key in ("liquidity", "liquidityNum", "volume", "volumeNum"):
        v = market.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _detect_option_type(question: str) -> str:
    ql = (question or "").lower()
    return "P" if any(k in ql for k in _PUT_KEYWORDS) else "C"


def _build_daily_slug(currency: str, target_date) -> str:
    asset = _CURRENCY_SLUGS.get(currency, currency.lower())
    month = target_date.strftime("%B").lower()
    return f"{asset}-price-on-{month}-{target_date.day}-{target_date.year}"


async def _fetch_polymarket_rows(currency: str) -> list[dict[str, Any]]:
    """Fetch today+tomorrow Polymarket markets for *currency* from the Gamma API."""
    now = datetime.now(timezone.utc)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(timeout=20) as client:
        for scan_date in (today, tomorrow):
            slug = _build_daily_slug(currency, scan_date)
            url = f"{_GAMMA_BASE}/events/slug/{slug}"
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                markets = resp.json().get("markets") or []
            except Exception as exc:
                logger.warning("[csv_signals] Polymarket fetch failed (slug=%s): %s", slug, exc)
                continue

            for market in markets:
                mid = str(market.get("id") or market.get("conditionId") or "")
                if not mid or mid in seen_ids:
                    continue
                question = market.get("question") or ""
                end_date = _extract_end_date(market)
                if not end_date:
                    continue
                if poly_resolution_time(end_date) < now:
                    continue
                outcome0_scaled = _extract_outcome_prices0_scaled(market)
                if outcome0_scaled is None:
                    continue
                seen_ids.add(mid)
                rows.append({
                    "market_id": mid,
                    "polymarket_question": question,
                    "currency": currency,
                    "option_type": _detect_option_type(question),
                    "target_price_from_question": extract_price_from_question(question),
                    "end_date_iso": end_date.isoformat(),
                    "liquidity_usd": _extract_liquidity(market),
                    "outcomePrices_0_scaled": outcome0_scaled,
                })

    logger.info("[csv_signals] Polymarket %s: %d markets fetched", currency, len(rows))
    return rows


# ── Core computation ──────────────────────────────────────────────────────────

def _compute_for_currency(
    *,
    currency: str,
    poly_rows: list[dict[str, Any]],
    deribit_today_rows: list[dict[str, Any]],
    deribit_tomorrow_rows: list[dict[str, Any]],
    dvol: Optional[float] = None,
    funding_8h: Optional[float] = None,
) -> tuple[list[dict[str, Any]], int]:
    der_today = deribit_today_rows
    der_tomorrow = deribit_tomorrow_rows

    if not poly_rows or (not der_today and not der_tomorrow):
        return [], len(poly_rows)

    # ── DVOL filter: skip if IV is too unstable for BSM to be reliable ────────
    if dvol is not None and dvol > DVOL_MAX:
        logger.info(
            "[csv_signals] %s DVOL=%.1f > %.1f — skipping signal computation (IV unstable)",
            currency, dvol, DVOL_MAX,
        )
        return [], len(poly_rows)

    der_today_by_opt: dict[str, list[dict[str, Any]]] = {"C": [], "P": []}
    der_tom_by_opt: dict[str, list[dict[str, Any]]] = {"C": [], "P": []}

    for r in der_today:
        opt = (r.get("option_type") or "").strip()
        if opt in ("C", "P"):
            der_today_by_opt[opt].append(r)
    for r in der_tomorrow:
        opt = (r.get("option_type") or "").strip()
        if opt in ("C", "P"):
            der_tom_by_opt[opt].append(r)

    candidates: list[dict[str, Any]] = []

    for m in poly_rows:
        poly_prob = _to_float(m.get("outcomePrices_0_scaled"))
        if poly_prob is None:
            continue
        polymarket_price = poly_prob / 100.0

        try:
            strike = float(m.get("target_price_from_question") or "")
        except ValueError:
            continue
        if not math.isfinite(strike) or strike <= 0:
            continue

        option_type = (m.get("option_type") or "").strip()
        if option_type not in ("C", "P"):
            continue

        polymarket_question = m.get("polymarket_question") or ""
        _strike_low, strike_high_parsed = extract_price_range_from_question(polymarket_question)
        is_range = strike_high_parsed is not None
        is_lt = is_less_than_question(polymarket_question)
        lookup_option_type = "C" if (is_range or is_lt) else option_type

        end_dt = _utc_from_iso(m.get("end_date_iso"))
        if not end_dt:
            continue
        t_star = poly_resolution_time(end_dt)

        der1 = _find_closest_in_strike(
            der_today_by_opt[lookup_option_type],
            strike=strike,
            strike_tol_pct=STRIKE_TOLERANCE_PCT,
        )
        der2 = _find_closest_in_strike(
            der_tom_by_opt[lookup_option_type],
            strike=strike,
            strike_tol_pct=STRIKE_TOLERANCE_PCT,
        )

        der1_high = der2_high = None
        if is_range and strike_high_parsed is not None:
            der1_high = _find_closest_in_strike(
                der_today_by_opt["C"],
                strike=strike_high_parsed,
                strike_tol_pct=STRIKE_TOLERANCE_PCT,
            )
            der2_high = _find_closest_in_strike(
                der_tom_by_opt["C"],
                strike=strike_high_parsed,
                strike_tol_pct=STRIKE_TOLERANCE_PCT,
            )

        if not der1 and not der2:
            continue

        # ── OI filter: skip if matched instrument has zero open interest ──────
        if MIN_OI_FILTER > 0:
            oi1 = _to_float((der1 or {}).get("open_interest")) if der1 else None
            oi2 = _to_float((der2 or {}).get("open_interest")) if der2 else None
            best_oi = max(v for v in (oi1, oi2) if v is not None) if (oi1 is not None or oi2 is not None) else None
            if best_oi is not None and best_oi < MIN_OI_FILTER:
                continue  # model quote only — no real IV traded here

        spot_price = _to_float((der1 or der2 or {}).get("index_price"))
        spot = spot_price or 0.0

        def _bsm_prob_from_row(
            row1: Optional[dict[str, Any]],
            row2: Optional[dict[str, Any]],
            k: float,
        ) -> Optional[float]:
            now = datetime.now(timezone.utc)
            t_poly_years = max((t_star - now).total_seconds(), 60) / 31_536_000

            def _expiry_years(row: dict[str, Any]) -> Optional[float]:
                expiry_str = (row.get("expiry_str") or "").strip()
                expiry_dt = _expiry_str_to_datetime(expiry_str) if expiry_str else None
                if expiry_dt is None:
                    return None
                return max((expiry_dt - now).total_seconds(), 60) / 31_536_000

            def _sigma(row: dict[str, Any]) -> Optional[float]:
                iv = _to_float(row.get("mark_iv"))
                return iv / 100.0 if iv else None

            s1 = _sigma(row1) if row1 else None
            s2 = _sigma(row2) if row2 else None

            if s1 is not None and s2 is not None:
                t1y = _expiry_years(row1)
                t2y = _expiry_years(row2)
                if t1y is not None and t2y is not None and (t2y - t1y) > 1e-9:
                    w = max(0.0, min(1.0, (t_poly_years - t1y) / (t2y - t1y)))
                    total_var = (1 - w) * s1 ** 2 * t1y + w * s2 ** 2 * t2y
                else:
                    total_var = s1 ** 2 * t_poly_years
                eff_sigma = math.sqrt(max(total_var, 0.0) / t_poly_years)
            elif s1 is not None:
                eff_sigma = s1
            elif s2 is not None:
                eff_sigma = s2
            else:
                return None

            if spot <= 0 or k <= 0:
                return None
            return round(bsm_call_prob(spot, k, eff_sigma, t_poly_years), 4)

        prob_low = _bsm_prob_from_row(der1, der2, strike)
        if prob_low is None:
            continue

        if is_range and strike_high_parsed is not None:
            prob_high = _bsm_prob_from_row(der1_high, der2_high, strike_high_parsed)
            if prob_high is None:
                prob_high = 1.0 if (spot > 0 and strike_high_parsed < spot) else 0.0

            # IV skew: for the lower strike of a range, also try put IV which
            # captures put-skew (OTM puts are more expensive, reflecting downside fear).
            # Use put-based prob_low if available and the lower strike is below spot.
            if spot > 0 and strike < spot:
                der1_put = _find_closest_in_strike(der_today_by_opt["P"], strike=strike, strike_tol_pct=STRIKE_TOLERANCE_PCT)
                der2_put = _find_closest_in_strike(der_tom_by_opt["P"], strike=strike, strike_tol_pct=STRIKE_TOLERANCE_PCT)
                if der1_put or der2_put:
                    prob_low_put = _bsm_prob_from_row(der1_put, der2_put, strike)
                    if prob_low_put is not None:
                        # Average call and put BSM probs (put-call parity weighted blend)
                        prob_low = round((float(prob_low) + float(prob_low_put)) / 2.0, 4)

            deribit_prob = _clamp01(float(prob_low) - float(prob_high))
        elif is_lt:
            deribit_prob = _clamp01(1.0 - float(prob_low))
        else:
            deribit_prob = float(prob_low)

        edge_pct = round((deribit_prob - polymarket_price) * 100.0, 2)
        abs_edge_pct = round(abs(edge_pct), 2)
        has_alpha = abs_edge_pct >= float(MIN_EDGE_PCT)

        liquidity_usd = _to_float(m.get("liquidity_usd")) or 0.0

        t1 = _expiry_str_to_datetime(der1.get("expiry_str") or "") if der1 else None
        t2 = _expiry_str_to_datetime(der2.get("expiry_str") or "") if der2 else None
        interp_w = None
        if t1 and t2:
            span = (t2 - t1).total_seconds()
            if span > 0:
                interp_w = round(max(0.0, min(1.0, (t_star - t1).total_seconds() / span)), 4)

        sigma_t1 = None
        sigma_t2 = None
        if der1:
            mv1 = _to_float(der1.get("mark_iv"))
            if mv1 is not None:
                sigma_t1 = round(mv1 / 100.0, 4)
        if der2:
            mv2 = _to_float(der2.get("mark_iv"))
            if mv2 is not None:
                sigma_t2 = round(mv2 / 100.0, 4)

        primary = der1 or der2
        candidates.append(
            {
                "instrument_t1": der1.get("instrument_name") if der1 else "N/A",
                "instrument_t2": der2.get("instrument_name") if der2 else "N/A",
                "instrument_t1_expiry": t1.isoformat() if t1 else None,
                "instrument_t2_expiry": t2.isoformat() if t2 else None,
                "option_type": option_type,
                "interp_method": "interpolated" if (der1 and der2) else ("T2-only" if der2 else "T1-only"),
                "interp_weight_w": interp_w,
                "interp_confidence": "high" if (der1 and der2) else "reduced",
                "polymarket_market_id": str(m.get("market_id") or ""),
                "polymarket_question": polymarket_question,
                "market_resolution_at": t_star.isoformat(),
                "spot_price": spot_price,
                "strike": strike,
                "t_poly_days": round(_days_until(t_star), 2),
                "T1_days": round(_days_until(t1), 2) if t1 else None,
                "T2_days": round(_days_until(t2), 2) if t2 else None,
                "sigma_t1": sigma_t1,
                "sigma_t2": sigma_t2,
                "delta": _to_float(primary.get("delta")) if primary else None,
                "gamma": _to_float(primary.get("gamma")) if primary else None,
                "vega": _to_float(primary.get("vega")) if primary else None,
                "theta": _to_float(primary.get("theta")) if primary else None,
                "polymarket_price": round(polymarket_price, 4),
                "deribit_prob": round(deribit_prob, 4),
                "edge_pct": edge_pct,
                "abs_edge_pct": abs_edge_pct,
                "has_alpha": has_alpha,
                "liquidity_usd": round(liquidity_usd, 2),
                "scanned_at": datetime.utcnow().isoformat(),
                "data_source": "csv_deribit_poly",
                "currency": currency,
                # ── Extra context fields (for logging/analysis) ────────────
                "dvol": round(dvol, 2) if dvol is not None else None,
                "funding_8h": round(funding_8h, 6) if funding_8h is not None else None,
                "open_interest_t1": _to_float((der1 or {}).get("open_interest")) if der1 else None,
                "open_interest_t2": _to_float((der2 or {}).get("open_interest")) if der2 else None,
            }
        )

    candidates.sort(key=lambda s: float(s.get("abs_edge_pct") or 0.0), reverse=True)
    return candidates, len(poly_rows)


async def refresh_latest_signals() -> None:
    """
    Recompute signals and update the in-memory store.

    Polymarket data: fetched live from the Gamma API on every call.
    Deribit data: read from Redis (written in real-time by deribit_ws_loop).
    Falls back to Deribit CSV files on first startup before Redis is warm.
    No CSV files are written or required for normal operation.
    """
    global _latest_signals
    import redis.asyncio as aioredis

    # ── Polymarket rows from Gamma API ────────────────────────────────────────
    btc_poly_rows = await _fetch_polymarket_rows("BTC")
    eth_poly_rows = await _fetch_polymarket_rows("ETH")

    # ── Deribit rows from Redis ────────────────────────────────────────────────
    from django.conf import settings
    redis_url = getattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        btc_today, btc_tomorrow = await _load_deribit_from_redis(r, "BTC")
        eth_today, eth_tomorrow = await _load_deribit_from_redis(r, "ETH")

        # ── DVOL + funding from Redis (written by deribit_ws_loop) ────────────
        async def _read_float(key: str) -> Optional[float]:
            v = await r.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        async def _read_funding(key: str) -> Optional[float]:
            v = await r.get(key)
            if not v:
                return None
            try:
                return float(json.loads(v).get("funding_8h") or 0)
            except Exception:
                return None

        btc_dvol     = await _read_float("deribit:dvol:btc_usd")
        eth_dvol     = await _read_float("deribit:dvol:eth_usd")
        btc_funding  = await _read_funding("deribit:perp:BTC")
        eth_funding  = await _read_funding("deribit:perp:ETH")

        if btc_dvol is not None:
            logger.info("[csv_signals] BTC DVOL=%.1f  funding_8h=%.5f", btc_dvol, btc_funding or 0)
        if eth_dvol is not None:
            logger.info("[csv_signals] ETH DVOL=%.1f  funding_8h=%.5f", eth_dvol, eth_funding or 0)
    finally:
        await r.aclose()

    # ── Deribit CSV fallback (first startup before WS has warmed Redis) ───────
    base = Path(__file__).resolve().parent
    depth = DERIBIT_DEPTH
    if not btc_today and not btc_tomorrow:
        logger.info("[csv_signals] No BTC data in Redis — falling back to CSV")
        btc_today = _load_deribit_csv(
            base / "deribit_orderbook_data" / "output" / "BTC" / f"order_book_today_depth{depth}.csv"
        )
        btc_tomorrow = _load_deribit_csv(
            base / "deribit_orderbook_data" / "output" / "BTC" / f"order_book_tomorrow_depth{depth}.csv"
        )

    if not eth_today and not eth_tomorrow:
        logger.info("[csv_signals] No ETH data in Redis — falling back to CSV")
        eth_today = _load_deribit_csv(
            base / "deribit_orderbook_data" / "output" / "ETH" / f"order_book_today_depth{depth}.csv"
        )
        eth_tomorrow = _load_deribit_csv(
            base / "deribit_orderbook_data" / "output" / "ETH" / f"order_book_tomorrow_depth{depth}.csv"
        )

    btc_candidates, _ = _compute_for_currency(
        currency="BTC",
        poly_rows=btc_poly_rows,
        deribit_today_rows=btc_today,
        deribit_tomorrow_rows=btc_tomorrow,
        dvol=btc_dvol,
        funding_8h=btc_funding,
    )
    eth_candidates, _ = _compute_for_currency(
        currency="ETH",
        poly_rows=eth_poly_rows,
        deribit_today_rows=eth_today,
        deribit_tomorrow_rows=eth_tomorrow,
        dvol=eth_dvol,
        funding_8h=eth_funding,
    )

    candidates = btc_candidates + eth_candidates
    with _lock:
        _latest_signals = candidates


def get_latest_signals() -> list[dict[str, Any]]:
    with _lock:
        return list(_latest_signals)
