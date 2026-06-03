"""
Redis-backed asset state — replaces the in-memory _AssetState dataclass.

All fields are stored under a Redis hash key:
    trader:state:<ASSET>   (e.g. trader:state:BTC)

This means the state survives process restarts, container redeploys, etc.
"""
from __future__ import annotations

from typing import Optional

from django.core.cache import cache

_PREFIX = "trader:state"


def _ttl() -> int:
    """Read TTL seconds from TradingConfig so it's configurable from admin."""
    try:
        from trading.models import TradingConfig
        return int(TradingConfig.load().redis_state_ttl_hours * 3600)
    except Exception:
        return 48 * 3600  # safe fallback if DB unavailable


def _key(asset: str) -> str:
    return f"{_PREFIX}:{asset.upper()}"


class AssetState:
    """
    Proxy object over a Redis hash. Reads/writes go directly to Redis so that
    any process (web, worker, management command) sees the same state.
    """

    def __init__(self, asset: str):
        self.asset = asset.upper()

    @property
    def tag(self) -> str:
        return f"[trader/{self.asset}]"

    def _get(self, field: str, default=None):
        key = _key(self.asset)
        val = cache.get(key)
        if val is None:
            return default
        return val.get(field, default)

    def _set(self, **kwargs):
        key = _key(self.asset)
        current: dict = cache.get(key) or {}
        current.update(kwargs)
        cache.set(key, current, timeout=_ttl())

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._get("state", "SCANNING")

    @state.setter
    def state(self, v: str):
        self._set(state=v)

    @property
    def active_token_id(self) -> Optional[str]:
        return self._get("active_token_id")

    @active_token_id.setter
    def active_token_id(self, v: Optional[str]):
        self._set(active_token_id=v)

    @property
    def active_order_id(self) -> Optional[str]:
        return self._get("active_order_id")

    @active_order_id.setter
    def active_order_id(self, v: Optional[str]):
        self._set(active_order_id=v)

    @property
    def active_outcome(self) -> Optional[str]:
        return self._get("active_outcome")

    @active_outcome.setter
    def active_outcome(self, v: Optional[str]):
        self._set(active_outcome=v)

    @property
    def active_sell_order_id(self) -> Optional[str]:
        return self._get("active_sell_order_id")

    @active_sell_order_id.setter
    def active_sell_order_id(self, v: Optional[str]):
        self._set(active_sell_order_id=v)

    @property
    def market_end_date(self) -> Optional[str]:
        return self._get("market_end_date")

    @market_end_date.setter
    def market_end_date(self, v: Optional[str]):
        self._set(market_end_date=v)

    @property
    def extra_token_ids(self) -> list:
        return self._get("extra_token_ids", [])

    @extra_token_ids.setter
    def extra_token_ids(self, v: list):
        self._set(extra_token_ids=v)

    @property
    def active_market_id(self) -> Optional[str]:
        return self._get("active_market_id")

    @active_market_id.setter
    def active_market_id(self, v: Optional[str]):
        self._set(active_market_id=v)

    @property
    def entry_edge(self) -> Optional[float]:
        return self._get("entry_edge")

    @entry_edge.setter
    def entry_edge(self, v: Optional[float]):
        self._set(entry_edge=v)

    @property
    def fill_time(self) -> Optional[str]:
        """ISO UTC datetime string set when BUY fill is first confirmed."""
        return self._get("fill_time")

    @fill_time.setter
    def fill_time(self, v: Optional[str]):
        self._set(fill_time=v)

    # ── Mutations (atomic-ish via full hash replace) ─────────────────────────

    def reset(self) -> None:
        cache.set(_key(self.asset), {
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
        }, timeout=_ttl())

    def close_and_promote(self) -> None:
        extras = self.extra_token_ids
        if extras:
            cache.set(_key(self.asset), {
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
            }, timeout=_ttl())
        else:
            self.reset()
