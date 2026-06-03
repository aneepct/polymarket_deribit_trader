from django.contrib import admin
from .models import TradingConfig, PolymarketCredentials


@admin.register(TradingConfig)
class TradingConfigAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Scheduler", {
            "fields": ("trading_enabled",),
            "description": "Toggle to pause/resume the trading loop without restarting the process. Takes effect within one scan cycle.",
        }),
        ("Order Sizing", {
            "fields": ("order_usd", "min_shares"),
        }),
        ("Profit / Loss", {
            "fields": ("profit_target_pct", "stop_loss_pct"),
            "description": "stop_loss_pct applies to extra positions only; primary position uses signal-based exit rules below.",
        }),
        ("Timing", {
            "fields": ("scan_interval_s", "today_lookahead_hours"),
        }),
        ("Deribit Signal", {
            "fields": ("deribit_neutral_low", "deribit_neutral_high"),
        }),
        ("Exit Rules (spec v1.1)", {
            "fields": ("min_fair_prob", "early_collapse_window_s", "early_collapse_edge_threshold"),
            "description": "Signal-based exit rules for the primary position. Exit when Deribit conviction is gone, edge flips, or early-collapse fires.",
        }),
        ("Assets", {
            "fields": ("assets",),
        }),
        ("CLOB / Chain", {
            "fields": ("chain_id",),
        }),
        ("Positions API", {
            "fields": ("positions_limit", "positions_size_threshold"),
        }),
        ("HTTP & Redis", {
            "fields": ("http_timeout_s", "redis_state_ttl_hours"),
        }),
        ("API Endpoints", {
            "fields": ("polymarket_clob_api", "polymarket_data_api", "gamma_api"),
            "classes": ("collapse",),
        }),
    )

    def has_add_permission(self, request):
        return not TradingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PolymarketCredentials)
class PolymarketCredentialsAdmin(admin.ModelAdmin):
    fields = ("private_key", "funder_address")

    def has_add_permission(self, request):
        return not PolymarketCredentials.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
