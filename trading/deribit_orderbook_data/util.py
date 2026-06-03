from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def utc_date(days_from_now: int = 0) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def deribit_expiry_str_from_date(d: datetime) -> str:
    """
    Deribit option instrument_name expiry part looks like: 28MAR26.
    Scanner uses %d%b%y to parse.
    """
    # Deribit instrument names do NOT always pad the day with a leading zero
    # (e.g. `BTC-2APR26-58000-C` instead of `BTC-02APR26-...`).
    month = d.strftime("%b").upper()
    year = d.strftime("%y")
    return f"{d.day}{month}{year}"


def parse_instrument_name(instrument_name: str) -> dict[str, Any]:
    # Example: BTC-2APR26-58000-C  or  BTC-2APR26-58000-P
    parts = instrument_name.split("-")
    out: dict[str, Any] = {"currency": None, "expiry_str": None, "strike": None, "option_type": None}
    if len(parts) != 4:
        return out
    out["currency"] = parts[0]
    out["expiry_str"] = parts[1]
    # Strike is the numeric segment: 58000
    try:
        out["strike"] = float(parts[2])
    except ValueError:
        out["strike"] = None
    out["option_type"] = parts[3]
    return out


def next_available_expiries(
    instruments: list[dict[str, Any]],
    currency: str,
    *,
    count: int = 2,
) -> list[str]:
    """Return the `count` soonest expiry strings that have active instruments
    for `currency`, sorted by calendar date.  When today has already expired
    Deribit removes those instruments, so the first entry becomes the next real
    expiry rather than today's wall-clock date.
    Falls back to wall-clock today/tomorrow strings when the list is empty.
    """
    seen: set[str] = set()
    dated: list[tuple[datetime, str]] = []
    for inst in instruments:
        name = inst.get("instrument_name") or ""
        meta = parse_instrument_name(name)
        if meta.get("currency") != currency:
            continue
        expiry_str = meta.get("expiry_str") or ""
        if not expiry_str or expiry_str in seen:
            continue
        seen.add(expiry_str)
        try:
            dt = datetime.strptime(expiry_str.upper(), "%d%b%y").replace(tzinfo=timezone.utc)
            dated.append((dt, expiry_str))
        except ValueError:
            continue
    dated.sort(key=lambda x: x[0])
    result = [expiry_str for _, expiry_str in dated[:count]]
    # Pad with wall-clock fallbacks if fewer than `count` expiries were found
    for i in range(len(result), count):
        result.append(deribit_expiry_str_from_date(utc_date(i)))
    return result


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # DictWriter will stringify numbers automatically, but leave None blank.
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})


def build_arg_parser(title: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=title)
    p.add_argument("--depth", type=int, default=1, help="Deribit order book depth")
    # 0 = no cap (fetch all matching instruments for today/tomorrow)
    p.add_argument(
        "--max-instruments-per-day",
        type=int,
        default=0,
        help="Cap instruments to control API load (0 = unlimited)",
    )
    p.add_argument(
        "--only-today",
        action="store_true",
        help="Export only today's expiry (UTC).",
    )
    p.add_argument(
        "--output-dir",
        type=str,
        default=str(Path(__file__).resolve().parent / "output"),
        help="Where CSV/JSON files are written",
    )
    p.add_argument("--include-json", action="store_true", help="Also write raw order book JSON per instrument")
    return p

