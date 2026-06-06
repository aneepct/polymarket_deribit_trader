"""
paper_trader.py — Paper trading loop using the delta-interpolation algorithm.

Simulates trades with zero real orders. Uses live Polymarket prices from
algo_signals and the public /book endpoint for current bid prices.

State is stored in Redis under 'paper:state:<ASSET>' — isolated from both
the main trader ('trader:state:') and the algo trader ('algo:state:').

Each completed paper trade is appended to a JSON-lines log file at:
    <BASE_DIR>/paper_trades.jsonl   (configurable via PAPER_TRADE_LOG in settings)

Running P&L summary is also stored in Redis under 'paper:pnl:<ASSET>'.

Start with:
    python manage.py run_paper_trader
    python manage.py run_paper_trader --assets BTC
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from django.core.cache import cache

from trading.algo_math import evaluate_exit, MIN_EDGE, MIN_FAIR_PROB

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg():
    from trading.models import TradingConfig
    return TradingConfig.load()


async def _acfg():
    return await asyncio.to_thread(_cfg)


def _log_path() -> pathlib.Path:
    from django.conf import settings
    base = pathlib.Path(getattr(settings, "BASE_DIR", pathlib.Path(__file__).parent.parent))
    custom = getattr(settings, "PAPER_TRADE_LOG", None)
    return pathlib.Path(custom) if custom else base / "paper_trades.jsonl"


# ── Redis state ───────────────────────────────────────────────────────────────

_PAPER_PREFIX = "paper:state"
_PAPER_PNL    = "paper:pnl"


def _paper_key(asset: str) -> str:
    return f"{_PAPER_PREFIX}:{asset.upper()}"


def _ttl() -> int:
    try:
        return int(_cfg().redis_state_ttl_hours * 3600)
    except Exception:
        return 48 * 3600


class PaperAssetState:
    """
    Lightweight Redis-backed paper position state.
    Stores under 'paper:state:<ASSET>' — no conflict with live traders.
    """

    def __init__(self, asset: str):
        self.asset = asset.upper()

    @property
    def tag(self) -> str:
        return f"[paper/{self.asset}]"

    def _get(self, field, default=None):
        val = cache.get(_paper_key(self.asset))
        return (val or {}).get(field, default)

    def _set(self, **kwargs):
        key = _paper_key(self.asset)
        current = cache.get(key) or {}
        current.update(kwargs)
        cache.set(key, current, timeout=_ttl())

    @property
    def state(self) -> str:
        return self._get("state", "SCANNING")

    @state.setter
    def state(self, v: str):
        self._set(state=v)

    @property
    def active_market_id(self) -> Optional[str]:
        return self._get("active_market_id")

    @active_market_id.setter
    def active_market_id(self, v: Optional[str]):
        self._set(active_market_id=v)

    @property
    def active_outcome(self) -> Optional[str]:
        return self._get("active_outcome")

    @active_outcome.setter
    def active_outcome(self, v: Optional[str]):
        self._set(active_outcome=v)

    @property
    def active_token_id(self) -> Optional[str]:
        return self._get("active_token_id")

    @active_token_id.setter
    def active_token_id(self, v: Optional[str]):
        self._set(active_token_id=v)

    @property
    def entry_price(self) -> Optional[float]:
        return self._get("entry_price")

    @entry_price.setter
    def entry_price(self, v: Optional[float]):
        self._set(entry_price=v)

    @property
    def entry_edge(self) -> Optional[float]:
        return self._get("entry_edge")

    @entry_edge.setter
    def entry_edge(self, v: Optional[float]):
        self._set(entry_edge=v)

    @property
    def fill_time(self) -> Optional[str]:
        return self._get("fill_time")

    @fill_time.setter
    def fill_time(self, v: Optional[str]):
        self._set(fill_time=v)

    @property
    def market_end_date(self) -> Optional[str]:
        return self._get("market_end_date")

    @market_end_date.setter
    def market_end_date(self, v: Optional[str]):
        self._set(market_end_date=v)

    @property
    def question(self) -> Optional[str]:
        return self._get("question")

    @question.setter
    def question(self, v: Optional[str]):
        self._set(question=v)

    @property
    def entry_size_usd(self) -> float:
        return float(self._get("entry_size_usd") or 0.0)

    @entry_size_usd.setter
    def entry_size_usd(self, v: float):
        self._set(entry_size_usd=v)

    @property
    def blocked_markets(self) -> dict:
        return self._get("blocked_markets", {})

    @blocked_markets.setter
    def blocked_markets(self, v: dict):
        self._set(blocked_markets=v)

    def block_market(self, market_id: str, cooldown_minutes: int = 90) -> None:
        import time
        blocked = self.blocked_markets
        blocked[market_id] = time.time() + cooldown_minutes * 60
        self.blocked_markets = blocked

    def is_market_blocked(self, market_id: str) -> bool:
        import time
        blocked = self.blocked_markets
        unblock_at = blocked.get(market_id)
        if unblock_at is None:
            return False
        if time.time() < unblock_at:
            return True
        del blocked[market_id]
        self.blocked_markets = blocked
        return False

    def reset(self) -> None:
        cache.set(_paper_key(self.asset), {
            "state": "SCANNING",
            "active_market_id": None,
            "active_outcome": None,
            "active_token_id": None,
            "entry_price": None,
            "entry_edge": None,
            "fill_time": None,
            "market_end_date": None,
            "question": None,
            "entry_size_usd": None,
            "blocked_markets": self.blocked_markets,
        }, timeout=_ttl())


# ── Running P&L tracker ───────────────────────────────────────────────────────

class PaperPnL:
    """Tracks cumulative paper P&L in Redis."""

    def __init__(self, asset: str):
        self._key = f"{_PAPER_PNL}:{asset.upper()}"
        self._asset = asset.upper()

    def _load(self) -> dict:
        return cache.get(self._key) or {
            "asset": self._asset,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_usd": 0.0,
            "total_spent_usd": 0.0,
        }

    def record(self, spent_usd: float, received_usd: float) -> None:
        d = self._load()
        pnl = received_usd - spent_usd
        d["total_trades"] += 1
        d["wins"] += 1 if pnl > 0 else 0
        d["losses"] += 1 if pnl < 0 else 0
        d["total_pnl_usd"] = round(d["total_pnl_usd"] + pnl, 4)
        d["total_spent_usd"] = round(d["total_spent_usd"] + spent_usd, 4)
        cache.set(self._key, d, timeout=_ttl())

    def summary(self) -> dict:
        return self._load()


# ── Trade logging ─────────────────────────────────────────────────────────────

def _append_trade_log(record: dict) -> None:
    """Append a completed paper trade to the JSONL log file."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("[paper] failed to write trade log: %s", exc)


# ── Signal helpers ────────────────────────────────────────────────────────────

def _best_signal(st: PaperAssetState, cfg) -> Optional[dict]:
    from trading.algo_signals import get_latest_signals
    signals = get_latest_signals()
    lookahead_h = cfg.today_lookahead_hours
    today_utc = (datetime.now(timezone.utc) + timedelta(hours=lookahead_h)).date()

    def _resolves_today(s: dict) -> bool:
        raw = s.get("market_resolution_at") or ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).date() == today_utc
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
        return None
    alpha.sort(key=lambda s: float(s.get("abs_edge_pct") or 0), reverse=True)
    return alpha[0]


def _current_signal(market_id: str) -> Optional[dict]:
    """Return the latest signal for an already-held market_id, or None."""
    from trading.algo_signals import get_latest_signals
    return next(
        (s for s in get_latest_signals() if s.get("polymarket_market_id") == market_id),
        None,
    )


def _current_price(market_id: str, outcome: str, token_id: Optional[str]) -> Optional[float]:
    """
    Get current price for the held side.
    Tries the live /book endpoint first (read-only), falls back to signal price.
    """
    # Try live bid from public CLOB book endpoint
    if token_id:
        try:
            from trading.polymarket_client import fetch_best_bid
            bid = fetch_best_bid(token_id)
            if bid and bid > 0.01:
                return bid if outcome == "YES" else bid
        except Exception:
            pass

    # Fall back to signal price
    sig = _current_signal(market_id)
    if sig is None:
        return None
    pm_yes = float(sig.get("polymarket_price") or 0)
    return pm_yes if outcome == "YES" else round(1.0 - pm_yes, 4)


# ── SCANNING ──────────────────────────────────────────────────────────────────

async def _paper_scan(st: PaperAssetState) -> bool:
    cfg = await _acfg()
    best = await asyncio.to_thread(_best_signal, st, cfg)
    if not best:
        return False

    market_id    = best.get("polymarket_market_id") or ""
    deribit_prob = float(best.get("deribit_prob") or 0)
    pm_yes_price = float(best.get("polymarket_price") or 0)
    question     = best.get("polymarket_question") or "?"
    edge_yes     = float(best.get("edge_yes") or 0)
    edge_no      = float(best.get("edge_no") or 0)

    if not market_id:
        return False

    fair = deribit_prob
    can_yes = edge_yes >= MIN_EDGE and fair >= MIN_FAIR_PROB
    can_no  = edge_no  >= MIN_EDGE and (1.0 - fair) >= MIN_FAIR_PROB

    if can_yes and can_no:
        outcome = "YES" if edge_yes >= edge_no else "NO"
    elif can_yes:
        outcome = "YES"
    elif can_no:
        outcome = "NO"
    else:
        return False

    entry_price = pm_yes_price if outcome == "YES" else round(1.0 - pm_yes_price, 4)
    entry_edge  = edge_yes if outcome == "YES" else edge_no

    if entry_price > cfg.max_poly_entry_price:
        logger.info("%s paper entry price %.4f > max %.2f — skipping", st.tag, entry_price, cfg.max_poly_entry_price)
        return False

    # Resolve token IDs (for live bid lookups during monitoring)
    token_id = None
    try:
        from trading.algo_trader import _resolve_token_ids
        yes_token, no_token = await asyncio.to_thread(_resolve_token_ids, market_id)
        token_id = yes_token if outcome == "YES" else no_token
    except Exception:
        pass

    logger.info(
        "%s PAPER BUY  %s '%s'  price=%.4f  edge=%.1f%%  deribit=%.3f  conf=%s  [SIMULATED]",
        st.tag, outcome, question[:70], entry_price,
        float(best.get("abs_edge_pct") or 0), deribit_prob,
        best.get("interp_confidence", "?"),
    )

    st.state           = "MONITORING"
    st.active_market_id = market_id
    st.active_outcome  = outcome
    st.active_token_id = token_id
    st.entry_price     = entry_price
    st.entry_edge      = round(entry_edge, 6)
    st.fill_time       = datetime.now(timezone.utc).isoformat()
    st.question        = question
    st.entry_size_usd  = cfg.order_usd
    if not st.market_end_date:
        st.market_end_date = best.get("market_resolution_at") or None
    return True


# ── MONITORING ────────────────────────────────────────────────────────────────

async def _paper_monitor(st: PaperAssetState) -> None:
    cfg = await _acfg()

    entry_price = st.entry_price
    outcome     = st.active_outcome
    market_id   = st.active_market_id

    if not entry_price or not outcome or not market_id:
        logger.warning("%s invalid paper state — resetting", st.tag)
        st.reset()
        return

    live_price = await asyncio.to_thread(_current_price, market_id, outcome, st.active_token_id)

    if live_price is None:
        logger.info("%s market %s not found in signals — holding", st.tag, market_id)
        return

    pnl_pct = (live_price - entry_price) / entry_price * 100.0
    logger.info(
        "%s MONITOR  %s  entry=%.4f  now=%.4f  pnl=%.2f%%",
        st.tag, outcome, entry_price, live_price, pnl_pct,
    )

    # ── Exit checks ────────────────────────────────────────────────────────────

    exit_reason: Optional[str] = None

    # 1. Market expired
    if st.market_end_date:
        try:
            end_dt = datetime.fromisoformat(st.market_end_date.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if end_dt <= datetime.now(timezone.utc):
                exit_reason = "market_expired"
        except Exception:
            pass

    # 2. Signal-based exit (evaluate_exit from algorithms_2026-05-28.py)
    if not exit_reason and st.entry_edge is not None and st.fill_time:
        sig = await asyncio.to_thread(_current_signal, market_id)
        if sig is not None:
            deribit_prob_yes = float(sig.get("deribit_prob") or 0)
            current_fair = deribit_prob_yes if outcome == "YES" else 1.0 - deribit_prob_yes
            try:
                fill_dt = datetime.fromisoformat(st.fill_time)
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                decision = evaluate_exit(
                    side=outcome,
                    current_fair=current_fair,
                    current_pm_price=live_price,
                    entry_edge=st.entry_edge,
                    fill_time=fill_dt,
                    min_fair_prob=cfg.min_fair_prob,
                    early_collapse_window_s=cfg.early_collapse_window_s,
                    early_collapse_threshold=cfg.early_collapse_edge_threshold,
                )
                if decision.action == "EXIT":
                    exit_reason = f"signal_exit({decision.reason})"
            except Exception as exc:
                logger.warning("%s evaluate_exit error: %s", st.tag, exc)

    # 3. Hard stop-loss
    if not exit_reason and pnl_pct <= cfg.stop_loss_pct:
        exit_reason = f"stop_loss({pnl_pct:.1f}%)"

    # 4. Profit target
    if not exit_reason and pnl_pct >= cfg.profit_target_pct:
        exit_reason = f"profit_target({pnl_pct:.1f}%)"

    if not exit_reason:
        return

    # ── Simulate close ─────────────────────────────────────────────────────────
    size_shares = round(cfg.order_usd / entry_price, 2)
    spent_usd    = round(entry_price * size_shares, 4)
    received_usd = round(live_price * size_shares, 4)
    net_usd      = round(received_usd - spent_usd, 4)

    logger.info(
        "%s PAPER SELL  %s  reason=%s  entry=%.4f  exit=%.4f  pnl=%.2f%%  net=$%.4f  [SIMULATED]",
        st.tag, outcome, exit_reason, entry_price, live_price, pnl_pct, net_usd,
    )

    # Persist trade record
    trade_record = {
        "asset":         st.asset,
        "question":      st.question,
        "outcome":       outcome,
        "market_id":     market_id,
        "entry_price":   entry_price,
        "exit_price":    live_price,
        "size_shares":   size_shares,
        "spent_usd":     spent_usd,
        "received_usd":  received_usd,
        "net_usd":       net_usd,
        "pnl_pct":       round(pnl_pct, 2),
        "entry_time":    st.fill_time,
        "exit_time":     datetime.now(timezone.utc).isoformat(),
        "exit_reason":   exit_reason,
        "entry_edge":    st.entry_edge,
    }
    await asyncio.to_thread(_append_trade_log, trade_record)

    pnl_tracker = PaperPnL(st.asset)
    pnl_tracker.record(spent_usd, received_usd)
    summary = pnl_tracker.summary()
    logger.info(
        "%s PAPER P&L SUMMARY  trades=%d  wins=%d  losses=%d  total=$%.4f",
        st.tag,
        summary["total_trades"],
        summary["wins"],
        summary["losses"],
        summary["total_pnl_usd"],
    )

    st.block_market(market_id)
    st.reset()


# ── Main loop ─────────────────────────────────────────────────────────────────

async def paper_trader_loop(asset: str) -> None:
    """Paper trading loop for a single asset. Runs forever until cancelled."""
    st = PaperAssetState(asset)
    logger.info("[paper/%s] starting paper trader — state=%s", asset, st.state)

    while True:
        try:
            cfg = await _acfg()
            if not cfg.trading_enabled:
                logger.info("[paper/%s] trading_enabled=False — sleeping", asset)
                await asyncio.sleep(cfg.scan_interval_s)
                continue

            if st.state == "SCANNING":
                await _paper_scan(st)
            elif st.state == "MONITORING":
                await _paper_monitor(st)
            else:
                logger.warning("[paper/%s] unknown state %r — resetting", asset, st.state)
                st.reset()

        except asyncio.CancelledError:
            logger.info("[paper/%s] loop cancelled", asset)
            raise
        except Exception as exc:
            logger.exception("[paper/%s] unhandled error: %s", asset, exc)

        await asyncio.sleep((await _acfg()).scan_interval_s)
