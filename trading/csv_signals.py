"""
Self-contained signal computation module.

Reads CSV files produced by the deribit_orderbook_data and
polymarket_markets_export scripts, computes BSM-based probability
estimates, and exposes get_latest_signals() for the trading loop.

No openclaw-specific dependencies (config, engine.scanner, agents, memory_store).
"""
from __future__ import annotations

import csv
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Constants (mirror openclaw/backend/config.py) ─────────────────────────────
STRIKE_TOLERANCE_PCT = 5.0
DERIBIT_DEPTH = 1
MIN_EDGE_PCT = 3.0

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


# ── Core computation ──────────────────────────────────────────────────────────

def _compute_for_currency(
    *,
    currency: str,
    poly_csv_path: Path,
    deribit_today_csv_path: Path,
    deribit_tomorrow_csv_path: Path,
) -> tuple[list[dict[str, Any]], int]:
    poly_rows: list[dict[str, Any]] = []
    if poly_csv_path.exists():
        with poly_csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            poly_rows = list(reader)

    der_today = _load_deribit_csv(deribit_today_csv_path)
    der_tomorrow = _load_deribit_csv(deribit_tomorrow_csv_path)

    if not poly_rows or (not der_today and not der_tomorrow):
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
            }
        )

    candidates.sort(key=lambda s: float(s.get("abs_edge_pct") or 0.0), reverse=True)
    return candidates, len(poly_rows)


async def refresh_latest_signals() -> None:
    """Recompute signals from the latest CSV files and update the in-memory store."""
    global _latest_signals

    # CSV files live alongside this file inside trading/
    base = Path(__file__).resolve().parent
    depth = DERIBIT_DEPTH

    btc_poly = base / "polymarket_markets_export" / "output" / "BTC" / "polymarket_markets_today_utc.csv"
    eth_poly = base / "polymarket_markets_export" / "output" / "ETH" / "polymarket_markets_today_utc.csv"

    btc_t1 = base / "deribit_orderbook_data" / "output" / "BTC" / f"order_book_today_depth{depth}.csv"
    btc_t2 = base / "deribit_orderbook_data" / "output" / "BTC" / f"order_book_tomorrow_depth{depth}.csv"
    eth_t1 = base / "deribit_orderbook_data" / "output" / "ETH" / f"order_book_today_depth{depth}.csv"
    eth_t2 = base / "deribit_orderbook_data" / "output" / "ETH" / f"order_book_tomorrow_depth{depth}.csv"

    btc_candidates, _ = _compute_for_currency(
        currency="BTC",
        poly_csv_path=btc_poly,
        deribit_today_csv_path=btc_t1,
        deribit_tomorrow_csv_path=btc_t2,
    )
    eth_candidates, _ = _compute_for_currency(
        currency="ETH",
        poly_csv_path=eth_poly,
        deribit_today_csv_path=eth_t1,
        deribit_tomorrow_csv_path=eth_t2,
    )

    candidates = btc_candidates + eth_candidates
    with _lock:
        _latest_signals = candidates


def get_latest_signals() -> list[dict[str, Any]]:
    with _lock:
        return list(_latest_signals)
