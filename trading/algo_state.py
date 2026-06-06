"""
algo_state.py — Redis-backed state for the algo trader.

Identical to AssetState (trading/state.py) but uses the key prefix
'algo:state:' so it does not conflict with the main trader ('trader:state:').
"""
from __future__ import annotations

from django.core.cache import cache
from trading.state import _ttl, AssetState

_ALGO_PREFIX = "algo:state"


def _algo_key(asset: str) -> str:
    return f"{_ALGO_PREFIX}:{asset.upper()}"


class AlgoAssetState(AssetState):
    """
    Thin subclass of AssetState that stores all state under 'algo:state:<ASSET>'
    instead of 'trader:state:<ASSET>', keeping the two services fully isolated.
    """

    def _get(self, field, default=None):
        val = cache.get(_algo_key(self.asset))
        if val is None:
            return default
        return val.get(field, default)

    def _set(self, **kwargs):
        key = _algo_key(self.asset)
        current = cache.get(key) or {}
        current.update(kwargs)
        cache.set(key, current, timeout=_ttl())

    def reset(self) -> None:
        cache.set(_algo_key(self.asset), {
            "state": "SCANNING",
            "active_token_id": None,
            "active_order_id": None,
            "active_outcome": None,
            "active_sell_order_id": None,
            "market_end_date": None,
            "extra_token_ids": [],
            "active_market_id": None,
            "entry_edge": None,
            "fill_time": None,
            "blocked_markets": self.blocked_markets,
        }, timeout=_ttl())

    def close_and_promote(self) -> None:
        extras = self.extra_token_ids
        if extras:
            cache.set(_algo_key(self.asset), {
                "state": "MONITORING",
                "active_token_id": extras[0],
                "active_order_id": None,
                "active_outcome": "UNKNOWN",
                "active_sell_order_id": None,
                "market_end_date": self.market_end_date,
                "extra_token_ids": extras[1:],
                "active_market_id": None,
                "entry_edge": None,
                "fill_time": None,
                "blocked_markets": self.blocked_markets,
            }, timeout=_ttl())
        else:
            self.reset()
