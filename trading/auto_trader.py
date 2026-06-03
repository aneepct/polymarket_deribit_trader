"""
auto_trader.py — Autonomous 60-second trading loop, one per asset (BTC / ETH).

Ported to Django + Redis:
  - State machine backed by Redis (AssetState) — survives restarts.
  - All tuning constants come from TradingConfig (Django admin).
  - Polymarket credentials come from PolymarketCredentials (encrypted in DB).

State machine (per asset):
  SCANNING   → every N s: fetch latest alpha signals, place a BUY order.
  MONITORING → every N s: check position P&L.
               • SELL not filled → cancel and re-place at fresh price.
               • BUY not filled  → cancel and re-scan.
               • Signal-based exit (spec v1.1 + early-collapse 2026-05-25):
                 - Deribit fair for held side < 0.51     → conviction gone
                 - Edge sign flipped vs entry            → signal reversed
                 - |edge| <= 1pp within 10 min of fill  → early-collapse
                 - Signal absent from CSV               → conviction gone
               • P&L >= profit_target_pct → place SELL to close.
               • Market expired           → place SELL to close.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from trading.state import AssetState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helper — read fresh from DB each cycle so admin changes take effect
# ---------------------------------------------------------------------------

def _cfg():
    from trading.models import TradingConfig
    return TradingConfig.load()


async def _acfg():
    """Async-safe wrapper — always use this from async functions."""
    return await asyncio.to_thread(_cfg)


# ---------------------------------------------------------------------------
# Gamma API helpers
# ---------------------------------------------------------------------------

def _resolve_token_ids(market_id: str) -> list[str]:
    """Return [yes_token_id, no_token_id] for a Gamma market ID / condition_id."""
    cfg = _cfg()
    gamma = cfg.gamma_api
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

    try:
        r = httpx.get(f"{gamma}/markets/{market_id}", timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            raw = data if isinstance(data, dict) else (data[0] if data else {})
            result = _extract_ids(raw)
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
    """Return {'question': str, 'end_date': str|None} for a CLOB token ID."""
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
    for kw in _ASSET_KEYWORDS.get(asset.upper(), [asset.upper()]):
        if kw.upper() in q:
            return True
    return False


# ---------------------------------------------------------------------------
# Signal source — reads from the local csv_signals module
# ---------------------------------------------------------------------------

def _get_signals() -> list[dict]:
    from trading.csv_signals import get_latest_signals
    return get_latest_signals()


# ---------------------------------------------------------------------------
# SCANNING phase
# ---------------------------------------------------------------------------

async def _scan_and_trade(st: AssetState) -> bool:
    from trading.polymarket_client import (
        create_order as pm_create_order,
        fetch_open_orders as pm_fetch_open_orders,
        fetch_positions as pm_fetch_positions,
    )
    cfg = await _acfg()

    # Guard 1: existing filled position → switch to MONITORING
    try:
        existing_positions = await asyncio.to_thread(pm_fetch_positions, True)
        for pos in existing_positions:
            token_id = pos.get("asset") or pos.get("tokenId") or ""
            if not token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                logger.warning(
                    "%s aborting scan — existing filled position token=%s; switching to MONITORING",
                    st.tag, token_id[:20],
                )
                st.state = "MONITORING"
                st.active_token_id = token_id
                st.active_order_id = None
                st.active_outcome = "UNKNOWN"
                if not st.market_end_date:
                    st.market_end_date = info["end_date"]
                return False
    except Exception as exc:
        logger.warning("%s pre-scan position check failed: %s", st.tag, exc)

    # Guard 2: existing unfilled BUY order → switch to MONITORING
    try:
        open_orders = await asyncio.to_thread(pm_fetch_open_orders)
        for order in open_orders:
            if (order.get("side") or "").upper() != "BUY":
                continue
            token_id = order.get("asset_id") or order.get("tokenId") or ""
            if not token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                order_id = order.get("id") or order.get("orderID") or ""
                logger.warning(
                    "%s aborting scan — existing open BUY order id=%s; switching to MONITORING",
                    st.tag, order_id[:20],
                )
                st.state = "MONITORING"
                st.active_token_id = token_id
                st.active_order_id = order_id
                st.active_outcome = "UNKNOWN"
                if not st.market_end_date:
                    st.market_end_date = info["end_date"]
                return False
    except Exception as exc:
        logger.warning("%s pre-scan open-order check failed: %s", st.tag, exc)

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
    ]
    if not alpha:
        asset_signals = [s for s in signals if (s.get("currency") or "").upper() == st.asset]
        today_signals = [s for s in asset_signals if _resolves_today(s)]
        alpha_signals = [s for s in today_signals if s.get("has_alpha")]
        logger.info(
            "%s no alpha signals resolving today (%s) — total=%d asset=%d today=%d has_alpha=%d",
            st.tag, today_utc, len(signals), len(asset_signals), len(today_signals), len(alpha_signals),
        )
        return False

    alpha.sort(key=lambda s: float(s.get("abs_edge_pct") or 0), reverse=True)
    best = alpha[0]

    market_id    = best.get("polymarket_market_id") or ""
    poly_price   = float(best.get("polymarket_price") or 0)
    deribit_prob = float(best.get("deribit_prob") or 0)
    question     = best.get("polymarket_question") or "?"
    abs_edge     = float(best.get("abs_edge_pct") or 0)

    if not market_id:
        logger.warning("%s best signal has no polymarket_market_id; skipping", st.tag)
        return False

    if deribit_prob > cfg.deribit_neutral_high:
        outcome_target = "YES"
    elif deribit_prob < cfg.deribit_neutral_low:
        outcome_target = "NO"
    else:
        logger.info(
            "%s deribit_prob=%.3f in neutral band (%.2f-%.2f) — skipping",
            st.tag, deribit_prob, cfg.deribit_neutral_low, cfg.deribit_neutral_high,
        )
        return False

    try:
        yes_token, no_token = await asyncio.to_thread(_resolve_token_ids, market_id)
    except Exception as exc:
        logger.error("%s failed to resolve market %r: %s", st.tag, market_id, exc)
        return False

    if outcome_target == "YES":
        token_id = yes_token
        price = poly_price
        outcome = "YES"
    else:
        token_id = no_token
        price = round(1.0 - poly_price, 4)
        outcome = "NO"

    if not (0 < price < 1):
        logger.warning("%s invalid price %.4f for %r; skipping", st.tag, price, market_id)
        return False

    size = round(cfg.order_usd / price, 2)
    size = max(cfg.min_shares, size)

    logger.info(
        "%s SCAN → BUY %s '%s'  price=%.4f  size=%.2f  edge=%.1f%%",
        st.tag, outcome, question[:70], price, size, abs_edge,
    )

    try:
        resp = await asyncio.to_thread(pm_create_order, token_id, price, size, "BUY")
    except Exception as exc:
        logger.error("%s order placement error: %s", st.tag, exc)
        return False

    if not resp.get("success"):
        logger.error("%s order rejected: %s", st.tag, resp.get("errorMsg", resp))
        return False

    order_id = resp.get("orderID")
    logger.info("%s order placed id=%s — switching to MONITORING", st.tag, order_id)

    st.state = "MONITORING"
    st.active_token_id = token_id
    st.active_order_id = order_id
    st.active_outcome = outcome
    st.active_market_id = market_id
    if outcome == "YES":
        st.entry_edge = round(deribit_prob - poly_price, 6)
    else:
        st.entry_edge = round((1.0 - deribit_prob) - (1.0 - poly_price), 6)
    st.fill_time = None  # set when fill confirmed in _monitor_position
    if not st.market_end_date:
        st.market_end_date = best.get("market_resolution_at") or None
    return True


# ---------------------------------------------------------------------------
# Signal-based exit predicates (spec v1.1 + early-collapse rule 2026-05-25)
# ---------------------------------------------------------------------------

def _check_signal_exit(st: AssetState, current_pm_price: float, cfg) -> Optional[str]:
    """
    Apply spec v1.1 exit rules from algorithms_2026-05-28.py / evaluate_exit().
    Returns an exit reason string if a rule fires, or None to HOLD.

    Rules (in priority order):
      1. Signal absent from CSV          → Deribit conviction gone
      2. Deribit fair for held side      < cfg.min_fair_prob (default 0.51)
      3. Edge sign flip vs entry_edge recorded at order placement
      4. Early-collapse: |edge| <= cfg.early_collapse_edge_threshold
                          within cfg.early_collapse_window_s seconds of fill

    Skips gracefully if active_market_id or active_outcome are unset
    (e.g. positions resumed from startup without full context).
    """
    if st.active_outcome not in ("YES", "NO") or not st.active_market_id:
        return None

    signals = _get_signals()
    match = next(
        (s for s in signals if s.get("polymarket_market_id") == st.active_market_id),
        None,
    )
    if match is None:
        logger.info(
            "%s signal for market %s absent from CSV — treating as conviction gone",
            st.tag, st.active_market_id,
        )
        return "signal_not_in_csv"

    try:
        deribit_prob_yes = float(match.get("deribit_prob") or 0)
    except (TypeError, ValueError):
        return None

    current_fair = deribit_prob_yes if st.active_outcome == "YES" else 1.0 - deribit_prob_yes

    # Rule 1: Deribit conviction below threshold.
    if current_fair < cfg.min_fair_prob:
        return f"deribit_fair_{current_fair:.3f}_lt_{cfg.min_fair_prob}"

    entry_edge = st.entry_edge
    if entry_edge is None:
        return None  # no entry context (resumed from startup) — skip

    current_edge = current_fair - current_pm_price

    # Rule 2: edge sign flipped.
    if (entry_edge > 0 and current_edge < 0) or (entry_edge < 0 and current_edge > 0):
        return f"edge_sign_flip_entry_{entry_edge:.3f}_now_{current_edge:.3f}"

    # Rule 3: early-collapse within window after fill.
    fill_time_str = st.fill_time
    if fill_time_str:
        try:
            fill_dt = datetime.fromisoformat(fill_time_str)
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            elapsed_s = (datetime.now(timezone.utc) - fill_dt).total_seconds()
            if elapsed_s <= cfg.early_collapse_window_s and abs(current_edge) <= cfg.early_collapse_edge_threshold:
                return f"early_collapse_edge_{abs(current_edge):.3f}_at_{int(elapsed_s)}s"
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# MONITORING phase
# ---------------------------------------------------------------------------

async def _monitor_position(st: AssetState) -> None:
    from trading.polymarket_client import (
        fetch_positions as pm_fetch_positions,
        create_order as pm_create_order,
        cancel_order as pm_cancel_order,
    )
    cfg = await _acfg()

    try:
        positions = await asyncio.to_thread(pm_fetch_positions, True)
    except Exception as exc:
        logger.error("%s fetch_positions failed: %s", st.tag, exc)
        return

    pos = next(
        (p for p in positions
         if p.get("asset") == st.active_token_id or p.get("tokenId") == st.active_token_id),
        None,
    )

    if pos is None:
        # SELL filled — position gone
        if st.active_sell_order_id:
            logger.info("%s SELL order %s filled — position closed", st.tag, st.active_sell_order_id[:20])
            st.close_and_promote()
            return
        # BUY not filled — cancel and re-scan
        if st.active_order_id:
            logger.info("%s BUY order %s not filled after interval — cancelling", st.tag, st.active_order_id[:20])
            try:
                await asyncio.to_thread(pm_cancel_order, st.active_order_id)
                logger.info("%s BUY cancelled — re-scanning", st.tag)
            except Exception as exc:
                logger.warning("%s cancel failed (%s) — will retry next cycle", st.tag, exc)
                return
        else:
            logger.debug("%s no open position for %s", st.tag, (st.active_token_id or "")[:20])

        if st.extra_token_ids:
            extras = st.extra_token_ids
            promoted = extras.pop(0)
            st.extra_token_ids = extras
            st.active_token_id = promoted
            st.active_order_id = None
            st.active_outcome = "UNKNOWN"
            logger.info("%s promoted extra %s to active", st.tag, promoted[:20])
            return

        st.reset()
        await _scan_and_trade(st)
        return

    avg = float(pos.get("avgPrice") or 0)
    cur = float(pos.get("curPrice") or 0)
    size = float(pos.get("size") or 0)

    if avg <= 0 or size <= 0:
        return

    # Detect BUY fill: first cycle where position is present while active_order_id is still held.
    if st.active_order_id is not None:
        logger.info(
            "%s BUY order %s fill confirmed — position established",
            st.tag, st.active_order_id[:20],
        )
        st.fill_time = datetime.now(timezone.utc).isoformat()
        st.active_order_id = None

    pnl_pct = (cur - avg) / avg * 100.0
    logger.info(
        "%s MONITOR %s: avg=%.4f cur=%.4f size=%.2f pnl=%.2f%%",
        st.tag, (st.active_token_id or "")[:20], avg, cur, size, pnl_pct,
    )

    # Cancel stale SELL and re-evaluate at current price
    if st.active_sell_order_id:
        logger.info("%s SELL order %s not filled — cancelling to re-place", st.tag, st.active_sell_order_id[:20])
        try:
            await asyncio.to_thread(pm_cancel_order, st.active_sell_order_id)
            st.active_sell_order_id = None
        except Exception as exc:
            logger.warning("%s cancel SELL failed (%s) — will retry next cycle", st.tag, exc)
            return

    # ── Expiry close ─────────────────────────────────────────────────────────
    if st.market_end_date:
        try:
            end_dt = datetime.fromisoformat(st.market_end_date.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= datetime.now(timezone.utc):
                sell_price = max(0.0001, min(0.9999, round(cur - 0.01, 4)))
                logger.warning(
                    "%s market expired — closing at %.4f (pnl=%.2f%%)", st.tag, sell_price, pnl_pct
                )
                try:
                    resp = await asyncio.to_thread(pm_create_order, st.active_token_id, sell_price, size, "SELL")
                except Exception as exc:
                    logger.error("%s expiry close error: %s", st.tag, exc)
                    return
                if not resp.get("success"):
                    logger.error("%s expiry close rejected: %s", st.tag, resp.get("errorMsg", resp))
                    return
                logger.info("%s expiry close placed id=%s", st.tag, resp.get("orderID"))
                st.active_sell_order_id = resp.get("orderID")
                return
        except Exception:
            pass

    # ── Signal-based exit (spec v1.1 + early-collapse rule 2026-05-25) ──────
    # Replaces the P&L stop-loss for the primary position. Exits when Deribit
    # conviction is gone, the edge has flipped, or early-collapse rule fires.
    _exit_reason = _check_signal_exit(st, cur, cfg)
    if _exit_reason:
        sell_price = max(0.0001, min(0.9999, round(cur - 0.01, 4)))
        logger.warning(
            "%s signal-exit triggered (%s) pnl=%.2f%% — closing at %.4f",
            st.tag, _exit_reason, pnl_pct, sell_price,
        )
        try:
            resp = await asyncio.to_thread(pm_create_order, st.active_token_id, sell_price, size, "SELL")
        except Exception as exc:
            logger.error("%s signal-exit order error: %s", st.tag, exc)
            return
        if not resp.get("success"):
            logger.error("%s signal-exit order rejected: %s", st.tag, resp.get("errorMsg", resp))
            return
        logger.info("%s signal-exit order placed id=%s — waiting for fill", st.tag, resp.get("orderID"))
        st.active_sell_order_id = resp.get("orderID")
        return

    # ── Extra positions ──────────────────────────────────────────────────────
    extras = list(st.extra_token_ids)
    extras_changed = False
    for extra_token_id in list(extras):
        extra_pos = next(
            (p for p in positions
             if p.get("asset") == extra_token_id or p.get("tokenId") == extra_token_id),
            None,
        )
        if extra_pos is None:
            logger.info("%s extra %s no longer open — removing", st.tag, extra_token_id[:20])
            extras.remove(extra_token_id)
            extras_changed = True
            continue

        e_avg = float(extra_pos.get("avgPrice") or 0)
        e_cur = float(extra_pos.get("curPrice") or 0)
        e_size = float(extra_pos.get("size") or 0)
        if e_avg <= 0 or e_size <= 0:
            continue

        e_pnl_pct = (e_cur - e_avg) / e_avg * 100.0

        should_close = False
        reason = ""

        # Expiry
        if st.market_end_date:
            try:
                end_dt = datetime.fromisoformat(st.market_end_date.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if end_dt <= datetime.now(timezone.utc):
                    should_close = True
                    reason = "expired"
            except Exception:
                pass

        if not should_close and e_pnl_pct <= cfg.stop_loss_pct:
            should_close = True
            reason = f"stop-loss {e_pnl_pct:.1f}%"

        if not should_close and e_pnl_pct >= cfg.profit_target_pct:
            should_close = True
            reason = f"profit {e_pnl_pct:.1f}%"

        if should_close:
            sell_price = max(0.0001, min(0.9999, round(e_cur - 0.01, 4)))
            logger.info("%s closing extra %s (%s) at %.4f", st.tag, extra_token_id[:20], reason, sell_price)
            try:
                resp = await asyncio.to_thread(pm_create_order, extra_token_id, sell_price, e_size, "SELL")
            except Exception as exc:
                logger.error("%s extra close error: %s", st.tag, exc)
                continue
            if resp.get("success"):
                extras.remove(extra_token_id)
                extras_changed = True

    if extras_changed:
        st.extra_token_ids = extras

    # ── Profit target ────────────────────────────────────────────────────────
    if pnl_pct < cfg.profit_target_pct:
        return

    sell_price = max(0.0001, min(0.9999, round(cur - 0.01, 4)))
    logger.info("%s %.1f%% profit — closing at %.4f", st.tag, pnl_pct, sell_price)

    try:
        resp = await asyncio.to_thread(pm_create_order, st.active_token_id, sell_price, size, "SELL")
    except Exception as exc:
        logger.error("%s close error: %s", st.tag, exc)
        return

    if not resp.get("success"):
        logger.error("%s close rejected: %s", st.tag, resp.get("errorMsg", resp))
        return

    logger.info("%s close placed id=%s — waiting for fill", st.tag, resp.get("orderID"))
    st.active_sell_order_id = resp.get("orderID")


# ---------------------------------------------------------------------------
# Startup resume
# ---------------------------------------------------------------------------

async def _resume_if_position_open(st: AssetState) -> None:
    from trading.polymarket_client import (
        fetch_positions as pm_fetch_positions,
        fetch_open_orders as pm_fetch_open_orders,
    )

    try:
        open_orders = await asyncio.to_thread(pm_fetch_open_orders)
        for order in [o for o in open_orders if (o.get("side") or "").upper() == "BUY"]:
            token_id = order.get("asset_id") or order.get("tokenId") or ""
            if not token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                order_id = order.get("id") or order.get("orderID") or ""
                logger.info("%s found open BUY order id=%s — resuming MONITORING", st.tag, order_id[:20])
                st.state = "MONITORING"
                st.active_token_id = token_id
                st.active_order_id = order_id
                st.active_outcome = "UNKNOWN"
                if not st.market_end_date:
                    st.market_end_date = info["end_date"]
                break
    except Exception as exc:
        logger.warning("%s startup open-order check failed: %s", st.tag, exc)

    try:
        positions = await asyncio.to_thread(pm_fetch_positions, True)
        for pos in positions:
            token_id = pos.get("asset") or pos.get("tokenId") or ""
            if not token_id or token_id == st.active_token_id:
                continue
            info = await asyncio.to_thread(_market_info_for_token, token_id)
            if _question_matches_asset(info["question"] or "", st.asset):
                avg = float(pos.get("avgPrice") or 0)
                if st.active_token_id is None:
                    logger.info("%s found existing position token=%s — resuming MONITORING", st.tag, token_id[:20])
                    st.state = "MONITORING"
                    st.active_token_id = token_id
                    st.active_order_id = None
                    st.active_outcome = "UNKNOWN"
                    if not st.market_end_date:
                        st.market_end_date = info["end_date"]
                else:
                    logger.info("%s found additional position token=%s — tracking as extra", st.tag, token_id[:20])
                    extras = st.extra_token_ids
                    extras.append(token_id)
                    st.extra_token_ids = extras
    except Exception as exc:
        logger.warning("%s startup position check failed: %s", st.tag, exc)


# ---------------------------------------------------------------------------
# Per-asset loop
# ---------------------------------------------------------------------------

async def auto_trader_loop(asset: str) -> None:
    """
    Main loop for a single asset. Run one per asset as concurrent asyncio tasks.
    Interval and all tuning values are re-read from DB on every cycle.
    """
    st = AssetState(asset=asset.upper())
    logger.info("%s starting", st.tag)

    try:
        await _resume_if_position_open(st)
    except Exception as exc:
        logger.exception("%s _resume_if_position_open failed: %s", st.tag, exc)

    _paused_logged = False
    while True:
        cfg = await _acfg()
        if not cfg.trading_enabled:
            if not _paused_logged:
                logger.info("%s trading_enabled=False — loop paused", st.tag)
                _paused_logged = True
            await asyncio.sleep(cfg.scan_interval_s)
            continue
        _paused_logged = False

        try:
            if st.state == "SCANNING":
                await _scan_and_trade(st)
            else:
                await _monitor_position(st)
        except asyncio.CancelledError:
            logger.info("%s cancelled — shutting down", st.tag)
            raise
        except Exception as exc:
            logger.exception("%s unexpected error: %s", st.tag, exc)

        interval = (await _acfg()).scan_interval_s
        await asyncio.sleep(interval)
