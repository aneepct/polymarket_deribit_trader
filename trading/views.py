import asyncio
from django.http import JsonResponse
from django.views import View


class PositionsView(View):
    def get(self, request):
        from trading.polymarket_client import fetch_positions
        open_only = request.GET.get("open_only", "true").lower() != "false"
        try:
            positions = fetch_positions(open_only=open_only)
            return JsonResponse({"positions": positions, "total": len(positions)})
        except Exception as exc:
            return JsonResponse({"error": str(exc)}, status=500)


class StateView(View):
    """Return the current Redis state for all configured assets."""
    def get(self, request):
        from trading.models import TradingConfig
        from trading.state import AssetState
        cfg = TradingConfig.load()
        result = {}
        for asset in cfg.asset_list():
            st = AssetState(asset)
            result[asset] = {
                "state": st.state,
                "active_token_id": st.active_token_id,
                "active_order_id": st.active_order_id,
                "active_sell_order_id": st.active_sell_order_id,
                "active_outcome": st.active_outcome,
                "market_end_date": st.market_end_date,
                "extra_token_ids": st.extra_token_ids,
            }
        return JsonResponse(result)
