"""
CSV refresh loop for polymarket_deribit_trader.

Runs the Deribit and Polymarket data export scripts as subprocesses,
then calls refresh_latest_signals() to recompute the in-memory signal store.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

# trading/ package root — all scripts live relative to this
TRADING_ROOT = Path(__file__).resolve().parent


async def _run_cmd(cmd: list[str], *, cwd: Path) -> int:
    def _sync() -> int:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr)
        return proc.returncode

    return await asyncio.to_thread(_sync)


async def export_all_csvs(*, deribit_depth: int = 1) -> None:
    py = sys.executable
    btc_script = TRADING_ROOT / "deribit_orderbook_data" / "btc.py"
    eth_script = TRADING_ROOT / "deribit_orderbook_data" / "eth.py"
    poly_script = TRADING_ROOT / "polymarket_markets_export" / "export_markets.py"

    rc = await _run_cmd(
        [py, str(btc_script), "--depth", str(deribit_depth), "--max-instruments-per-day", "40"],
        cwd=TRADING_ROOT,
    )
    if rc != 0:
        print(f"[csv_refresh] BTC deribit export failed (rc={rc})")
        return

    rc = await _run_cmd(
        [py, str(eth_script), "--depth", str(deribit_depth), "--max-instruments-per-day", "40"],
        cwd=TRADING_ROOT,
    )
    if rc != 0:
        print(f"[csv_refresh] ETH deribit export failed (rc={rc})")
        return

    rc = await _run_cmd([py, str(poly_script)], cwd=TRADING_ROOT)
    if rc != 0:
        print(f"[csv_refresh] Polymarket markets export failed (rc={rc})")
        return

    from trading.csv_signals import refresh_latest_signals
    await refresh_latest_signals()
    print("[csv_refresh] Signals refreshed.")


async def csv_refresh_loop(*, interval_seconds: int = 3600, deribit_depth: int = 1) -> None:
    print(f"[csv_refresh] Starting — interval={interval_seconds}s depth={deribit_depth}")
    in_progress = False
    while True:
        if not in_progress:
            in_progress = True
            try:
                await export_all_csvs(deribit_depth=deribit_depth)
            except Exception as exc:
                print(f"[csv_refresh] Error during export: {exc}")
            finally:
                in_progress = False
        await asyncio.sleep(interval_seconds)
