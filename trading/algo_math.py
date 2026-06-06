"""
algo_math.py — Pure-Python math primitives for the delta-interpolation algorithm.

Ported from algorithms_2026-05-28.py (the Kate dashboard formula).
No external dependencies — stdlib only (math, datetime, re).

Exported symbols used by algo_signals, algo_trader, paper_trader:
  phi, phi_inv, rescale_delta, interp_delta, call_deltas,
  chain_expiry_hours, compute_fair, calendar_weights,
  parse_band, next_deribit_expiry_utc, evaluate_exit,
  MIN_EDGE, MIN_FAIR_PROB,
  EARLY_COLLAPSE_WINDOW_S, EARLY_COLLAPSE_EDGE_THRESHOLD
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_EDGE      = 0.05    # 5 percentage points
MIN_FAIR_PROB = 0.51    # 51% on winning side
MIN_ALIVE_DELTA = 0.001

EARLY_COLLAPSE_WINDOW_S      = 10 * 60   # 10 minutes
EARLY_COLLAPSE_EDGE_THRESHOLD = 0.01     # 1pp


# ---------------------------------------------------------------------------
# Normal CDF and its inverse
# ---------------------------------------------------------------------------

def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def phi_inv(p: float) -> float:
    """Inverse standard normal CDF (Beasley-Springer-Moro approximation)."""
    if p <= 0:
        return float("-inf")
    if p >= 1:
        return float("inf")
    a = [
        -3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
        1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
        6.680131188771972e+01, -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
        -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
        3.754408661907416e+00,
    ]
    pl, ph = 0.02425, 1.0 - 0.02425
    if p < pl:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if p <= ph:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)


# ---------------------------------------------------------------------------
# Delta rescaling
# ---------------------------------------------------------------------------

def rescale_delta(delta: float, h_src: float, h_tgt: float) -> float:
    """
    Rescale a call |delta| from horizon h_src to h_tgt (hours).
    delta_tgt = Phi( Phi^{-1}(delta_src) * sqrt(h_src / h_tgt) )
    """
    if not math.isfinite(delta) or h_src <= 0 or h_tgt <= 0:
        return delta
    if delta >= 0.99999:
        return 1.0
    if delta <= 0.00001:
        return 0.0
    return phi(phi_inv(delta) * math.sqrt(h_src / h_tgt))


# ---------------------------------------------------------------------------
# Delta interpolation
# ---------------------------------------------------------------------------

def interp_delta(deltas: Mapping[float, float], target: float) -> Optional[float]:
    """
    Linearly interpolate |delta| at *target* strike.
    Tail rules: above max strike → 0, below min strike → 1.
    """
    strikes = sorted(deltas.keys())
    if not strikes:
        return None
    if target in deltas:
        return deltas[target]
    if target > strikes[-1]:
        return 0.0
    if target < strikes[0]:
        return 1.0
    lo = hi = strikes[0]
    for s in strikes:
        if s <= target:
            lo = s
        if s >= target:
            hi = s
            break
    if hi == lo:
        return deltas[lo]
    frac = (target - lo) / (hi - lo)
    return deltas[lo] + frac * (deltas[hi] - deltas[lo])


# ---------------------------------------------------------------------------
# Chain parsing
# ---------------------------------------------------------------------------

def call_deltas(rows: Iterable[Mapping]) -> Dict[float, float]:
    """Build {strike: |delta|} from Deribit ticker rows (calls only)."""
    out: Dict[float, float] = {}
    for r in rows:
        if (r.get("option_type") or "").strip() != "C":
            continue
        try:
            s = float(r.get("strike", ""))
            d = float(r.get("delta", ""))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(s) and math.isfinite(d)):
            continue
        if abs(d) < MIN_ALIVE_DELTA:
            continue
        out[s] = abs(d)
    return out


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_EXPIRY_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")


def chain_expiry_hours(rows: Iterable[Mapping], now: datetime) -> Optional[float]:
    """Hours from *now* to 08:00 UTC expiry parsed from any row's expiry_str."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for r in rows:
        s = (r.get("expiry_str") or "").upper()
        m = _EXPIRY_RE.match(s)
        if not m:
            continue
        month = _MONTHS.get(m.group(2))
        if month is None:
            continue
        exp = datetime(2000 + int(m.group(3)), month, int(m.group(1)), 8, 0, 0, tzinfo=timezone.utc)
        return (exp - now).total_seconds() / 3600.0
    return None


# ---------------------------------------------------------------------------
# Fair value computation
# ---------------------------------------------------------------------------

def compute_fair(
    chain_today: Mapping[float, float],
    chain_tomorrow: Mapping[float, float],
    k_lo: float,
    k_hi: float,
    w1: float,
    w2: float,
    h_src1: Optional[float],
    h_src2: Optional[float],
    h_tgt: float,
) -> Optional[float]:
    """
    Two-chain calendar-weighted band probability with per-chain time rescaling.
    P(band) = max(0, delta(K_lo) - delta(K_hi)) weighted across today/tomorrow chains.
    """
    dL1 = interp_delta(chain_today, k_lo)
    dH1 = interp_delta(chain_today, k_hi)
    dL2 = interp_delta(chain_tomorrow, k_lo)
    dH2 = interp_delta(chain_tomorrow, k_hi)

    if h_tgt and h_src1 and h_src1 > 0 and h_tgt > 0:
        if dL1 is not None:
            dL1 = rescale_delta(dL1, h_src1, h_tgt)
        if dH1 is not None:
            dH1 = rescale_delta(dH1, h_src1, h_tgt)
    if h_tgt and h_src2 and h_src2 > 0 and h_tgt > 0:
        if dL2 is not None:
            dL2 = rescale_delta(dL2, h_src2, h_tgt)
        if dH2 is not None:
            dH2 = rescale_delta(dH2, h_src2, h_tgt)

    have1 = dL1 is not None and dH1 is not None
    have2 = dL2 is not None and dH2 is not None

    if have1 and have2:
        f = w1 * max(0.0, dL1 - dH1) + w2 * max(0.0, dL2 - dH2)
    elif have1:
        f = max(0.0, dL1 - dH1)
    elif have2:
        f = max(0.0, dL2 - dH2)
    else:
        return None

    return max(0.0, min(1.0, f))


def calendar_weights(t_target_h: float, t1_h: float, t2_h: float) -> Tuple[float, float]:
    """Linear calendar weight between two Deribit chains."""
    if t1_h < t_target_h < t2_h:
        w1 = (t2_h - t_target_h) / (t2_h - t1_h)
        return (w1, 1.0 - w1)
    if t_target_h <= t1_h:
        return (0.0, 1.0)
    return (1.0, 0.0)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def next_deribit_expiry_utc(now: datetime) -> datetime:
    """Next Deribit daily expiry: 08:00 UTC today or tomorrow."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    r = now.replace(hour=8, minute=0, second=0, microsecond=0)
    return r if r > now else r + timedelta(days=1)


# ---------------------------------------------------------------------------
# Question parsing
# ---------------------------------------------------------------------------

_BAND_RE = re.compile(r"\$(\d+(?:,\d{3})*)\D+\$(\d+(?:,\d{3})*)")


def parse_band(question: str) -> Tuple[Optional[int], Optional[int]]:
    """Extract (K_lo, K_hi) from a question like 'BTC between $76,000 and $78,000'."""
    m = _BAND_RE.search(question or "")
    if not m:
        return (None, None)
    return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))


# ---------------------------------------------------------------------------
# Exit decision
# ---------------------------------------------------------------------------

class ExitDecision:
    __slots__ = ("action", "reason")

    def __init__(self, action: str, reason: str):
        self.action = action   # 'HOLD' or 'EXIT'
        self.reason = reason

    def __repr__(self) -> str:
        return f"ExitDecision({self.action}, {self.reason!r})"


def evaluate_exit(
    *,
    side: str,
    current_fair: float,
    current_pm_price: float,
    entry_edge: float,
    fill_time: datetime,
    now: Optional[datetime] = None,
    market_resolved: bool = False,
    min_fair_prob: float = MIN_FAIR_PROB,
    early_collapse_window_s: float = EARLY_COLLAPSE_WINDOW_S,
    early_collapse_threshold: float = EARLY_COLLAPSE_EDGE_THRESHOLD,
) -> ExitDecision:
    """
    Decide whether to EXIT a held position. Four rules per spec v1.1:
      1. market_resolved
      2. current_fair < min_fair_prob  (conviction gone)
      3. edge sign flipped vs entry
      4. early-collapse: |edge| <= threshold within window_s of fill
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if fill_time.tzinfo is None:
        fill_time = fill_time.replace(tzinfo=timezone.utc)

    if market_resolved:
        return ExitDecision("EXIT", "market_resolved")

    if current_fair < min_fair_prob:
        return ExitDecision("EXIT", f"deribit_fair_lt_{min_fair_prob:.2f}")

    current_edge = current_fair - current_pm_price
    if (entry_edge > 0 and current_edge < 0) or (entry_edge < 0 and current_edge > 0):
        return ExitDecision("EXIT", "edge_sign_flip")

    elapsed_s = (now - fill_time).total_seconds()
    if elapsed_s <= early_collapse_window_s and abs(current_edge) <= early_collapse_threshold:
        return ExitDecision(
            "EXIT",
            f"early_collapse_edge_{abs(current_edge):.3f}_within_{int(elapsed_s)}s",
        )

    return ExitDecision("HOLD", "no_exit_rule_fired")
