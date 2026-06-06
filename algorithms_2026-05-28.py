"""
algorithms.py — Python port of the algorithm the Kate dashboard renders.

Audience: Aneep (server-side runner). This file is a faithful translation of the
JS in `dashboard-squarespace.html` that deploys to levenstein.net/polymarketinfo.
The runner can import these functions and consume the same CSVs the dashboard
fetches from openclaw-api.aneep.tech, and produce the same fair-value numbers
and the same SIGNAL list users see in the browser.

Source of truth diff'd from on 2026-05-26:
    C:\\Users\\nickl\\Nickolasteam Dropbox\\nicholas levenstein\\agents\\
        TraderKate-Working\\dashboard-squarespace.html

------------------------------------------------------------------------------
Algorithm in one paragraph
------------------------------------------------------------------------------
For each Polymarket BTC/ETH band market [K_lo, K_hi]:
  1. Fetch today + tomorrow Deribit call chains; keep calls with |delta| >= 0.001.
  2. Interpolate |delta| at K_lo and K_hi from each chain (linear between listed
     strikes; tail-extrapolate: above the grid -> 0, below -> 1).
  3. Time-rescale each delta from its native chain horizon (hours-to-Deribit-
     expiry) to the Polymarket resolution horizon, using the N(d1) trick
     delta_target = Phi( Phi^{-1}(delta_source) * sqrt(h_src / h_tgt) ).
  4. Per-chain band prob = max(0, delta(K_lo) - delta(K_hi)).
  5. Calendar-weight across the two chains by linear interp in time-to-PM
     resolution: w1 = (T2 - T_target)/(T2 - T1), w2 = 1 - w1, clipped at edges.
  6. Fair = clip(w1 * prob1 + w2 * prob2, 0, 1).

Signal qualification:
  Edge_YES = fair - pm_yes_price
  Edge_NO  = (1 - fair) - (1 - pm_yes_price)
  Emit SIGNAL when edge >= 0.05 AND winning-side fair >= 0.51.

Exit predicates (per spec v1.1, active_signals.json):
  EXIT when ANY of:
    - Deribit fair on the held side < 0.51
    - |edge| < 0.01 within 10 minutes of fill (early-collapse rule, 2026-05-25)
    - edge sign flipped vs entry
    - market resolved

------------------------------------------------------------------------------
Status vs the locked spec
------------------------------------------------------------------------------
This file mirrors what the LIVE DASHBOARD computes today. The locked next-
version math is the skew-adjusted Breeden-Litzenberger formula in
    C:\\TraderKate\\live-trade-pilot\\FAIR_VALUE_FORMULA.md   (v1.1)
That spec replaces the delta-proxy here with N(d_2) - S*phi(d_1)*sqrt(tau)*
(d_sigma/d_K) per boundary, using mark_iv from the chain rather than delta.
Migration is queued; the runner can implement either, but if both run in
parallel they will disagree on bands that are away from ATM. Coordinate which
one feeds active_signals.json.

------------------------------------------------------------------------------
Dependencies: stdlib only (math, datetime, re, csv, io). No numpy/scipy.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Constants — keep in sync with dashboard-squarespace.html
# ---------------------------------------------------------------------------

OPENCLAW_BASE_URL = "https://openclaw-api.aneep.tech"

# Minimum number of usable call strikes a chain needs before we trust it for
# fair-value computation. Below this we treat the chain as unusable.
MIN_CHAIN_STRIKES = 5

# An option is "alive" only if |delta| is above this floor. Filters out
# post-expiry stubs that report delta=0 across the chain.
MIN_ALIVE_DELTA = 0.001

# Stale-tolerant per-chain cache lifetime.
MAX_CACHE_AGE_S = 15 * 60

# Signal thresholds (per active_signals.json v1.1).
MIN_EDGE = 0.05           # 5 percentage points
MIN_FAIR_PROB = 0.51      # 51% on winning side

# Exit rule (early-collapse, 2026-05-25).
EARLY_COLLAPSE_WINDOW_S = 10 * 60     # 10 minutes after fill
EARLY_COLLAPSE_EDGE_THRESHOLD = 0.01  # |edge| <= 1pp


# ---------------------------------------------------------------------------
# Math primitives — Abramowitz erf, normal CDF, Beasley-Springer-Moro inverse
# ---------------------------------------------------------------------------

def erf(x: float) -> float:
    """Abramowitz & Stegun 7.1.26 — same coefficients as the dashboard JS."""
    a1, a2, a3, a4, a5 = (
        0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429,
    )
    p = 0.3275911
    sign = -1.0 if x < 0 else 1.0
    x = abs(x)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - ((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return sign * y


def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + erf(x / math.sqrt(2.0)))


def phi_inv(p: float) -> float:
    """Inverse standard normal CDF (Beasley-Springer-Moro)."""
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
    pl = 0.02425
    ph = 1.0 - pl
    if p < pl:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= ph:
        q = p - 0.5
        r = q * q
        return (
            ((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]
        ) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
    ) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


# ---------------------------------------------------------------------------
# Delta rescaling and interpolation
# ---------------------------------------------------------------------------

def rescale_delta(delta: float, h_src: float, h_tgt: float) -> float:
    """
    Rescale a Deribit call |delta| from horizon h_src to h_tgt (hours).

    Black-Scholes intuition: |delta| ~ Phi(d1) and d1 scales as 1/sqrt(T).
    So delta_tgt = Phi( Phi^{-1}(delta_src) * sqrt(h_src / h_tgt) ).

    For h_tgt < h_src the chain becomes sharper (ITM more ITM, OTM more OTM).
    For h_tgt > h_src it smooths. Sub-day-only approximation.
    """
    if not math.isfinite(delta) or h_src <= 0 or h_tgt <= 0:
        return delta
    if delta >= 0.99999:
        return 1.0
    if delta <= 0.00001:
        return 0.0
    d1 = phi_inv(delta)
    factor = math.sqrt(h_src / h_tgt)
    return phi(d1 * factor)


def interp_delta(deltas: Mapping[float, float], target: float) -> Optional[float]:
    """
    Linearly interpolate |delta| at `target` strike from the chain `deltas`.

    Returns None only if the chain is empty.
    Tail rule: above max strike -> 0 (call OTM, finishes worthless);
               below min strike -> 1 (call ITM, finishes valuable).
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
    lo = strikes[0]
    hi = strikes[-1]
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
# CSV / chain parsing
# ---------------------------------------------------------------------------

def parse_csv(text: str) -> List[Dict[str, str]]:
    """Parse the openclaw CSV format (UTF-8 BOM tolerant)."""
    if not text:
        return []
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def call_deltas(rows: Iterable[Mapping[str, str]]) -> Dict[float, float]:
    """
    Build a {strike: |delta|} dict from a Deribit chain CSV.

    Only call options with finite strike, finite delta, and |delta| above
    MIN_ALIVE_DELTA are kept. Stores absolute value (puts and calls are
    handled symmetrically downstream).
    """
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


def chain_health(rows: Iterable[Mapping[str, str]]) -> int:
    """Number of alive call strikes the chain offers."""
    return len(call_deltas(rows))


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_EXPIRY_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")


def chain_expiry_hours(rows: Iterable[Mapping[str, str]], now: datetime) -> Optional[float]:
    """
    Parse the chain's 08:00 UTC expiry from any row's `expiry_str` (e.g. '6MAY26')
    and return the hours from `now` to that expiry. Returns None if unparseable.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for r in rows:
        s = (r.get("expiry_str") or "").upper()
        m = _EXPIRY_RE.match(s)
        if not m:
            continue
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2))
        year = 2000 + int(m.group(3))
        if month is None:
            continue
        exp = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)
        return (exp - now).total_seconds() / 3600.0
    return None


# ---------------------------------------------------------------------------
# Time helpers — Polymarket resolution + Deribit next expiry
# ---------------------------------------------------------------------------

def next_pm_resolution_utc(now: datetime) -> datetime:
    """Polymarket BTC/ETH daily band resolution: 16:00 UTC of today (or tomorrow if past)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    r = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if r <= now:
        r = r + timedelta(days=1)
    return r


def next_deribit_expiry_utc(now: datetime) -> datetime:
    """Deribit daily expiry: 08:00 UTC of today (or tomorrow if past)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    r = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if r <= now:
        r = r + timedelta(days=1)
    return r


# ---------------------------------------------------------------------------
# Polymarket question parsing
# ---------------------------------------------------------------------------

_BAND_RE = re.compile(r"\$(\d+(?:,\d{3})*)\D+\$(\d+(?:,\d{3})*)")


def parse_band(question: str) -> Tuple[Optional[int], Optional[int]]:
    """Pull (K_lo, K_hi) out of a question string like 'BTC between $76,000 and $78,000'."""
    m = _BAND_RE.search(question or "")
    if not m:
        return (None, None)
    return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))


# ---------------------------------------------------------------------------
# Fair value — the core function rendered on the dashboard
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

    Inputs:
      chain_today, chain_tomorrow : {strike: |delta|} for the two Deribit chains.
      k_lo, k_hi                  : band boundary strikes.
      w1, w2                      : calendar weights (sum to 1) along the time axis.
      h_src1, h_src2              : hours-to-expiry of each chain (or None to skip rescale).
      h_tgt                       : hours-to-Polymarket resolution.

    Returns the band probability clipped to [0, 1], or None if neither chain
    has usable deltas at the boundaries.
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

    have1 = (dL1 is not None) and (dH1 is not None)
    have2 = (dL2 is not None) and (dH2 is not None)

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
    """
    Dashboard's calendar weighting:
      - if t_target between t1 and t2 -> linear interp by time
      - if t_target <= t1             -> all weight on today
      - else                          -> all weight on tomorrow

    Returns (w1, w2) summing to 1.
    """
    if t1_h < t_target_h < t2_h:
        w1 = (t2_h - t_target_h) / (t2_h - t1_h)
        return (w1, 1.0 - w1)
    if t_target_h <= t1_h:
        return (0.0, 1.0)   # all weight on today's chain
    return (1.0, 0.0)       # all weight on tomorrow's chain


# ---------------------------------------------------------------------------
# Signal qualification — what the dashboard puts in the "Signals" section
# ---------------------------------------------------------------------------

class Signal:
    """A qualifying SIGNAL the dashboard would render and the runner should consider."""

    __slots__ = ("asset", "k_lo", "k_hi", "side", "fair_win", "edge", "take", "market_id")

    def __init__(
        self,
        asset: str,
        k_lo: float,
        k_hi: float,
        side: str,            # 'YES' or 'NO'
        fair_win: float,      # winning-side fair probability
        edge: float,          # in fraction (0.05 = 5pp), NOT percentage points
        take: float,          # the price you'd pay to enter (pm_yes for YES, 1-pm_yes for NO)
        market_id: Optional[str] = None,
    ):
        self.asset = asset
        self.k_lo = k_lo
        self.k_hi = k_hi
        self.side = side
        self.fair_win = fair_win
        self.edge = edge
        self.take = take
        self.market_id = market_id

    def __repr__(self) -> str:
        return (
            f"Signal({self.asset} ${self.k_lo:,.0f}-${self.k_hi:,.0f} "
            f"{self.side} fair={self.fair_win:.3f} edge={self.edge * 100:.1f}pp "
            f"take={self.take:.3f})"
        )


def build_signals(
    markets: Sequence[Mapping],
    *,
    min_edge: float = MIN_EDGE,
    min_fair_prob: float = MIN_FAIR_PROB,
) -> List[Signal]:
    """
    Apply the dashboard's signal filter to a list of market rows.

    Each `market` is expected to expose:
      asset    : 'BTC' or 'ETH'
      lo, hi   : band strikes
      pmYes    : Polymarket YES price (fraction)
      fair     : computed band probability (fraction) — None if unusable
      market_id (optional) : Polymarket condition_id or slug for the runner

    A market can generate BOTH a YES and a NO signal (they're correlated and
    will exit-fight). The dashboard flags the second one with "correlated —
    pick one". The runner should also dedupe to one side per market.

    Returns signals sorted by descending edge.
    """
    recs: List[Signal] = []
    for m in markets:
        fair = m.get("fair")
        if fair is None:
            continue
        pm_yes = float(m.get("pmYes"))
        fair_no = 1.0 - fair
        pm_no = 1.0 - pm_yes
        edge_yes = fair - pm_yes
        edge_no = fair_no - pm_no

        if edge_yes >= min_edge and fair >= min_fair_prob:
            recs.append(Signal(
                asset=m.get("asset", ""),
                k_lo=float(m.get("lo")),
                k_hi=float(m.get("hi")),
                side="YES",
                fair_win=fair,
                edge=edge_yes,
                take=pm_yes,
                market_id=m.get("market_id"),
            ))
        if edge_no >= min_edge and fair_no >= min_fair_prob:
            recs.append(Signal(
                asset=m.get("asset", ""),
                k_lo=float(m.get("lo")),
                k_hi=float(m.get("hi")),
                side="NO",
                fair_win=fair_no,
                edge=edge_no,
                take=pm_no,
                market_id=m.get("market_id"),
            ))

    recs.sort(key=lambda s: s.edge, reverse=True)
    return recs


# ---------------------------------------------------------------------------
# Exit predicates — per spec v1.1 and early-collapse rule (2026-05-25)
# ---------------------------------------------------------------------------

class ExitDecision:
    """Why a held position should be closed (or HOLD if no rule fired)."""

    __slots__ = ("action", "reason")

    def __init__(self, action: str, reason: str):
        self.action = action   # 'HOLD' or 'EXIT'
        self.reason = reason

    def __repr__(self) -> str:
        return f"ExitDecision({self.action}, {self.reason!r})"


def evaluate_exit(
    *,
    side: str,                       # 'YES' or 'NO' — the side held
    current_fair: float,             # current Deribit fair probability of the held side
    current_pm_price: float,         # current Polymarket price of the held side
    entry_edge: float,               # edge (in fraction) at fill time, signed wrt the side
    fill_time: datetime,             # when the position was filled (UTC-aware)
    now: Optional[datetime] = None,  # defaults to datetime.now(timezone.utc)
    market_resolved: bool = False,
    min_fair_prob: float = MIN_FAIR_PROB,
    early_collapse_window_s: float = EARLY_COLLAPSE_WINDOW_S,
    early_collapse_threshold: float = EARLY_COLLAPSE_EDGE_THRESHOLD,
) -> ExitDecision:
    """
    Decide whether to EXIT a held position. The four exit rules per spec v1.1
    plus the early-collapse rule added 2026-05-25.

    `current_fair` and `current_pm_price` must refer to the SAME side as `side`.
      - For a YES position: current_fair = P(band hit), current_pm_price = YES price.
      - For a NO position : current_fair = 1 - P(band hit), current_pm_price = 1 - YES.

    `entry_edge` should be positive at entry (entered because edge >= min_edge).
    A sign flip means current_edge < 0.
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

    # Sign flip — our edge has gone against us.
    if (entry_edge > 0 and current_edge < 0) or (entry_edge < 0 and current_edge > 0):
        return ExitDecision("EXIT", "edge_sign_flip")

    # Early-collapse: within the window after fill, exit if edge has gone flat.
    elapsed_s = (now - fill_time).total_seconds()
    if elapsed_s <= early_collapse_window_s and abs(current_edge) <= early_collapse_threshold:
        return ExitDecision(
            "EXIT",
            f"early_collapse_edge_{abs(current_edge):.3f}_within_{int(elapsed_s)}s",
        )

    return ExitDecision("HOLD", "no_exit_rule_fired")


# ---------------------------------------------------------------------------
# End-to-end convenience wrapper
# ---------------------------------------------------------------------------

def score_market(
    *,
    asset: str,                                       # 'BTC' or 'ETH'
    k_lo: float,
    k_hi: float,
    pm_yes_price: float,
    chain_today_rows: Sequence[Mapping[str, str]],
    chain_tomorrow_rows: Sequence[Mapping[str, str]],
    now: Optional[datetime] = None,
    pm_resolution_utc: Optional[datetime] = None,
) -> Dict[str, Optional[float]]:
    """
    Convenience: take raw chain CSV rows + a market band and return everything
    the dashboard would render for that row.

    Output dict:
      fair       : band probability (None if both chains unusable)
      edge_yes   : fair - pm_yes_price
      edge_no    : (1 - fair) - (1 - pm_yes_price)
      qualifies_yes : bool — passes the SIGNAL filter on YES
      qualifies_no  : bool — passes the SIGNAL filter on NO
      h_src1, h_src2, h_tgt, w1, w2 : intermediate diagnostics
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if pm_resolution_utc is None:
        pm_resolution_utc = next_pm_resolution_utc(now)

    h_tgt = (pm_resolution_utc - now).total_seconds() / 3600.0
    exp1 = next_deribit_expiry_utc(now)
    exp2 = exp1 + timedelta(hours=24)
    t1_h = (exp1 - now).total_seconds() / 3600.0
    t2_h = (exp2 - now).total_seconds() / 3600.0
    w1, w2 = calendar_weights(h_tgt, t1_h, t2_h)

    chain1 = call_deltas(chain_today_rows)
    chain2 = call_deltas(chain_tomorrow_rows)
    h_src1 = chain_expiry_hours(chain_today_rows, now)
    h_src2 = chain_expiry_hours(chain_tomorrow_rows, now)

    fair = compute_fair(chain1, chain2, k_lo, k_hi, w1, w2, h_src1, h_src2, h_tgt)

    if fair is None:
        return {
            "fair": None, "edge_yes": None, "edge_no": None,
            "qualifies_yes": False, "qualifies_no": False,
            "h_src1": h_src1, "h_src2": h_src2, "h_tgt": h_tgt,
            "w1": w1, "w2": w2,
        }

    edge_yes = fair - pm_yes_price
    edge_no = (1.0 - fair) - (1.0 - pm_yes_price)
    return {
        "fair": fair,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "qualifies_yes": edge_yes >= MIN_EDGE and fair >= MIN_FAIR_PROB,
        "qualifies_no": edge_no >= MIN_EDGE and (1.0 - fair) >= MIN_FAIR_PROB,
        "h_src1": h_src1,
        "h_src2": h_src2,
        "h_tgt": h_tgt,
        "w1": w1,
        "w2": w2,
    }


# ---------------------------------------------------------------------------
# Self-check — run `python algorithms.py` for a sanity-check on the math
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1) phi round-trips through phi_inv.
    # BSM inverse is a ~1e-6-precision approximation, so use a tolerant bound.
    for p in (0.05, 0.25, 0.5, 0.71, 0.9):
        back = phi(phi_inv(p))
        assert abs(back - p) < 1e-5, (p, back)

    # 2) rescale_delta: shrinking horizon sharpens probabilities.
    # A 24h delta of 0.71 (modestly ITM) at 7h horizon should sharpen toward 1.
    sharpened = rescale_delta(0.71, h_src=24.0, h_tgt=7.0)
    assert sharpened > 0.71, sharpened
    # At the same horizon, no change (within BSM round-trip noise).
    assert abs(rescale_delta(0.71, h_src=12.0, h_tgt=12.0) - 0.71) < 1e-5

    # 3) interp_delta tail rules.
    chain = {100.0: 0.90, 110.0: 0.50, 120.0: 0.10}
    assert interp_delta(chain, 90.0) == 1.0
    assert interp_delta(chain, 130.0) == 0.0
    assert abs(interp_delta(chain, 105.0) - 0.70) < 1e-9   # linear interp

    # 4) compute_fair degenerate cases.
    assert compute_fair({}, {}, 100, 110, 0.5, 0.5, 12, 12, 7) is None
    # ITM band: with delta_lo high and delta_hi low, band prob ~= dL - dH.
    # When ONE chain is empty, compute_fair falls back to the other regardless
    # of calendar weight — matches dashboard behavior.
    one_chain = {2000.0: 0.95, 2100.0: 0.60, 2200.0: 0.05}
    fair_w1 = compute_fair(one_chain, {}, 2000, 2200, 1.0, 0.0, None, None, None)
    assert fair_w1 is not None and 0.85 < fair_w1 < 0.95, fair_w1
    fair_w0 = compute_fair(one_chain, {}, 2000, 2200, 0.0, 1.0, None, None, None)
    assert fair_w0 is not None and abs(fair_w0 - fair_w1) < 1e-9, (fair_w0, fair_w1)

    # 5) signal qualification.
    markets = [
        {"asset": "BTC", "lo": 76000, "hi": 78000, "pmYes": 0.62, "fair": 0.71},   # YES edge 9pp, fair 71% → qualify
        {"asset": "BTC", "lo": 78000, "hi": 80000, "pmYes": 0.30, "fair": 0.55},   # NO edge: 0.45 - 0.70 = -25pp; YES edge 25pp BUT fair 55% qualifies YES
        {"asset": "ETH", "lo": 2000,  "hi": 2100,  "pmYes": 0.40, "fair": 0.42},   # YES edge 2pp — below threshold
        {"asset": "ETH", "lo": 2100,  "hi": 2200,  "pmYes": 0.20, "fair": 0.30},   # YES fair below 0.51 → no qualify
    ]
    sigs = build_signals(markets)
    sides = [(s.asset, s.k_lo, s.side) for s in sigs]
    assert ("BTC", 76000.0, "YES") in sides
    assert ("BTC", 78000.0, "YES") in sides
    assert all(s.fair_win >= MIN_FAIR_PROB and s.edge >= MIN_EDGE for s in sigs)

    # 6) exit predicates.
    fill = datetime.now(timezone.utc) - timedelta(minutes=3)
    d = evaluate_exit(
        side="YES", current_fair=0.40, current_pm_price=0.55,
        entry_edge=0.10, fill_time=fill,
    )
    assert d.action == "EXIT" and "deribit_fair" in d.reason, d

    d = evaluate_exit(
        side="YES", current_fair=0.60, current_pm_price=0.595,
        entry_edge=0.10, fill_time=fill,
    )
    # |edge| = 0.005 within 10 minutes → early collapse.
    assert d.action == "EXIT" and "early_collapse" in d.reason, d

    d = evaluate_exit(
        side="YES", current_fair=0.65, current_pm_price=0.55,
        entry_edge=0.10, fill_time=fill,
    )
    assert d.action == "HOLD", d

    print("algorithms.py self-check passed.")
    
    assert d.action == "HOLD", d

    print("algorithms.py self-check passed.")
    
    assert d.action == "HOLD", d

    print("algorithms.py self-check passed.")
