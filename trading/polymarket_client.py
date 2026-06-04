"""
Polymarket trading client — reads credentials from the DB (PolymarketCredentials)
and tuning from TradingConfig. No env vars needed at runtime.
"""
from __future__ import annotations

import json
from functools import lru_cache

import httpx
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
from py_clob_client_v2.order_utils.model.side import Side


def _get_config():
    from trading.models import TradingConfig, PolymarketCredentials
    cfg = TradingConfig.load()
    creds = PolymarketCredentials.load()
    return cfg, creds


def _build_client() -> ClobClient:
    cfg, creds = _get_config()
    if not creds.private_key:
        raise RuntimeError("Polymarket private key not set. Configure it in Django admin.")
    client = ClobClient(
        cfg.polymarket_clob_api,
        key=creds.private_key,
        chain_id=cfg.chain_id,
        signature_type=3,
        funder=creds.funder_address or None,
    )
    api_creds = client.derive_api_key()
    client.set_api_creds(api_creds)
    return client


# Module-level cache — invalidated by calling _reset_client()
_client_cache: ClobClient | None = None
_client_key_fingerprint: str = ""


def _get_client() -> ClobClient:
    """Return a cached ClobClient, rebuilding if credentials have changed."""
    global _client_cache, _client_key_fingerprint
    from trading.models import PolymarketCredentials
    creds = PolymarketCredentials.load()
    fingerprint = creds.private_key[:8] if creds.private_key else ""
    if _client_cache is None or fingerprint != _client_key_fingerprint:
        _client_cache = _build_client()
        _client_key_fingerprint = fingerprint
    return _client_cache


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def fetch_positions(open_only: bool = True) -> list[dict]:
    cfg, creds = _get_config()
    if not creds.funder_address:
        raise RuntimeError("Funder address not set. Configure it in Django admin.")
    r = httpx.get(
        f"{cfg.polymarket_data_api}/positions",
        params={
            "user": creds.funder_address,
            "sizeThreshold": cfg.positions_size_threshold,
            "limit": cfg.positions_limit,
        },
        timeout=cfg.http_timeout_s,
    )
    r.raise_for_status()
    positions = r.json()
    if open_only:
        positions = [p for p in positions if not p.get("redeemable")]
    return positions


# ---------------------------------------------------------------------------
# Open orders
# ---------------------------------------------------------------------------

def fetch_open_orders() -> list[dict]:
    cfg, _ = _get_config()
    client = _get_client()
    headers = client._l2_headers("GET", "/data/orders")
    r = httpx.get(
        f"{cfg.polymarket_clob_api}/data/orders",
        headers=headers,
        params={"limit": cfg.positions_limit},
        timeout=cfg.http_timeout_s,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("data", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Create order
# ---------------------------------------------------------------------------

def create_order(token_id: str, price: float, size: float, side: str) -> dict:
    if not (0 < price < 1):
        raise ValueError("price must be between 0 and 1 (exclusive)")
    if size <= 0:
        raise ValueError("size must be positive")
    side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL
    client = _get_client()
    order = OrderArgsV2(token_id=token_id, price=price, size=size, side=side_enum)
    signed = client.create_order(order)
    resp = client.post_order(signed, OrderType.GTC)
    return resp if isinstance(resp, dict) else vars(resp)


# ---------------------------------------------------------------------------
# Cancel order
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Orderbook best bid
# ---------------------------------------------------------------------------

def fetch_best_bid(token_id: str) -> float | None:
    """Return the highest bid price for *token_id*, or None on failure."""
    cfg, _ = _get_config()
    try:
        r = httpx.get(
            f"{cfg.polymarket_clob_api}/book",
            params={"token_id": token_id},
            timeout=cfg.http_timeout_s,
        )
        r.raise_for_status()
        bids = r.json().get("bids", [])
        if bids:
            return float(bids[0]["price"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Cancel order
# ---------------------------------------------------------------------------

def cancel_order(order_id: str) -> dict:
    cfg, _ = _get_config()
    client = _get_client()
    body = {"orderID": order_id}
    headers = client._l2_headers("DELETE", "/order", body=body)
    headers["Content-Type"] = "application/json"
    r = httpx.request(
        "DELETE",
        f"{cfg.polymarket_clob_api}/order",
        headers=headers,
        content=json.dumps(body),
        timeout=cfg.http_timeout_s,
    )
    r.raise_for_status()
    return r.json() if r.content else {"cancelled": True}
