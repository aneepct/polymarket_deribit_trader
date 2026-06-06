"""
algo_trader.py — Autonomous trading loop driven by the delta-interpolation
algorithm from algorithms_2026-05-28.py.

This is a parallel service to auto_trader.py. It uses:
  - algo_signals.get_latest_signals()  instead of csv_signals
  - algorithms_2026-05-28.evaluate_exit() for exit decisions (all rules active)
  - AlgoAssetState (Redis key prefix 'algo:state:') — no conflict with main trader

State machine (per asset):
  SCANNING   → find best alpha signal, place BUY.
  MONITORING → check P&L each cycle.
               • BUY not filled     → cancel, re-scan.
               • SELL filled        → reset, re-scan.
               • evaluate_exit()    → place SELL (signal-based exit).
               • profit_target_pct  → place SELL.
               • stop_loss_pct      → place SELL.
               • market expired     → place SELL.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from trading.algo_state import AlgoAssetState

logger = logging.getLogger(__name__)

# ── Load evaluate_exit from algorithms_2026-05-28.py ─────────────────────────
_ALGO_PATH = pathlib.Path(__file__).parent.parent / "algorithms_2026-05-28.py"
_spec = importlib.util.spec_from_file_location("_algo_math_trader", _ALGO_PATH)
_algo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_algo)
evaluate_exit = _algo.evaluate_exit


# ── Config helper ─────────────────────────────────────────────────────────────

def _cfg():
    from trading.models import TradingConfig
    return TradingConfig.load()


async def _acfg():
    return await asyncio.to_thread(_cfg)


# ── Gamma API helpers (shared with auto_trader.py) ───────────────────────────

def _resolve_token_ids(market_id: str) -> list[str]:
    cfg = _cfg()
    timeout = cfg.http_timeout_s

    def _extract_ids(m: dict) -> Optional[list[str]]:
        ids = m.get("clobTokenIds") or []
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:
                return None
        if isinstance(ids, list) and len(ids) >= 2:
            return [str(ids[0]), str(ids[1])]
        return None

    gamma = cfg.gamma_api
    try:
        r = httpx.get(f"{gamma}/markets/{market_id}", timeout=timeout)
        if r.status_code == 200:
            result = _extract_ids(r.json() if isinstance(r.json(), dict) else (r.json()[0] if r.json() else {}))
            if result:
                return result
    except Exception:
        pass
    try:
        r2 = httpx.get(f"{gamma}/markets", params={"condition_id": market_id}, timeout=timeout)
        if r2.status_code == 200:
            data2 = r2.json()
            markets = data2 if isinstance(data2, list) else data2.get("markets", [])
            if markets:
                result = _extract_ids(markets[0])
                if result:
                    return result
    except Exception:
        pass
    raise ValueError(f"Could not resolve token IDs for market_id={market_id!r}")


def _market_info_for_token(token_id: str) -> dict:
    cfg = _cfg()
    try:
        r = httpx.get(
            f"{cfg.gamma_api}/markets",
            params={"clob_token_ids": token_id},
            timeout=cfg.http_timeout_s,
        )
        if r.status_code == 200:
            data = r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            if markets:
                m = markets[0]
                return {"question": m.get("question") or "", "end_date": m.get("endDate") or None}
    except Exception:
        pass
    return {"question": "", "end_date": None}


_ASSET_KEYWORDS: dict[str, list[str]] = {
    "BTC": ["BTC", "Bitcoin"],
    "ETH": ["ETH", "Ethereum"],
}


def _question_matches_asset(question: str, asset: str) -> bool:
    q = question.upper()
    return any(kw.upper() in q for kw in _ASSET_KEYWORDS.get(asset.upper(), [asset.upper()]))


# ── Signal helpers ────────────────────────────────────────────────────────────

def _get_signals() -> list[dict]:
    from trading.algo_signals import get_latest_signals
    return get_latest_signals()


def _best_signal_for_asset(st: AlgoAssetState, cfg) -> Optional[dict]:
    """Return the highest-edge unblocked signal for st.asset resolving today."""
    signals = _get_signals()
    lookahead_h = cfg.today_lookahead_hours
    today_utc = (datetime.now(timezone.utc) + timedelta(hours=lookahead_h)).date()

    def _resolves_today(s: dict) -> bool:
        raw = s.get("market_resolution_at") or ""
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).date() == today_utc
        except Exception:
            return False

    alpha = [
        s for s in signals
        if s.get("has_alpha")
        and (s.get("currency") or "").upper() == st.asset
        and _resolves_today(s)
        and not st.is_market_blocked(s.get("polymarket_market_id") or "")
    ]

    if not alpha:
        logger.info("[algo/%s] no alpha signals for today (%s)", st.asset, today_utc)
        return None

    alpha.sort(key=lambda s: float(s.get("abs_edge_pct") or 0), reverse=True)
    return alpha[0]


# ── SCANNING ──────────────────────────────────────────────────────────────────

async def _scan_and_trade(st: AlgoAssetState) -> bool:
    from trading.polymarket_client import (
        create_order as pm_create_order,
        fetch_open_orders as pm_fetch_open_orders,
        fetch_positions as pm_fetch_positions,
    )
    cfg = await _acfg()

    # Guard: existing filled position → switch to MONITORING
    try:
        for pos in await asyncio.to_thread(pm_fetch_positions, True):
            token_id = pos.get("asset") or pos.get("tokenId") or ""
            if not token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                logger.warning("[algo/%s] existing position %s — switching to MONITORING", st.asset, token_id[:20])
                st.state = "MONITORING"
                st.active_token_id = token_id
                st.active_order_id = None
                st.active_outcome = "UNKNOWN"
                if not st.market_end_date:
                    st.market_end_date = info["end_date"]
                return False
    except Exception as exc:
        logger.warning("[algo/%s] pre-scan position check failed: %s", st.asset, exc)

    # Guard: existing open BUY → switch to MONITORING
    try:
        for order in await asyncio.to_thread(pm_fetch_open_orders):
            if (order.get("side") or "").upper() != "BUY":
                continue
            token_id = order.get("asset_id") or order.get("tokenId") or ""
            if not token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                order_id = order.get("id") or order.get("orderID") or ""
                logger.warning("[algo/%s] existing BUY order %s — switching to MONITORING", st.asset, order_id[:20])
                st.state = "MONITORING"
                st.active_token_id = token_id
                st.active_order_id = order_id
                st.active_outcome = "UNKNOWN"
                if not st.market_end_date:
                    st.market_end_date = info["end_date"]
                return False
    except Exception as exc:
        logger.warning("[algo/%s] pre-scan open-order check failed: %s", st.asset, exc)

    best = _best_signal_for_asset(st, cfg)
    if not best:
        return False

    market_id    = best.get("polymarket_market_id") or ""
    deribit_prob = float(best.get("deribit_prob") or 0)
    pm_yes_price = float(best.get("polymarket_price") or 0)
    question     = best.get("polymarket_question") or "?"
    edge_yes     = float(best.get("edge_yes") or 0)
    edge_no      = float(best.get("edge_no") or 0)

    if not market_id:
        logger.warning("[algo/%s] best signal has no market_id — skipping", st.asset)
        return False

    # Determine side: pick the one with higher qualifying edge
    fair = deribit_prob
    can_yes = edge_yes >= _algo.MIN_EDGE and fair >= _algo.MIN_FAIR_PROB
    can_no  = edge_no  >= _algo.MIN_EDGE and (1.0 - fair) >= _algo.MIN_FAIR_PROB

    if can_yes and can_no:
        outcome_target = "YES" if edge_yes >= edge_no else "NO"
    elif can_yes:
        outcome_target = "YES"
    elif can_no:
        outcome_target = "NO"
    else:
        logger.info("[algo/%s] signal no longer qualifies — skipping", st.asset)
        return False

    try:
        yes_token, no_token = await asyncio.to_thread(_resolve_token_ids, market_id)
    except Exception as exc:
        logger.error("[algo/%s] failed to resolve market %r: %s", st.asset, market_id, exc)
        return False

    if outcome_target == "YES":
        token_id = yes_token
        price    = pm_yes_price
        entry_edge = edge_yes
    else:
        token_id = no_token
        price    = round(1.0 - pm_yes_price, 4)
        entry_edge = edge_no

    if not (0 < price < 1):
        logger.warning("[algo/%s] invalid price %.4f — skipping", st.asset, price)
        return False

    if price > cfg.max_poly_entry_price:
        logger.info("[algo/%s] price %.4f > max %.2f for '%s' — skipping", st.asset, price, cfg.max_poly_entry_price, question[:50])
        return False

    size = max(cfg.min_shares, round(cfg.order_usd / price, 2))

    logger.info(
        "[algo/%s] SCAN → BUY %s '%s'  price=%.4f  size=%.2f  edge=%.1f%%  deribit=%.3f  conf=%s",
        st.asset, outcome_target, question[:70], price, size,
        float(best.get("abs_edge_pct") or 0), deribit_prob,
        best.get("interp_confidence", "?"),
    )

    try:
        resp = await asyncio.to_thread(pm_create_order, token_id, price, size, "BUY")
    except Exception as exc:
        logger.error("[algo/%s] order placement error: %s", st.asset, exc)
        return False

    if not resp.get("success"):
        logger.error("[algo/%s] order rejected: %s", st.asset, resp.get("errorMsg", resp))
        return False

    order_id = resp.get("orderID")
    logger.info("[algo/%s] order placed id=%s — switching to MONITORING", st.asset, order_id)
    st.state = "MONITORING"
    st.active_token_id = token_id
    st.active_order_id = order_id
    st.active_outcome = outcome_target
    st.active_market_id = market_id
    st.entry_edge = round(entry_edge, 6)
    st.fill_time = None
    if not st.market_end_date:
        st.market_end_date = best.get("market_resolution_at") or None
    return True


# ── Signal-based exit via evaluate_exit() ────────────────────────────────────

def _check_algo_exit(st: AlgoAssetState, live_price: float, cfg) -> Optional[str]:
    """
    Use algorithms_2026-05-28.evaluate_exit() for exit decisions.

    Note: early-collapse and edge-sign-flip rules are enabled in evaluate_exit()
    but require a valid fill_time and entry_edge. If these are missing (position
    resumed from startup), the function returns None (HOLD).
    """
    if st.active_outcome not in ("YES", "NO") or not st.active_market_id:
        return None

    signals = _get_signals()
    match = next(
        (s for s in signals if s.get("polymarket_market_id") == st.active_market_id),
        None,
    )
    if match is None:
        logger.info("[algo/%s] market %s not in latest signals — holding", st.asset, st.active_market_id)
        return None

    try:
        deribit_prob_yes = float(match.get("deribit_prob") or 0)
    except (TypeError, ValueError):
        return None

    current_fair = deribit_prob_yes if st.active_outcome == "YES" else 1.0 - deribit_prob_yes

    entry_edge = st.entry_edge
    fill_time_str = st.fill_time

    if entry_edge is None or fill_time_str is None:
        # Not enough context — fall back to simple conviction check only
        if current_fair < cfg.min_fair_prob:
            return f"deribit_fair_{current_fair:.3f}_lt_{cfg.min_fair_prob}"
        return None

    try:
        fill_dt = datetime.fromisoformat(fill_time_str)
        if fill_dt.tzinfo is None:
            fill_dt = fill_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    decision = evaluate_exit(
        side=st.active_outcome,
        current_fair=current_fair,
        current_pm_price=live_price,
        entry_edge=entry_edge,
        fill_time=fill_dt,
        min_fair_prob=cfg.min_fair_prob,
        early_collapse_window_s=cfg.early_collapse_window_s,
        early_collapse_threshold=cfg.early_collapse_edge_threshold,
    )

    if decision.action == "EXIT":
        return decision.reason
    return None


# ── MONITORING ────────────────────────────────────────────────────────────────

async def _monitor_position(st: AlgoAssetState) -> None:
    from trading.polymarket_client import (
        fetch_positions as pm_fetch_positions,
        create_order as pm_create_order,
        cancel_order as pm_cancel_order,
        fetch_best_bid as pm_fetch_best_bid,
    )
    cfg = await _acfg()

    try:
        positions = await asyncio.to_thread(pm_fetch_positions, True)
    except Exception as exc:
        logger.error("[algo/%s] fetch_positions failed: %s", st.asset, exc)
        return

    pos = next(
        (p for p in positions
         if p.get("asset") == st.active_token_id or p.get("tokenId") == st.active_token_id),
        None,
    )

    if pos is None:
        if st.active_sell_order_id:
            logger.info("[algo/%s] SELL %s filled — closing", st.asset, st.active_sell_order_id[:20])
            st.close_and_promote()
            return
        if st.active_order_id:
            logger.info("[algo/%s] BUY %s not filled — cancelling", st.asset, st.active_order_id[:20])
            try:
                await asyncio.to_thread(pm_cancel_order, st.active_order_id)
            except Exception as exc:
                logger.warning("[algo/%s] cancel failed (%s) — retrying next cycle", st.asset, exc)
                return
        st.reset()
        await _scan_and_trade(st)
        return

    avg  = float(pos.get("avgPrice") or 0)
    cur  = float(pos.get("curPrice") or 0)
    size = float(pos.get("size") or 0)

    if avg <= 0 or size <= 0:
        return

    if st.active_order_id is not None:
        logger.info("[algo/%s] BUY fill confirmed", st.asset)
        st.fill_time = datetime.now(timezone.utc).isoformat()
        st.active_order_id = None

    live_bid  = await asyncio.to_thread(pm_fetch_best_bid, st.active_token_id)
    live_price = live_bid if (live_bid and live_bid > 0.01) else cur
    pnl_pct   = (live_price - avg) / avg * 100.0

    logger.info(
        "[algo/%s] MONITOR %s: avg=%.4f cur=%.4f live=%.4f size=%.2f pnl=%.2f%%",
        st.asset, (st.active_token_id or "")[:20], avg, cur, live_bid or 0, size, pnl_pct,
    )

    # Cancel stale SELL
    if st.active_sell_order_id:
        logger.info("[algo/%s] SELL %s not filled — re-placing", st.asset, st.active_sell_order_id[:20])
        try:
            await asyncio.to_thread(pm_cancel_order, st.active_sell_order_id)
            st.active_sell_order_id = None
        except Exception as exc:
            logger.warning("[algo/%s] cancel SELL failed (%s) — retrying", st.asset, exc)
            return

    def _sell(reason: str) -> None:
        pass  # placeholder — actual sell is inline below

    async def _place_sell(reason: str) -> None:
        sell_price = max(0.0001, min(0.9999, round((live_bid if live_bid else cur - 0.01), 4)))
        logger.warning("[algo/%s] %s (pnl=%.2f%%) — closing at %.4f", st.asset, reason, pnl_pct, sell_price)
        try:
            resp = await asyncio.to_thread(pm_create_order, st.active_token_id, sell_price, size, "SELL")
        except Exception as exc:
            logger.error("[algo/%s] SELL order error: %s", st.asset, exc)
            return
        if not resp.get("success"):
            logger.error("[algo/%s] SELL rejected: %s", st.asset, resp.get("errorMsg", resp))
            return
        st.active_sell_order_id = resp.get("orderID")
        if st.active_market_id:
            st.block_market(st.active_market_id)
            logger.info("[algo/%s] market %s blocked (90 min cooldown)", st.asset, st.active_market_id)

    # Expiry close
    if st.market_end_date:
        try:
            end_dt = datetime.fromisoformat(st.market_end_date.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= datetime.now(timezone.utc):
                await _place_sell("market_expired")
                return
        except Exception:
            pass

    # Signal-based exit
    exit_reason = _check_algo_exit(st, live_price, cfg)
    if exit_reason:
        await _place_sell(f"signal_exit({exit_reason})")
        return

    # Hard stop-loss
    if pnl_pct <= cfg.stop_loss_pct:
        await _place_sell(f"stop_loss({pnl_pct:.1f}%)")
        return

    # Profit target
    if pnl_pct >= cfg.profit_target_pct:
        await _place_sell(f"profit_target({pnl_pct:.1f}%)")
        return


# ── Main loop ─────────────────────────────────────────────────────────────────

async def algo_trader_loop(asset: str) -> None:
    """
    Main trading loop for a single asset. Runs forever until cancelled.
    Reads scan_interval_s and trading_enabled from TradingConfig each cycle
    so admin changes take effect without a restart.
    """
    st = AlgoAssetState(asset)
    logger.info("[algo/%s] starting — state=%s", asset, st.state)

    while True:
        try:
            cfg = await _acfg()

            if not cfg.trading_enabled:
                logger.info("[algo/%s] trading_enabled=False — sleeping", asset)
                await asyncio.sleep(cfg.scan_interval_s)
                continue

            if st.state == "SCANNING":
                await _scan_and_trade(st)
            elif st.state == "MONITORING":
                await _monitor_position(st)
            else:
                logger.warning("[algo/%s] unknown state %r — resetting", asset, st.state)
                st.reset()

        except asyncio.CancelledError:
            logger.info("[algo/%s] loop cancelled", asset)
            raise
        except Exception as exc:
            logger.exception("[algo/%s] unhandled error: %s", asset, exc)

        await asyncio.sleep((await _acfg()).scan_interval_s)
