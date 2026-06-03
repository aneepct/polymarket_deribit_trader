from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import httpx

from polymarket_calendar import utc_today_tomorrow_dates

GAMMA_BASE = "https://gamma-api.polymarket.com"

CURRENCY_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
}

PUT_KEYWORDS = ["dip", "fall", "drop", "below", "under", "crash", "decline", "sink"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def extract_end_date(market: dict[str, Any]) -> Optional[datetime]:
    end = market.get("endDate") or market.get("endDateIso")
    if not end:
        return None
    try:
        return datetime.strptime(str(end)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def poly_resolution_time(end_date: datetime) -> datetime:
    """Match scanner behavior: treat endDate as settlement at 16:00 UTC."""
    return end_date.replace(
        hour=16,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=timezone.utc,
    )


def detect_currency(question: str) -> Optional[str]:
    q = (question or "").lower()
    for code, keywords in CURRENCY_KEYWORDS.items():
        if any(k in q for k in keywords):
            return code
    return None


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


def detect_option_type(question: str) -> str:
    ql = (question or "").lower()
    if any(keyword in ql for keyword in PUT_KEYWORDS):
        return "P"
    return "C"


def _parse_maybe_json_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            loaded = json.loads(v)
            if isinstance(loaded, list):
                return loaded
        except json.JSONDecodeError:
            return []
    return []


def extract_outcome_prices0_raw(market: dict[str, Any]) -> Optional[float]:
    prices = _parse_maybe_json_list(market.get("outcomePrices"))
    if not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


def extract_yes_price_from_market(market: dict[str, Any]) -> Optional[float]:
    """
    Mirror scanner behavior:
    - prefer outcomes/outcomePrices mapping to the 'yes' outcome
    - fallback to tokens[*] where token.outcome == 'yes'
    """
    outcomes = _parse_maybe_json_list(market.get("outcomes", "[]"))
    prices = _parse_maybe_json_list(market.get("outcomePrices", "[]"))

    if outcomes and prices and len(outcomes) == len(prices):
        for idx, outcome in enumerate(outcomes):
            if str(outcome).lower() == "yes":
                try:
                    return float(prices[idx])
                except (TypeError, ValueError):
                    return None

    for token in market.get("tokens", []) or []:
        if str((token or {}).get("outcome", "")).lower() == "yes":
            price = (token or {}).get("price")
            if price is not None:
                try:
                    return float(price)
                except (TypeError, ValueError):
                    return None
    return None


def extract_outcome_prices0_scaled(market: dict[str, Any]) -> Optional[float]:
    raw0 = extract_outcome_prices0_raw(market)
    return None if raw0 is None else raw0 * 100


def extract_liquidity(market: dict[str, Any]) -> float:
    for key in ["liquidity", "liquidityNum", "volume", "volumeNum"]:
        value = market.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return 0.0


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})


CURRENCY_SLUGS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
}

# ---------------------------------------------------------------------------
# Slug builders
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "snapshot_at",
    "market_id",
    "polymarket_question",
    "currency",
    "option_type",
    "target_price_from_question",
    "end_date_iso",
    "liquidity_usd",
    "outcomePrices_0_scaled",
    "outcomePrices_0_raw",
]


def build_daily_slug(currency: str, target_date) -> str:
    """
    Build the Polymarket event slug for a daily price event.
    Format: {asset}-price-on-{month}-{day}-{year}
    e.g. BTC + May 25 2026 -> 'bitcoin-price-on-may-25-2026'
    """
    asset = CURRENCY_SLUGS.get(currency, currency.lower())
    month = target_date.strftime("%B").lower()
    day = str(target_date.day)
    year = str(target_date.year)
    return f"{asset}-price-on-{month}-{day}-{year}"


async def fetch_event_markets(client: httpx.AsyncClient, slug: str) -> list[dict[str, Any]]:
    """Fetch markets from a Polymarket event slug."""
    url = f"{GAMMA_BASE}/events/slug/{slug}"
    resp = await client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json().get("markets") or []


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Polymarket daily price event markets to CSV using event slugs.",
    )
    parser.add_argument("--output-dir", type=str, default=str(Path(__file__).resolve().parent / "output"))
    args = parser.parse_args()

    now = utc_now()
    today = now.date()
    out_root = Path(args.output_dir)
    rows_by_currency: dict[str, list[dict[str, Any]]] = {"BTC": [], "ETH": []}
    combined_rows: list[dict[str, Any]] = []

    # Scan both today and tomorrow so we always return the freshest available data.
    # Markets for today are still live until they resolve at 16:00 UTC; after that
    # they are filtered below by poly_resolution_time(end_date) < now.
    tomorrow = (now + timedelta(days=1)).date()
    scan_dates = [today, tomorrow]

    async with httpx.AsyncClient(timeout=20) as client:
        for currency in ("BTC", "ETH"):
            seen_ids: set[str] = set()
            for scan_date in scan_dates:
                slug = build_daily_slug(currency, scan_date)
                markets = await fetch_event_markets(client, slug)
                print(f"[{currency}] slug={slug} → {len(markets)} markets")

                for market in markets:
                    mid = str(market.get("id") or market.get("conditionId") or "")
                    if mid in seen_ids:
                        continue
                    question = market.get("question") or ""
                    end_date = extract_end_date(market)
                    if not end_date:
                        continue
                    if poly_resolution_time(end_date) < now:
                        continue

                    outcome0_scaled = extract_outcome_prices0_scaled(market)
                    if outcome0_scaled is None:
                        continue

                    seen_ids.add(mid)
                    row = {
                        "snapshot_at": now.isoformat(),
                        "market_id": mid,
                        "polymarket_question": question,
                        "currency": currency,
                        "option_type": detect_option_type(question),
                        "target_price_from_question": extract_price_from_question(question),
                        "end_date_iso": end_date.isoformat(),
                        "liquidity_usd": extract_liquidity(market),
                        "outcomePrices_0_scaled": outcome0_scaled,
                        "outcomePrices_0_raw": extract_outcome_prices0_raw(market),
                    }
                    rows_by_currency[currency].append(row)
                    combined_rows.append(row)

    btc_rows = rows_by_currency["BTC"]
    eth_rows = rows_by_currency["ETH"]

    btc_path = out_root / "BTC" / "polymarket_markets_today_utc.csv"
    eth_path = out_root / "ETH" / "polymarket_markets_today_utc.csv"
    combined_path = out_root / "polymarket_markets_today_utc_both.csv"

    write_csv(btc_path, btc_rows, fieldnames=FIELDNAMES)
    write_csv(eth_path, eth_rows, fieldnames=FIELDNAMES)
    write_csv(combined_path, combined_rows, fieldnames=FIELDNAMES)

    print(f"Wrote BTC rows: {len(btc_rows)} -> {btc_path}")
    print(f"Wrote ETH rows: {len(eth_rows)} -> {eth_path}")
    print(f"Wrote combined rows: {len(combined_rows)} -> {combined_path}")


if __name__ == "__main__":
    asyncio.run(main())

